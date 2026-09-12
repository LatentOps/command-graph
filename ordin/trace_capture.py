"""Opt-in local action evidence. Default records contain no action arguments."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import __version__
from ._private_storage import private_database
from .action import ActionReview, _temporal_evidence_for_action
from .agent import AgentGate
from .api import Ordin
from .audit import AuditEvent, AuditSink, build_audit_event
from .execution import ActionObservation
from .data import load_effect_catalog
from .schema import load_schema, validate_instance
from .policy import ReviewPolicy, validate_fail_threshold
from .session import _load, configuration_digest


TRACE_EVENT_SCHEMA_VERSION = "ordin.trace_event.v1"
MAX_TRACE_EVENTS = 4096
MAX_TRACE_EVENT_BYTES = 1_048_576
_HEX = re.compile(r"^[a-f0-9]{64}$")
_INTEGRATIONS = {"python", "claude-code", "codex", "mcp-proxy", "mcp-http"}
_RESOURCE_TYPES = {
    "path",
    "file",
    "directory",
    "url",
    "host",
    "network",
    "endpoint",
    "process",
    "package",
    "branch",
    "unknown",
}


def raw_capture_flag(value: str) -> bool:
    if value not in {"0", "1"}:
        raise ValueError("raw capture flag must be explicitly 0 or 1")
    return value == "1"


@lru_cache(maxsize=1)
def _event_schema() -> dict[str, Any]:
    return load_schema("trace_event")


def _validate_event(payload: Mapping[str, Any]) -> None:
    if validate_instance(dict(payload), _event_schema()):
        raise ValueError("invalid trace event schema")


def _effects(values: Any) -> list[str]:
    catalog = load_effect_catalog()
    return [value for value in values if value in catalog][:64]


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@lru_cache(maxsize=1)
def source_revision() -> str | None:
    """Use installed VCS metadata when available; never infer the caller's repository."""
    try:
        raw = distribution("ordin").read_text("direct_url.json")
        value = json.loads(raw or "{}").get("vcs_info", {}).get("commit_id")
        return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40,64}", value) else None
    except (PackageNotFoundError, ValueError, AttributeError):
        return None


def _resources(values: Any) -> list[dict[str, str]]:
    return [
        {
            "type": resource.type if resource.type in _RESOURCE_TYPES else "unknown",
            "value_digest": digest(resource.value),
        }
        for resource in list(values)[:64]
    ]


