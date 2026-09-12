"""Small shared checks for data-only public input contracts."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping


def reject_unknown(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be an object")
    if any(key not in allowed for key in payload):
        raise ValueError(f"{label} contains unknown fields")


def load_configuration(path: str | Path, *, label: str, maximum: int) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains a duplicate JSON member")
            result[key] = value
        return result

    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(maximum + 1)
    except OSError as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc
    if len(raw) > maximum:
        raise ValueError(f"{label} exceeds maximum size {maximum} bytes")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"{label} requires finite JSON numbers")
            if isinstance(item, (dict, list)):
                if depth > 32:
                    raise ValueError(f"{label} nesting exceeds its limit")
                pending.extend(
                    (child, depth + 1)
                    for child in (item.values() if isinstance(item, dict) else item)
                )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid {label} JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value
