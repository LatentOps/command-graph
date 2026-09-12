from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from . import AUDIT_EVENT_SCHEMA_VERSION, __version__
from .action import ActionReview
from ._audit_storage import MAX_AUDIT_BYTES, load_audit_line, locked_audit, signature
from .policy import Decision
from .provenance import DecisionProvenance


MAX_AUDIT_LINE_BYTES = 1_048_576


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def action_digest(review: ActionReview) -> str:
    canonical = _canonical_json(review.action.as_dict())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_hash(payload: dict[str, Any]) -> str:
    material = dict(payload)
    material.pop("event_hash", None)
    canonical = _canonical_json(material)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    timestamp: str
    ordin_version: str
    action_digest: str
    action_kind: str
    operation: str
    decision: Decision
    risk: str
    policy: dict[str, str] | None
    provenance: DecisionProvenance
    previous_hash: str | None = None
    event_hash: str | None = None
    schema_version: str = AUDIT_EVENT_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
            "ordin_version": self.ordin_version,
            "action_digest": self.action_digest,
            "action_kind": self.action_kind,
            "operation": self.operation,
            "decision": self.decision,
            "risk": self.risk,
            "policy": dict(self.policy) if self.policy else None,
            "provenance": self.provenance.as_dict(),
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
        }


@dataclass(frozen=True)
class AuditVerification:
    ok: bool
    event_count: int
    last_hash: str | None
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "event_count": self.event_count,
            "last_hash": self.last_hash,
            "errors": list(self.errors),
        }


class AuditSink(Protocol):
    def record(self, review: ActionReview) -> AuditEvent: ...


def build_audit_event(
    review: ActionReview,
    *,
    timestamp: str | None = None,
    previous_hash: str | None = None,
    hash_chain: bool = False,
    include_resource_values: bool = False,
    include_summaries: bool = False,
    include_action_ids: bool = False,
) -> AuditEvent:
    if review.provenance is None:
        raise ValueError("generic action review does not contain decision provenance")
    provenance = review.provenance.for_audit(
        include_resource_values=include_resource_values,
        include_summaries=include_summaries,
        include_action_ids=include_action_ids,
    )
    event = AuditEvent(
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        ordin_version=__version__,
        action_digest=action_digest(review),
        action_kind=review.action.kind,
        operation=review.action.operation,
        decision=review.decision,
        risk=review.risk,
        policy=dict(review.policy) if review.policy else None,
        provenance=provenance,
        previous_hash=previous_hash if hash_chain else None,
    )
    if not hash_chain:
        return event
    digest = _event_hash(event.as_dict())
    return AuditEvent(
        timestamp=event.timestamp,
        ordin_version=event.ordin_version,
        action_digest=event.action_digest,
        action_kind=event.action_kind,
        operation=event.operation,
        decision=event.decision,
        risk=event.risk,
        policy=event.policy,
        provenance=event.provenance,
        previous_hash=event.previous_hash,
        event_hash=digest,
    )


class JsonlAuditSink:
    """Explicit local JSONL audit sink. No sink is configured by default."""

    def __init__(
        self,
        path: str | Path,
        *,
        hash_chain: bool = False,
        include_resource_values: bool = False,
        include_summaries: bool = False,
        include_action_ids: bool = False,
        fsync: bool = True,
    ) -> None:
        self.path = Path(path)
        self.hash_chain = hash_chain
        self.include_resource_values = include_resource_values
        self.include_summaries = include_summaries
        self.include_action_ids = include_action_ids
        self.fsync = fsync
        self._lock = threading.Lock()
        self._last_hash: str | None = None
        self._signature: tuple[int, ...] | None = None
        if self.hash_chain and self.path.exists() and self.path.stat().st_size:
            with locked_audit(self.path) as fd:
                with os.fdopen(os.dup(fd), "rb") as handle:
                    verification = _verify_audit_stream(handle, require_hash_chain=True)
                self._signature = signature(os.fstat(fd))
            if not verification.ok:
                raise ValueError(
                    "existing audit hash chain is invalid: " + "; ".join(verification.errors)
                )
            self._last_hash = verification.last_hash

    def record(self, review: ActionReview) -> AuditEvent:
        with self._lock, locked_audit(self.path) as fd:
            info = os.fstat(fd)
            if self.hash_chain and signature(info) != self._signature:
                # Reuse the verified head only when the file is unchanged. Other
                # instances/processes must observe the current head under the lock.
                with os.fdopen(os.dup(fd), "rb") as handle:
                    handle.seek(0)
                    verification = _verify_audit_stream(handle, require_hash_chain=True)
                if not verification.ok:
                    raise ValueError(
                        "existing audit hash chain is invalid: " + "; ".join(verification.errors)
                    )
                self._last_hash = verification.last_hash
            event = build_audit_event(
                review,
                previous_hash=self._last_hash,
                hash_chain=self.hash_chain,
                include_resource_values=self.include_resource_values,
                include_summaries=self.include_summaries,
                include_action_ids=self.include_action_ids,
            )
            line = (_canonical_json(event.as_dict()) + "\n").encode("utf-8")
            if len(line) > MAX_AUDIT_LINE_BYTES:
                raise ValueError(f"audit event exceeds maximum size {MAX_AUDIT_LINE_BYTES} bytes")
            if info.st_size + len(line) > MAX_AUDIT_BYTES:
                raise ValueError("audit file exceeds byte limit")
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("audit append made no progress")
                view = view[written:]
            if self.fsync:
                os.fsync(fd)
            if self.hash_chain:
                self._last_hash = event.event_hash
            self._signature = signature(os.fstat(fd))
            return event


