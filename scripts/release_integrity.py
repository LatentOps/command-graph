"""Validate release identities and produce bounded, inspectable integrity metadata."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import re
import subprocess
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any


RELEASE_CHECKS = {
    "Quality",
    "Safety benchmark",
    "Build and install package",
    "Linux Debian 12",
    "Linux Fedora 42",
    "macOS 15 Python 3.13",
    "Python 3.10",
    "Python 3.11",
    "Python 3.12",
    "Python 3.13",
    "CodeQL Python",
    "Workflow security",
}


def verify_release_checks(repository: str, revision: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(
        r"[a-f0-9]{40}", revision
    ):
        raise ValueError("invalid release repository/revision")
    pages = json.loads(
        subprocess.check_output(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repository}/commits/{revision}/check-runs?per_page=100",
            ],
            text=True,
        )
    )
    latest = {}
    for page in pages:
        for run in page["check_runs"]:
            name = run["name"]
            if (
                run.get("app", {}).get("slug") == "github-actions"
                and run.get("head_sha") == revision
                and (name not in latest or latest[name]["id"] < run["id"])
            ):
                latest[name] = run
    if any(
        name not in latest
        or latest[name]["status"] != "completed"
        or latest[name]["conclusion"] != "success"
        for name in RELEASE_CHECKS
    ):
        raise ValueError("all maintained CI checks must pass on the exact release commit")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_version(source: bytes) -> str:
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("artifact runtime version is missing")


def validate_distributions(dist: Path, version: str) -> list[Path]:
    wheel = dist / f"ordin-{version}-py3-none-any.whl"
    sdist = dist / f"ordin-{version}.tar.gz"
    if set(dist.glob("*.whl")) != {wheel} or set(dist.glob("*.tar.gz")) != {sdist}:
        raise ValueError("release must contain exactly the expected wheel and source distribution")
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(f"ordin-{version}.dist-info/METADATA")
        runtime = archive.read("ordin/__init__.py")
        parsed = BytesParser().parsebytes(metadata)
        if (
            parsed["Name"] != "ordin"
            or parsed["Version"] != version
            or _runtime_version(runtime) != version
        ):
            raise ValueError("wheel metadata/runtime version mismatch")
    with tarfile.open(sdist, "r:gz") as archive:

        def member(name: str) -> bytes:
            info = archive.getmember(f"ordin-{version}/{name}")
            if not info.isfile() or info.size > 1024 * 1024:
                raise ValueError("invalid source distribution metadata")
            handle = archive.extractfile(info)
            if handle is None:
                raise ValueError("missing source distribution metadata")
            with handle:
                return handle.read(1024 * 1024 + 1)

        parsed = BytesParser().parsebytes(member("PKG-INFO"))
        if (
            parsed["Name"] != "ordin"
            or parsed["Version"] != version
            or _runtime_version(member("ordin/__init__.py")) != version
        ):
            raise ValueError("source distribution metadata/runtime version mismatch")
    return [wheel, sdist]


def create_metadata(dist: Path, *, version: str, revision: str, tag: str) -> None:
    if re.fullmatch(r"[a-f0-9]{40}", revision) is None:
        raise ValueError("release revision must be a full commit SHA")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.dev[0-9]+)?", version):
        raise ValueError("invalid release version")
    assets = validate_distributions(dist, version)
    components = []
    for installed in sorted(
        importlib.metadata.distributions(), key=lambda item: item.metadata.get("Name", "").lower()
    ):
        name = installed.metadata.get("Name", "")
        if name:
            components.append(
                {
                    "type": "library",
                    "name": name,
                    "version": installed.version,
                    "scope": "excluded",
                    "properties": [
                        {
                            "name": "ordin:role",
                            "value": "build-environment; not a claim of bundled runtime dependency",
                        }
                    ],
                }
            )
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "library",
                "name": "ordin",
                "version": version,
                "purl": f"pkg:pypi/ordin@{version}",
            },
            "properties": [
                {"name": "ordin:source-commit", "value": revision},
                {
                    "name": "ordin:scope",
                    "value": "core artifact and observed build environment; optional extras are not installed",
                },
            ],
        },
        "components": components,
    }
    identity = {
        "schema_version": "ordin.release.v1",
        "version": version,
        "revision": revision,
        "tag": tag,
        "artifacts": {asset.name: sha256(asset) for asset in assets},
    }
    for name, payload in [("sbom.cdx.json", sbom), ("release.json", identity)]:
        with (dist / name).open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    paths = sorted([*assets, dist / "sbom.cdx.json", dist / "release.json"])
    with (dist / "SHA256SUMS").open("x", encoding="ascii") as handle:
        handle.write("".join(f"{sha256(path)}  {path.name}\n" for path in paths))


def verify_metadata(dist: Path, *, version: str, revision: str, tag: str) -> dict[str, Any]:
    expected = {
        f"ordin-{version}-py3-none-any.whl",
        f"ordin-{version}.tar.gz",
        "sbom.cdx.json",
        "release.json",
    }
    lines = (dist / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    seen = set()
    for line in lines:
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", line)
        if match is None or match[2] not in expected or match[2] in seen:
            raise ValueError("invalid or duplicate checksum manifest entry")
        seen.add(match[2])
        path = dist / match[2]
        if path.is_symlink() or sha256(path) != match[1]:
            raise ValueError("release artifact checksum mismatch")
    if seen != expected:
        raise ValueError("release checksum manifest is incomplete")
    if (
        {path.name for path in dist.iterdir()}
        - expected
        - {"SHA256SUMS", "provenance.sigstore.json"}
    ):
        raise ValueError("unexpected release assets")
    identity = json.loads((dist / "release.json").read_text())
    if identity.get("schema_version") != "ordin.release.v1" or any(
        identity.get(key) != value
        for key, value in [("version", version), ("revision", revision), ("tag", tag)]
    ):
        raise ValueError("release/source/tag identity mismatch")
    assets = validate_distributions(dist, version)
    if identity.get("artifacts") != {asset.name: sha256(asset) for asset in assets}:
        raise ValueError("release identity artifact digests mismatch")
    return identity


def main() -> None:
    import os
    import tomllib

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["create", "verify"])
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--revision", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    if _runtime_version(Path("ordin/__init__.py").read_bytes()) != version:
        raise ValueError("source package/runtime version mismatch")
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if actual != args.revision:
        raise ValueError("checked-out source differs from requested release revision")
    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        raise ValueError("release integrity requires a clean source checkout")
    if args.publish:
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or args.tag != f"v{version}":
            raise ValueError("published tags require exactly vX.Y.Z matching the package")
        subprocess.run(["git", "merge-base", "--is-ancestor", actual, "origin/main"], check=True)
        refs = subprocess.check_output(
            [
                "git",
                "ls-remote",
                "--exit-code",
                "origin",
                f"refs/tags/{args.tag}",
                f"refs/tags/{args.tag}^{{}}",
            ],
            text=True,
        ).splitlines()
        if not refs or refs[-1].split()[0] != actual:
            raise ValueError("remote release tag no longer identifies the tested commit")
        if (
            os.environ.get("GITHUB_EVENT_NAME") != "push"
            or os.environ.get("GITHUB_REF_TYPE") != "tag"
        ):
            raise ValueError("publishing requires a tag push event")
        verify_release_checks(os.environ["GITHUB_REPOSITORY"], actual)
    if args.mode == "create":
        create_metadata(args.dist, version=version, revision=actual, tag=args.tag)
    print(
        json.dumps(
            verify_metadata(args.dist, version=version, revision=actual, tag=args.tag),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
