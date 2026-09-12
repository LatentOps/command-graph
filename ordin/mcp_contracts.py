"""Reviewed MCP contract pins; discovery never creates trusted effects."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .action import ActionReview


MCP_CONTRACT_LOCK_SCHEMA_VERSION = "ordin.mcp_contract_lock.v1"
MAX_CONTRACT_BYTES = 1_048_576
MAX_CONTRACT_DEPTH = 32
MAX_CONTRACT_TOOLS = 256
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMA_ARRAYS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SCHEMA_VALUES = {
    "items",
    "additionalProperties",
    "additionalItems",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "propertyNames",
    "unevaluatedProperties",
    "unevaluatedItems",
}


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{label} requires exact bounded non-empty text")
    return value


def _canonical(value: Any, depth: int = 0) -> Any:
    if depth > MAX_CONTRACT_DEPTH:
        raise ValueError("MCP contract exceeds nesting limit")
    if isinstance(value, Mapping):
        if len(value) > 1024 or any(not isinstance(key, str) for key in value):
            raise ValueError("MCP contract object is invalid or too large")
        return {key: _canonical(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        if len(value) > 1024:
            raise ValueError("MCP contract array is too large")
        return [_canonical(item, depth + 1) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > 2**53:
            raise ValueError("MCP contract number is outside the canonical numeric range")
        return int(value) if value.is_integer() else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError("MCP contract contains non-JSON data")


def canonical_json(value: Any) -> bytes:
    try:
        result = json.dumps(
            _canonical(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("MCP contract cannot be canonicalized") from exc
    if len(result) > MAX_CONTRACT_BYTES:
        raise ValueError("MCP contract exceeds byte limit")
    return result


def _schema(value: Any, *, root: Mapping[str, Any], depth: int = 0) -> Any:
    if depth > MAX_CONTRACT_DEPTH:
        raise ValueError("MCP schema exceeds nesting limit")
    if isinstance(value, bool):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("MCP schema nodes must be objects or booleans")
    result = {}
    for key, item in value.items():
        if key in {"description", "title", "$comment", "examples"}:
            continue
        if key in {"$dynamicRef", "$recursiveRef"}:
            raise ValueError("dynamic MCP schema references cannot be verified")
        if key == "$ref":
            if not isinstance(item, str) or (item != "#" and not item.startswith("#/")):
                raise ValueError("only local MCP schema references can be verified")
            target: Any = root
            for part in item[2:].split("/") if item != "#" else []:
                part = part.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, Mapping) or part not in target:
                    raise ValueError("MCP schema reference cannot be resolved locally")
                target = target[part]
            if not isinstance(target, (Mapping, bool)):
                raise ValueError("MCP schema reference does not identify a schema")
        if key == "$schema" and (
            not isinstance(item, str)
            or item
            not in {
                "https://json-schema.org/draft/2020-12/schema",
                "http://json-schema.org/draft-07/schema#",
                "https://json-schema.org/draft-07/schema#",
            }
        ):
            raise ValueError("unsupported MCP JSON schema revision")
        if key in {
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "minProperties",
            "maxProperties",
            "minContains",
            "maxContains",
        }:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError("MCP schema bounds must be non-negative integers")
        if key in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}:
            if (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or (key == "multipleOf" and item <= 0)
            ):
                raise ValueError("invalid MCP numeric schema bound")
        if key in {"uniqueItems", "readOnly", "writeOnly", "deprecated"} and not isinstance(
            item, bool
        ):
            raise ValueError("invalid MCP schema boolean annotation")
        if key in {"pattern", "format", "$id", "$anchor"} and not isinstance(item, str):
            raise ValueError("invalid MCP schema string keyword")
        if key == "enum" and (not isinstance(item, list) or not item):
            raise ValueError("invalid MCP schema enum")
        if key == "type":
            types = item if isinstance(item, list) else [item]
            if not types or any(
                not isinstance(t, str)
                or t not in {"null", "boolean", "object", "array", "number", "integer", "string"}
                for t in types
            ):
                raise ValueError("invalid MCP schema type")
        if key == "required":
            if (
                not isinstance(item, list)
                or any(not isinstance(t, str) for t in item)
                or len(set(item)) != len(item)
            ):
                raise ValueError("invalid MCP schema required properties")
            item = sorted(item)
        if key in _SCHEMA_MAPS:
            if not isinstance(item, Mapping):
                raise ValueError("invalid MCP schema map")
            item = {
                name: _schema(child, root=root, depth=depth + 1) for name, child in item.items()
            }
        elif key in _SCHEMA_ARRAYS:
            if not isinstance(item, list) or not item:
                raise ValueError("invalid MCP schema composition")
            item = [_schema(child, root=root, depth=depth + 1) for child in item]
        elif key in _SCHEMA_VALUES:
            item = _schema(item, root=root, depth=depth + 1)
        result[key] = item
    return result


def canonical_tool_contract(tool: Mapping[str, Any]) -> dict[str, Any]:
    # Bound the original, including ignored prose, before traversing schemas.
    if not isinstance(tool, Mapping):
        raise ValueError("MCP tool contract must be an object")
    parsed = json.loads(canonical_json(tool))
    name = _text(parsed.get("name"), "MCP tool name")
    result: dict[str, Any] = {"name": name}
    for field in ("inputSchema", "outputSchema"):
        if field not in parsed and field == "outputSchema":
            continue
        schema = parsed.get(field)
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            raise ValueError(f"MCP {field} must declare an object schema")
        result[field] = _schema(schema, root=schema)
    annotations = parsed.get("annotations", {})
    if not isinstance(annotations, Mapping):
        raise ValueError("MCP tool annotations must be an object")
    for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        if key in annotations and not isinstance(annotations[key], bool):
            raise ValueError("MCP invocation hints must be boolean")
    result["annotations"] = {key: value for key, value in annotations.items() if key != "title"}
    execution = parsed.get("execution", {})
    if (
        not isinstance(execution, Mapping)
        or not isinstance(execution.get("taskSupport", "forbidden"), str)
        or execution.get("taskSupport", "forbidden") not in {"forbidden", "optional", "required"}
    ):
        raise ValueError("invalid MCP task execution contract")
    result["execution"] = {"taskSupport": "forbidden", **execution}
    # Future execution fields remain pinned; only known display metadata is
    # excluded. Discovery metadata is never interpreted as effects or resources.
    for key in parsed.keys() - {
        "name",
        "inputSchema",
        "outputSchema",
        "annotations",
        "execution",
        "title",
        "description",
        "icons",
    }:
        result[key] = parsed[key]
    return result


def tool_contract_digest(tool: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(canonical_tool_contract(tool))).hexdigest()


def semantics_binding_digest(semantics: Any, shell_tools: frozenset[str] = frozenset()) -> str:
    registry = getattr(semantics, "registry", semantics)
    return hashlib.sha256(
        canonical_json(
            {
                "tool_semantics": registry.as_dict() if registry is not None else None,
                "shell_tools": sorted(shell_tools),
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class MCPContractCheck:
    server: str
    tool: str
    status: str
    expected_digest: str | None
    observed_digest: str | None

    def __post_init__(self) -> None:
        _text(self.server, "MCP server")
        _text(self.tool, "MCP tool")
        if self.status not in {
            "matched",
            "missing",
            "unverified",
            "ambiguous",
            "invalid",
            "semantics_changed",
            "unpinned",
            "changed",
        }:
            raise ValueError("invalid MCP contract verification status")
        for digest in (self.expected_digest, self.observed_digest):
            if digest is not None and (
                not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
            ):
                raise ValueError("invalid contract verification digest")
        if self.verified and (
            self.expected_digest is None or self.expected_digest != self.observed_digest
        ):
            raise ValueError("a verified contract requires matching expected and observed digests")

    @property
    def verified(self) -> bool:
        return self.status == "matched"

    def as_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "tool": self.tool,
            "status": self.status,
            "expected_digest": self.expected_digest,
            "observed_digest": self.observed_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MCPContractCheck:
        fields = {"server", "tool", "status", "expected_digest", "observed_digest"}
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError("invalid MCP contract check fields")
        if not isinstance(payload["status"], str):
            raise ValueError("invalid MCP contract check status")
        return cls(
            payload["server"],
            payload["tool"],
            payload["status"],
            payload["expected_digest"],
            payload["observed_digest"],
        )

    def apply(self, review: ActionReview) -> ActionReview:
        from .provenance import DecisionProvenance, ProvenanceRecord

        params = review.action.parameters
        if (review.action.kind, review.action.operation) != ("mcp", "call") and not (
            (review.action.kind, review.action.operation) == ("shell", "execute")
            and params.get("source_runtime") == "mcp"
        ):
            raise ValueError("contract checks require an MCP-derived action")
        identity = (
            (params.get("server"), params.get("tool"))
            if review.action.kind == "mcp"
            else (params.get("source_server"), params.get("source_tool"))
        )
        if identity != (self.server, self.tool):
            raise ValueError("MCP contract evidence identity does not match the action")
        decision = review.decision if self.verified or review.blocked else "ask"
        reason = f"MCP contract verification: {self.status}; review the exact contract and local semantics before execution"
        provenance = review.provenance or DecisionProvenance(
            records=(), final_decision=review.decision, final_risk=review.risk
        )
        provenance = provenance.append(
            ProvenanceRecord(
                source="adapter",
                kind="finding",
                code=f"mcp.contract.{self.status}",
                action_id=review.action.action_id,
                decision=decision,
                metadata=self.as_dict(),
            ),
            final_decision=decision,
        )
        return replace(
            review,
            decision=decision,
            reasons=review.reasons if self.verified else [reason, *review.reasons],
            provenance=provenance,
        )


@dataclass(frozen=True)
class MCPContractLock:
    semantics_digest: str
    pins: Mapping[tuple[str, str], str]
    protocol_revision: str = "2025-11-25"

    def __post_init__(self) -> None:
        if not isinstance(self.semantics_digest, str) or not _DIGEST.fullmatch(
            self.semantics_digest
        ):
            raise ValueError("invalid semantics binding digest")
        if not self.pins or len(self.pins) > MAX_CONTRACT_TOOLS:
            raise ValueError("MCP lock requires 1 to 256 exact contract pins")
        for (server, tool), digest in self.pins.items():
            _text(server, "MCP server")
            _text(tool, "MCP tool")
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError("invalid MCP contract pin digest")
        _text(self.protocol_revision, "MCP protocol revision")
        object.__setattr__(self, "pins", MappingProxyType(dict(self.pins)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MCP_CONTRACT_LOCK_SCHEMA_VERSION,
            "canonicalization": "ordin.mcp_contract.v1",
            "semantics_digest": self.semantics_digest,
            "protocol_revision": self.protocol_revision,
            "pins": [
                {"server": server, "tool": tool, "digest": digest}
                for (server, tool), digest in sorted(self.pins.items())
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MCPContractLock:
        from .schema import validate_named_schema

        if validate_named_schema("mcp_contract_lock", dict(payload)):
            raise ValueError("invalid MCP contract lock schema")
        pins: dict[tuple[str, str], str] = {}
        for pin in payload["pins"]:
            identity = (pin["server"], pin["tool"])
            if identity in pins:
                raise ValueError("duplicate MCP contract pin identity")
            pins[identity] = pin["digest"]
        return cls(payload["semantics_digest"], pins, payload["protocol_revision"])


def load_contract_json(path: str | Path) -> Mapping[str, Any]:
    # Reuse the maintained strict duplicate-member/finite-number parser. Import
    # lazily to avoid a transport/contract module initialization cycle.
    from .mcp_proxy import _parse_jsonrpc_line

    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_CONTRACT_BYTES + 1)
    if len(raw) > MAX_CONTRACT_BYTES:
        raise ValueError("MCP contract file exceeds byte limit")
    payload = _parse_jsonrpc_line(raw)
    canonical_json(payload)
    return payload


class MCPContractObserver:
    """Correlated, bounded discovery state for one exact upstream connection."""

    def __init__(self, server: str, lock: MCPContractLock, semantics_digest: str) -> None:
        self.server, self.lock = server, lock
        self.semantics_digest = semantics_digest
        self.live: dict[str, str] = {}
        self._building: dict[str, str] = {}
        self._request: str | int | float | None = None
        self._cursor: str | None = None
        self._seen_cursors: set[str] = set()
        self.status = "missing"

    def invalidate(self, status: str = "unverified") -> None:
        self.live.clear()
        self._building.clear()
        self._request = None
        self._cursor = None
        self._seen_cursors.clear()
        self.status = status

    def request(self, request_id: str | int | float, params: Mapping[str, Any]) -> None:
        cursor = params.get("cursor")
        if self._request is not None:
            raise ValueError("a contract discovery request is already in flight")
        if cursor is None:
            self.invalidate()
        elif not isinstance(cursor, str) or cursor != self._cursor or cursor in self._seen_cursors:
            self.invalidate("ambiguous")
            raise ValueError("MCP discovery cursor does not match the current contract inventory")
        if cursor is not None:
            if len(self._seen_cursors) >= MAX_CONTRACT_TOOLS:
                self.invalidate("invalid")
                raise ValueError("MCP discovery exceeds page limit")
            self._seen_cursors.add(cursor)
        self._request = request_id

    def response(self, request_id: Any, message: Mapping[str, Any]) -> bool:
        if self._request is None or request_id != self._request:
            return False
        self._request = None
        try:
            if "method" in message or "error" in message:
                raise ValueError("contract discovery did not return a terminal result")
            result = message.get("result")
            if not isinstance(result, Mapping) or not isinstance(result.get("tools"), list):
                raise ValueError("invalid tools/list contract response")
            canonical_json(result)
            for tool in result["tools"]:
                if not isinstance(tool, Mapping):
                    raise ValueError("invalid MCP tool contract")
                name = _text(tool.get("name"), "MCP tool")
                if name in self._building or len(self._building) >= MAX_CONTRACT_TOOLS:
                    raise ValueError("duplicate or excessive MCP tool contracts")
                self._building[name] = tool_contract_digest(tool)
            cursor = result.get("nextCursor")
            if cursor is not None:
                if (
                    not isinstance(cursor, str)
                    or not cursor
                    or len(cursor) > 4096
                    or cursor in self._seen_cursors
                ):
                    raise ValueError("invalid MCP contract pagination cursor")
                self._cursor = cursor
            else:
                self.live = dict(self._building)
                self._building.clear()
                self._cursor = None
                self.status = "complete"
        except (ValueError, TypeError):
            self.invalidate("invalid")
        return True

    def check(self, tool: str) -> MCPContractCheck:
        expected = self.lock.pins.get((self.server, tool))
        observed = self.live.get(tool)
        if self.semantics_digest != self.lock.semantics_digest:
            status = "semantics_changed"
        elif expected is None:
            status = "unpinned"
        elif self.status != "complete":
            status = self.status
        elif observed is None:
            status = "missing"
        elif observed != expected:
            status = "changed"
        else:
            status = "matched"
        return MCPContractCheck(self.server, tool, status, expected, observed)