def verify_audit_jsonl(
    path: str | Path,
    *,
    require_hash_chain: bool = False,
    expected_last_hash: str | None = None,
) -> AuditVerification:
    audit_path = Path(path)
    if not audit_path.exists():
        return AuditVerification(
            ok=False,
            event_count=0,
            last_hash=None,
            errors=("audit file does not exist",),
        )

    try:
        with audit_path.open("rb") as handle:
            result = _verify_audit_stream(handle, require_hash_chain=require_hash_chain)
            if expected_last_hash is not None and result.last_hash != expected_last_hash:
                return AuditVerification(
                    False,
                    result.event_count,
                    result.last_hash,
                    (*result.errors, "audit head differs from trusted checkpoint"),
                )
            return result
    except OSError as exc:
        return AuditVerification(False, 0, None, (str(exc),))


def _verify_audit_stream(handle: BinaryIO, *, require_hash_chain: bool) -> AuditVerification:
    errors: list[str] = []
    previous_hash: str | None = None
    event_count = 0
    try:
        if os.fstat(handle.fileno()).st_size > MAX_AUDIT_BYTES:
            return AuditVerification(False, 0, None, ("audit file exceeds byte limit",))
        for line_number, raw in enumerate(
            iter(lambda: handle.readline(MAX_AUDIT_LINE_BYTES + 1), b""), start=1
        ):
            if len(raw) > MAX_AUDIT_LINE_BYTES:
                errors.append(f"line {line_number}: audit event exceeds maximum size")
                break
            if not raw.endswith(b"\n"):
                errors.append(f"line {line_number}: incomplete audit line")
            try:
                payload = load_audit_line(raw)
            except (UnicodeDecodeError, ValueError, RecursionError):
                errors.append(f"line {line_number}: invalid or ambiguous JSON")
                break
            if not isinstance(payload, dict):
                errors.append(f"line {line_number}: audit event must be an object")
                continue
            from .schema import validate_named_schema

            schema_errors = validate_named_schema("audit_event", payload)
            if schema_errors:
                errors.extend(f"line {line_number}: {error}" for error in schema_errors)
            provenance = payload.get("provenance")
            if isinstance(provenance, dict):
                if payload.get("decision") != provenance.get("final_decision"):
                    errors.append(f"line {line_number}: decision disagrees with provenance")
                if payload.get("risk") != provenance.get("final_risk"):
                    errors.append(f"line {line_number}: risk disagrees with provenance")
            event_count += 1
            event_hash = payload.get("event_hash")
            previous = payload.get("previous_hash")
            if event_hash is None:
                if require_hash_chain:
                    errors.append(f"line {line_number}: missing event_hash")
                if previous is not None:
                    errors.append(f"line {line_number}: previous_hash without event_hash")
                previous_hash = None
                continue
            if not isinstance(event_hash, str) or len(event_hash) != 64:
                errors.append(f"line {line_number}: invalid event_hash")
                continue
            if previous != previous_hash:
                errors.append(f"line {line_number}: previous_hash does not match prior event")
            expected = _event_hash(payload)
            if event_hash != expected:
                errors.append(f"line {line_number}: event_hash mismatch")
            previous_hash = event_hash
    except OSError as exc:
        return AuditVerification(
            ok=False,
            event_count=event_count,
            last_hash=previous_hash,
            errors=(str(exc),),
        )

    return AuditVerification(
        ok=not errors,
        event_count=event_count,
        last_hash=previous_hash,
        errors=tuple(errors),
    )
