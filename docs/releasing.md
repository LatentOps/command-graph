# Releasing Ordin

Ordin releases are distributed through GitHub for now. A release consists of an immutable version tag plus validated wheel and source-distribution assets attached to the corresponding GitHub Release.

No package-index account, publishing token, or external registry is required.

## Version lifecycle

Published package versions are immutable identifiers. Never build new source under a version that has already been released.

The expected lifecycle is:

1. normal development uses the next PEP 440 development version, such as `0.2.0.dev0`;
2. a release preparation PR changes both version declarations to the exact final version, such as `0.2.0`;
3. the full merge gate passes and the version PR is merged to `main`;
4. the matching `v0.2.0` tag is created;
5. the release workflow validates, builds, smoke-tests, and attaches the exact artifacts to a GitHub Release;
6. immediately after the release, `main` advances to the next unique development version, such as `0.3.0.dev0`.

A published version must never be reused for different source contents.

Update the version in both:

- `pyproject.toml` -> `project.version`
- `ordin/__init__.py` -> `__version__`

Tests require the installed distribution metadata and runtime version to match.

## Preparing a final release

Change the development version to the exact final version in both version declarations, then run the normal local gate:

```bash
pre-commit run --all-files
pytest -q
python -m build
python -m twine check dist/*
```

Open a focused release-version PR and merge only after the complete CI matrix passes.

Then create the matching tag. For a final version `0.2.0`:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The release workflow refuses a tag that does not exactly equal `v<project.version>` and refuses a mismatch between distribution and runtime versions. Published tags must use a final `X.Y.Z` version; development versions can be validated through `workflow_dispatch` without publishing.

## Release workflow

Every `v*` tag:

1. validates version/tag consistency;
2. builds wheel and source distribution;
3. runs Twine metadata validation;
4. installs the built wheel into a fresh virtual environment;
5. runs installed `ordin doctor`, search, isolated Python API/version checks, adapter conformance, a Claude Code hook request, and the MCP entry point;
6. uploads the validated distributions as a workflow artifact;
7. creates the matching GitHub Release and attaches the exact validated wheel and source distribution.

The workflow refuses to replace an already-existing GitHub Release under the same tag. Published release assets are treated as immutable.

`workflow_dispatch` can exercise build and validation without creating a release.

## Installing releases

Users can install a stable release directly from its Git tag:

```bash
python -m pip install "git+https://github.com/LatentOps/ordin.git@v0.2.0"
```

They can also install the wheel attached to the GitHub Release.

For the current development tree:

```bash
python -m pip install "git+https://github.com/LatentOps/ordin.git"
```

## After publishing

Treat the released tag and artifacts as immutable. Do not replace assets with different package contents under the same version. Advance `main` to the next development version in a new PR before resuming feature work.

## Failure policy

Do not publish artifacts from a failed build by hand. Fix the source or release configuration, rerun the merge gate, and use a new version if an artifact with the previous version was already published.