class TraceRecorder:
    def __init__(
        self,
        path: str | Path,
        *,
        integration: str,
        raw_local: bool = False,
        session_id: str | None = None,
        config_digest: str | None = None,
        fail_on: str = "warn",
    ) -> None:
        if integration not in _INTEGRATIONS:
            raise ValueError("trace capture requires a known integration identity")
        if not isinstance(raw_local, bool):
            raise ValueError("raw local capture must be explicitly boolean")
        self.path = Path(path).absolute()
        self.integration = integration
        self.raw_local = raw_local
        self.default_session = digest(session_id) if session_id is not None else None
        self.config_digest = config_digest
        self.policy = ReviewPolicy(validate_fail_threshold(fail_on))
        self._permission = Ordin(policy=self.policy)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with private_database(self.path, "trace") as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS capture_meta (id INTEGER PRIMARY KEY, mode TEXT NOT NULL)"
            )
            row = connection.execute("SELECT mode FROM capture_meta WHERE id=1").fetchone()
            mode = "raw_local" if self.raw_local else "metadata"
            if row is None:
                connection.execute("INSERT INTO capture_meta VALUES (1, ?)", (mode,))
            elif row[0] != mode:
                raise ValueError("trace mode mismatch; use a new capture file")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, session_key TEXT NOT NULL, action_key TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(session_key, action_key, kind))"
            )
            yield connection

    def _append(
        self,
        connection: sqlite3.Connection,
        session_key: str,
        action_key: str,
        kind: str,
        payload: Mapping[str, Any],
    ) -> None:
        _validate_event(payload)
        raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > MAX_TRACE_EVENT_BYTES:
            raise ValueError("trace event exceeds byte limit")
        if connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= MAX_TRACE_EVENTS:
            raise ValueError("trace capture is full; rotate or disable capture explicitly")
        if connection.execute(
            "SELECT 1 FROM events WHERE session_key=? AND action_key=? AND kind=?",
            (session_key, action_key, kind),
        ).fetchone():
            raise ValueError("duplicate captured action or observation")
        connection.execute(
            "INSERT INTO events(session_key, action_key, kind, payload) VALUES (?, ?, ?, ?)",
            (session_key, action_key, kind, raw),
        )

    def record_review(self, review: ActionReview) -> None:
        if not review.action.action_id:
            raise ValueError("trace review requires an action_id")
        parameters = review.action.parameters
        metadata = parameters.get("integration", {})
        session_key = metadata.get("session_id_sha256") if isinstance(metadata, Mapping) else None
        session_key = session_key or self.default_session
        if not isinstance(session_key, str) or not _HEX.fullmatch(session_key):
            raise ValueError("trace review requires an explicit session identity")
        identity = {
            key: digest(parameters[key])
            for key in (
                "runtime",
                "server",
                "tool",
                "source_runtime",
                "source_server",
                "source_tool",
            )
            if key in parameters
        }
        action_key = digest(review.action.action_id)
        # Temporal signals are bounded semantic evidence, not command text.
        signals = _temporal_evidence_for_action(review.action, review=review).signals
        allowed = self._permission.allows(review)
        disposition = (
            "execute"
            if allowed
            else "deny"
            if review.blocked or self.integration == "codex"
            else "escalate"
        )
        payload: dict[str, Any] = {
            "schema_version": TRACE_EVENT_SCHEMA_VERSION,
            "event": "review",
            "integration": self.integration,
            "integration_version": "1",
            "ordin_version": __version__,
            "ordin_revision": source_revision(),
            "configuration_digest": self.config_digest,
            "fail_on": self.policy.fail_on,
            "kind": review.action.kind
            if review.action.kind in {"shell", "tool", "mcp"}
            else "unknown",
            "operation": review.action.operation
            if review.action.operation in {"execute", "call"}
            else "unknown",
            "identity": identity,
            "decision": review.decision,
            "risk": review.risk,
            "disposition": disposition,
            "effects": _effects(review.effects),
            "resources": _resources(review.resources),
            "truncated": len(review.effects) != len(_effects(review.effects))
            or len(review.resources) > 64
            or any(r.type not in _RESOURCE_TYPES for r in review.resources),
            "temporal_signals": sorted(
                signals & {"signal:path_execution", "category:secret_exposure"}
            ),
            "categories": [digest(category) for category in review.trajectory_categories][:32],
            "provenance_codes": [
                digest(record.code)
                for record in (review.provenance.records if review.provenance else ())
            ][:64],
        }
        if self.raw_local:
            payload["raw_action"] = review.action.as_dict()
        with self._connection() as connection:
            self._append(connection, session_key, action_key, "review", payload)

    def record_observation(
        self, observation: ActionObservation, *, session_key: str | None = None
    ) -> None:
        action_key = digest(observation.action_id)
        selected = session_key or self.default_session
        with self._connection() as connection:
            if selected is None:
                matches = connection.execute(
                    "SELECT session_key FROM events WHERE action_key=? AND kind='review'",
                    (action_key,),
                ).fetchall()
                if len(matches) != 1:
                    raise ValueError("trace observation requires an unambiguous captured action")
                selected = matches[0][0]
            row = connection.execute(
                "SELECT payload, seq FROM events WHERE session_key=? AND action_key=? AND kind='review'",
                (selected, action_key),
            ).fetchone()
            if row is None:
                raise ValueError("trace observation does not match a captured action")
            if connection.execute(
                "SELECT 1 FROM events WHERE session_key=? AND kind='boundary' AND seq>?",
                (selected, row[1]),
            ).fetchone():
                raise ValueError("trace observation crosses a session lifecycle boundary")
            review = json.loads(row[0])
            if review["disposition"] == "deny":
                raise ValueError("trace observation cannot attach to a denied action")
            status = observation.metadata.get("status")
            if not isinstance(status, str) or status not in {
                "success",
                "failure",
                "reported",
                "protocol_error",
                "tool_error",
                "task_accepted",
                "input_required",
                "unrecognized_result_type",
            }:
                status = "reported"
            payload = {
                "schema_version": TRACE_EVENT_SCHEMA_VERSION,
                "event": "observation",
                "integration": self.integration,
                "exit_code": observation.exit_code,
                "effects": _effects(observation.effects),
                "resources": _resources(observation.resources),
                "truncated": len(observation.effects) != len(_effects(observation.effects))
                or len(observation.resources) > 64
                or any(r.type not in _RESOURCE_TYPES for r in observation.resources),
                "status": status,
            }
            self._append(connection, selected, action_key, "observation", payload)

    def record_boundary(self, session_key: str) -> None:
        if not _HEX.fullmatch(session_key):
            raise ValueError("invalid capture session identity")
        with self._connection() as connection:
            self._append(
                connection,
                session_key,
                digest(uuid.uuid4().hex),
                "boundary",
                {
                    "schema_version": TRACE_EVENT_SCHEMA_VERSION,
                    "event": "boundary",
                    "integration": self.integration,
                },
            )


