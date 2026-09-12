from __future__ import annotations

import json
import re
from pathlib import Path
from string import Formatter
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCHEMA_DIR = ROOT / "schemas"
PACKAGE_SCHEMA_DIR = Path(__file__).resolve().parent / "resources" / "schemas"
SCHEMA_DIR = SOURCE_SCHEMA_DIR if SOURCE_SCHEMA_DIR.exists() else PACKAGE_SCHEMA_DIR
SOURCE_DATA_DIR = ROOT / "data"
PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "resources"
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
VALID_RISKS = {"unknown", "low", "medium", "high", "critical"}
KNOWN_TEMPLATE_FIELDS = {
    "url",
    "host",
    "port",
    "pattern",
    "path",
    "depth",
    "package",
    "process",
    "branch",
}
SCHEMA_FILES = {
    "integration_session": "integration-session.v1.schema.json",
    "command_card": "command-card.v1.schema.json",
    "search_result": "search-result.v1.schema.json",
    "risk_review": "risk-review.v1.schema.json",
    "review_result": "review-result.v1.schema.json",
    "action_envelope": "action-envelope.v1.schema.json",
    "action_history": "action-history.v1.schema.json",
    "action_review": "action-review.v1.schema.json",
    "execution_capabilities": "execution-capabilities.v1.schema.json",
    "action_observation": "action-observation.v1.schema.json",
    "observation_history": "observation-history.v1.schema.json",
    "provenance": "provenance.v1.schema.json",
    "audit_event": "audit-event.v1.schema.json",
    "policy_set": "policy-set.v1.schema.json",
    "temporal_policy_set": "temporal-policy-set.v1.schema.json",
    "tool_semantics": "tool-semantics.v1.schema.json",
    "review_request": "review-request.v1.schema.json",
    "risk_rules": "risk-rules.v1.schema.json",
    "effect_catalog": "effect-catalog.v1.schema.json",
    "effect_graph": "effect-graph.v1.schema.json",
    "command_pack": "command-pack.v1.schema.json",
    "pack_list": "pack-list.v1.schema.json",
}


def load_schema(name: str) -> dict[str, Any]:
    filename = SCHEMA_FILES.get(name, name)
    path = SCHEMA_DIR / filename
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"schema {filename!r} must contain a JSON object")
    return payload


def _type_matches(instance: Any, expected: str) -> bool:
    if expected == "null":
        return instance is None
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "object":
        return isinstance(instance, dict)
    return True


