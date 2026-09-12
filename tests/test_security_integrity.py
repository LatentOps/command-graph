import io
import json
import os
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ordin import ActionEnvelope, JsonlAuditSink, Ordin, verify_audit_jsonl
from ordin.regression_replay import load_failure_regressions, run_failure_regressions
from scripts.check_workflow_security import workflow_errors
from scripts.release_integrity import (
    RELEASE_CHECKS,
    create_metadata,
    verify_metadata,
    verify_release_checks,
)


def distributions(path, *, runtime="1.2.3"):
    metadata = b"Metadata-Version: 2.1\nName: ordin\nVersion: 1.2.3\n"
    source = f'__version__ = "{runtime}"\n'.encode()
    with zipfile.ZipFile(path / "ordin-1.2.3-py3-none-any.whl", "w") as archive:
        archive.writestr("ordin-1.2.3.dist-info/METADATA", metadata)
        archive.writestr("ordin/__init__.py", source)
    with tarfile.open(path / "ordin-1.2.3.tar.gz", "w:gz") as archive:
        for name, content in [("PKG-INFO", metadata), ("ordin/__init__.py", source)]:
            info = tarfile.TarInfo("ordin-1.2.3/" + name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def test_release_integrity_binds_both_formats_and_build_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.release_integrity.importlib.metadata.distributions", lambda: [])
    distributions(tmp_path)
    kwargs = dict(version="1.2.3", revision="a" * 40, tag="v1.2.3")
    create_metadata(tmp_path, **kwargs)
    assert verify_metadata(tmp_path, **kwargs)["revision"] == "a" * 40
    sbom = json.loads((tmp_path / "sbom.cdx.json").read_text())
    assert sbom["bomFormat"] == "CycloneDX" and sbom["metadata"]["component"]["version"] == "1.2.3"
    with pytest.raises(ValueError, match="identity mismatch"):
        verify_metadata(tmp_path, **{**kwargs, "revision": "b" * 40})
    wheel = tmp_path / "ordin-1.2.3-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        verify_metadata(tmp_path, **kwargs)


def test_release_rejects_runtime_version_mismatch(tmp_path):
    distributions(tmp_path, runtime="9.9.9")
    with pytest.raises(ValueError, match="version mismatch"):
        create_metadata(tmp_path, version="1.2.3", revision="a" * 40, tag="v1.2.3")
    assert not (tmp_path / "SHA256SUMS").exists()


def test_release_checks_reject_stale_failed_or_missing_check_heads(monkeypatch):
    runs = [
        {
            "name": name,
            "id": index,
            "status": "completed",
            "conclusion": "success",
            "head_sha": "a" * 40,
            "app": {"slug": "github-actions"},
        }
        for index, name in enumerate(sorted(RELEASE_CHECKS))
    ]
    monkeypatch.setattr(
        "scripts.release_integrity.subprocess.check_output",
        lambda *a, **k: json.dumps([{"check_runs": runs}]),
    )
    verify_release_checks("LatentOps/ordin", "a" * 40)
    runs.append({**runs[0], "id": 100, "status": "in_progress", "conclusion": None})
    with pytest.raises(ValueError, match="exact release commit"):
        verify_release_checks("LatentOps/ordin", "a" * 40)


def test_workflow_guard_rejects_floating_actions_and_persisted_credentials(tmp_path):
    path = tmp_path / "unsafe.yml"
    path.write_text(
        "permissions:\n  contents: read\njobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n"
    )
    errors = workflow_errors(path)
    assert any("immutable" in error for error in errors)
    assert any("credentials" in error for error in errors)
    root = Path(__file__).resolve().parents[1]
    assert not [
        error
        for workflow in (root / ".github/workflows").glob("*.yml")
        for error in workflow_errors(workflow)
    ]


def test_audit_independent_concurrent_writers_serialize_one_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    review = Ordin().review_action(ActionEnvelope.shell("git status --short"))
    sinks = [JsonlAuditSink(path, hash_chain=True, fsync=False) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda index: sinks[index % 4].record(review), range(24)))
    result = verify_audit_jsonl(path, require_hash_chain=True)
    assert result.ok and result.event_count == 24


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":2}\n',
        b'{"x":NaN}\n',
        b"[" * 1000 + b"]" * 1000 + b"\n",
    ],
)
def test_audit_rejects_ambiguous_nonfinite_and_deep_json(tmp_path, raw):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(raw)
    assert not verify_audit_jsonl(path, require_hash_chain=True).ok


def test_audit_refuses_append_after_external_tampering_and_bounds_reads(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path, hash_chain=True)
    review = Ordin().review_action(ActionEnvelope.shell("git status --short"))
    sink.record(review)
    raw = path.read_bytes().replace(b'"risk":"low"', b'"risk":"high"')
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="invalid"):
        sink.record(review)
    assert path.read_bytes() == raw
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    assert not verify_audit_jsonl(path).ok


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode and symlink controls")
def test_audit_rejects_public_modes_and_symlinks(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.touch(mode=0o644)
    review = Ordin().review_action(ActionEnvelope.shell("git status --short"))
    with pytest.raises(ValueError, match="owner-only"):
        JsonlAuditSink(path).record(review)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises((OSError, ValueError)):
        JsonlAuditSink(alias).record(review)


def test_security_findings_are_permanent_failure_regression_controls():
    path = Path(__file__).resolve().parents[1] / "benchmarks/failure_regressions.jsonl"
    cases = [case for case in load_failure_regressions(path) if case.audit_control]
    assert {case.audit_control for case in cases} == {
        "interleaved_writers",
        "ambiguous_json",
        "tail_checkpoint",
    }
    report = run_failure_regressions(cases)
    assert report.matches == len(cases), report.regression_errors()