class TraceAuditSink:
    def __init__(self, recorder: TraceRecorder, audit: AuditSink | None = None) -> None:
        self.recorder, self.audit = recorder, audit

    def record(self, review: ActionReview) -> AuditEvent:
        self.recorder.record_review(review)
        return self.audit.record(review) if self.audit is not None else build_audit_event(review)


def attach_trace(
    ordin: Ordin,
    path: str | Path | None,
    *,
    integration: str,
    raw_local: bool = False,
    configuration: Mapping[str, Any] | None = None,
) -> tuple[Ordin, TraceRecorder | None]:
    if path is None:
        if raw_local:
            raise ValueError("raw local capture requires an explicit trace path")
        return ordin, None
    recorder = TraceRecorder(
        path,
        integration=integration,
        raw_local=raw_local,
        config_digest=digest(
            {"evaluator": configuration_digest(AgentGate(ordin)), "adapter": configuration}
        ),
        fail_on=ordin.policy.fail_on,
    )
    return replace(ordin, audit=TraceAuditSink(recorder, ordin.audit)), recorder


def read_capture(path: str | Path, *, include_raw: bool = False) -> dict[str, Any]:
    if not isinstance(include_raw, bool):
        raise ValueError("raw inspection requires an explicit boolean")
    target = Path(path)
    if not target.is_file():
        raise ValueError("trace capture does not exist")
    with private_database(target, "trace", readonly=True) as connection:
        mode = connection.execute("SELECT mode FROM capture_meta WHERE id=1").fetchone()
        if mode is None or mode[0] not in {"metadata", "raw_local"}:
            raise ValueError("invalid trace capture mode")
        if connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] > MAX_TRACE_EVENTS:
            raise ValueError("trace capture exceeds event limit")
        records = []
        reviewed: dict[tuple[str, str], dict[str, Any]] = {}
        for seq, session, action, kind, raw in connection.execute(
            "SELECT seq, session_key, action_key, kind, payload FROM events ORDER BY seq"
        ):
            if (
                not isinstance(seq, int)
                or seq < 1
                or not isinstance(raw, str)
                or len(raw.encode()) > MAX_TRACE_EVENT_BYTES
                or not isinstance(session, str)
                or not isinstance(action, str)
                or not _HEX.fullmatch(session)
                or not _HEX.fullmatch(action)
            ):
                raise ValueError("invalid bounded trace event")
            event = _load(raw)
            _validate_event(event)
            if (
                event.get("schema_version") != TRACE_EVENT_SCHEMA_VERSION
                or event.get("event") != kind
            ):
                raise ValueError("invalid trace event schema")
            if mode[0] == "metadata" and "raw_action" in event:
                raise ValueError("metadata capture unexpectedly contains raw action data")
            key = (session, action)
            if kind == "boundary":
                reviewed = {key: value for key, value in reviewed.items() if key[0] != session}
            elif kind == "review":
                reviewed[key] = event
            elif (
                key not in reviewed
                or reviewed[key]["integration"] != event["integration"]
                or reviewed[key]["disposition"] == "deny"
            ):
                raise ValueError("invalid captured observation linkage")
            if not include_raw:
                event.pop("raw_action", None)
            records.append({"sequence": seq, "session_key": session, "action_key": action, **event})
    return {"mode": mode[0], "unsafe_to_share": mode[0] == "raw_local", "events": records}