def validate_instance(
    instance: Any,
    schema: dict[str, Any],
    path: str = "$",
) -> list[str]:
    errors: list[str] = []

    expected_type = schema.get("type")
    if expected_type is not None:
        expected = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(isinstance(item, str) and _type_matches(instance, item) for item in expected):
            errors.append(f"{path}: expected type {expected_type!r}")
            return errors

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value {instance!r} is not in enum")

    if isinstance(instance, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(instance) < min_length:
            errors.append(f"{path}: string is shorter than {min_length}")
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(instance) > max_length:
            errors.append(f"{path}: string is longer than {max_length}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, instance) is None:
                    errors.append(f"{path}: string does not match pattern {pattern!r}")
            except re.error as exc:
                errors.append(f"{path}: schema has invalid pattern: {exc}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and instance < minimum:
            errors.append(f"{path}: value is below minimum {minimum}")

    if isinstance(instance, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(instance) < min_items:
            errors.append(f"{path}: array has fewer than {min_items} items")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(instance) > max_items:
            errors.append(f"{path}: array has more than {max_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, value in enumerate(instance):
                errors.extend(validate_instance(value, item_schema, f"{path}[{index}]"))

    if isinstance(instance, dict):
        max_properties = schema.get("maxProperties")
        if isinstance(max_properties, int) and len(instance) > max_properties:
            errors.append(f"{path}: object has more than {max_properties} properties")
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in instance:
                    errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        for key, value in instance.items():
            if key in properties and isinstance(properties[key], dict):
                errors.extend(validate_instance(value, properties[key], f"{path}.{key}"))
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                errors.append(f"{path}: unexpected property {key!r}")
            elif isinstance(additional, dict):
                errors.extend(validate_instance(value, additional, f"{path}.{key}"))

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        branch_errors = [
            validate_instance(instance, branch, path)
            for branch in any_of
            if isinstance(branch, dict)
        ]
        if branch_errors and not any(not item for item in branch_errors):
            errors.append(f"{path}: value does not satisfy anyOf")

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        successes = sum(
            not validate_instance(instance, branch, path)
            for branch in one_of
            if isinstance(branch, dict)
        )
        if successes != 1:
            errors.append(f"{path}: value must satisfy exactly one oneOf branch")

    return errors


def validate_named_schema(name: str, instance: Any) -> list[str]:
    return validate_instance(instance, load_schema(name))


def validate_schema_files() -> list[str]:
    errors: list[str] = []
    for name, filename in SCHEMA_FILES.items():
        try:
            schema = load_schema(name)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"schema {filename}: {exc}")
            continue
        if schema.get("$schema") != DRAFT_2020_12:
            errors.append(f"schema {filename}: expected Draft 2020-12 declaration")
        if not isinstance(schema.get("$id"), str) or not schema["$id"]:
            errors.append(f"schema {filename}: missing $id")
        if schema.get("type") != "object":
            errors.append(f"schema {filename}: top-level type must be object")
    return errors


def validate_command_card_schemas(commands: Iterable[dict[str, Any]]) -> list[str]:
    schema = load_schema("command_card")
    errors: list[str] = []
    for entry in commands:
        name = entry.get("command", "<unknown>")
        for error in validate_instance(entry, schema):
            errors.append(f"command {name}: {error}")
    return errors


def validate_risk_rule_semantics(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    rules = payload.get("rules", [])
    if not isinstance(rules, list):
        return ["risk rules payload: rules must be a list"]
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"risk rule {index}: must be an object")
            continue
        rule_id = rule.get("id")
        if isinstance(rule_id, str):
            if rule_id in seen:
                errors.append(f"risk rule {rule_id}: duplicate id")
            seen.add(rule_id)
        pattern = rule.get("pattern")
        if isinstance(pattern, str):
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"risk rule {rule_id or index}: invalid regex: {exc}")
        if rule.get("risk") not in VALID_RISKS - {"unknown"}:
            errors.append(f"risk rule {rule_id or index}: invalid risk {rule.get('risk')!r}")
    return errors


def _template_fields(template: str) -> set[str]:
    result: set[str] = set()
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name:
            result.add(field_name)
    return result


def validate_template_semantics(commands: Iterable[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for entry in commands:
        command = entry.get("command", "<unknown>")
        templates = entry.get("templates", [])
        if not isinstance(templates, list):
            errors.append(f"command {command}: templates must be a list")
            continue
        for index, template in enumerate(templates):
            if not isinstance(template, dict):
                continue
            text = template.get("command")
            if not isinstance(text, str):
                continue
            try:
                fields = _template_fields(text)
            except ValueError as exc:
                errors.append(f"command {command} template {index}: invalid format: {exc}")
                continue
            unknown = sorted(fields - KNOWN_TEMPLATE_FIELDS)
            if unknown:
                errors.append(
                    f"command {command} template {index}: unknown template fields "
                    + ", ".join(unknown)
                )
            defaults = template.get("safe_defaults", {})
            if defaults is not None and not isinstance(defaults, dict):
                errors.append(
                    f"command {command} template {index}: safe_defaults must be an object"
                )
            elif isinstance(defaults, dict):
                extra_defaults = sorted(set(defaults) - fields)
                if extra_defaults:
                    errors.append(
                        f"command {command} template {index}: safe_defaults reference "
                        + ", ".join(extra_defaults)
                    )
    return errors


def _compare_json_tree(
    source_root: Path,
    package_root: Path,
    label: str,
    *,
    ignored_package_roots: set[str] | None = None,
) -> list[str]:
    ignored = ignored_package_roots or set()
    errors: list[str] = []
    source_files = {
        path.relative_to(source_root) for path in source_root.rglob("*.json") if path.is_file()
    }
    package_files = {
        path.relative_to(package_root)
        for path in package_root.rglob("*.json")
        if path.is_file()
        and not (
            path.relative_to(package_root).parts
            and path.relative_to(package_root).parts[0] in ignored
        )
    }
    for relative in sorted(source_files | package_files):
        source_path = source_root / relative
        package_path = package_root / relative
        if relative not in package_files:
            errors.append(f"packaged {label} missing: {relative.as_posix()}")
            continue
        if relative not in source_files:
            errors.append(f"packaged {label} has extra file: {relative.as_posix()}")
            continue
        try:
            source_payload = json.loads(source_path.read_text(encoding="utf-8"))
            package_payload = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{label} {relative.as_posix()}: {exc}")
            continue
        if source_payload != package_payload:
            errors.append(f"packaged {label} differs from source: {relative.as_posix()}")
    return errors


def resource_parity_errors() -> list[str]:
    if not SOURCE_DATA_DIR.exists() or not PACKAGE_DATA_DIR.exists():
        return []
    errors = _compare_json_tree(
        SOURCE_DATA_DIR,
        PACKAGE_DATA_DIR,
        "resource",
        ignored_package_roots={"schemas"},
    )
    if SOURCE_SCHEMA_DIR.exists() and PACKAGE_SCHEMA_DIR.exists():
        errors.extend(
            _compare_json_tree(
                SOURCE_SCHEMA_DIR,
                PACKAGE_SCHEMA_DIR,
                "schema",
            )
        )
    return sorted(set(errors))
