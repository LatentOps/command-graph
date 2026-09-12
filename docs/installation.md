# Installation

Ordin is distributed directly from GitHub. The current development version
installs `ordin`, `ordin-claude-hook`, and `ordin-mcp-proxy`.

The root README describes `main` (`0.2.0.dev0`). Use the development install below
for its current APIs and integrations. The older `v0.1.0` tag predates the Claude
Code hook and MCP proxy; use that tag's documentation when installing it.

## Install the current stable release

The current stable release is `v0.1.0`. Install it directly from the immutable Git tag:

```bash
python -m pip install "git+https://github.com/LatentOps/ordin.git@v0.1.0"
```

You can also install the validated wheel attached to the GitHub release:

```bash
python -m pip install https://github.com/LatentOps/ordin/releases/download/v0.1.0/ordin-0.1.0-py3-none-any.whl
```

Then verify the installation:

```bash
ordin --help
ordin doctor
ordin make file runnable
```

The release also includes a source distribution for environments that prefer to build locally.

## Install the current development tree

`main` is development code and currently identifies itself as `0.2.0.dev0`.

Install it directly from GitHub:

```bash
python -m pip install "git+https://github.com/LatentOps/ordin.git"
```

Or clone the repository:

```bash
git clone https://github.com/LatentOps/ordin.git
cd ordin
python -m pip install .
```

## Optional semantic reranking

The deterministic search and safety paths have no ML runtime dependency. For semantic support, install from a checkout with the optional extra:

```bash
git clone https://github.com/LatentOps/ordin.git
cd ordin
python -m pip install ".[semantic]"
```

A semantic model must be supplied from an explicit local path. Ordin does not automatically download one.

## Development setup

```bash
git clone https://github.com/LatentOps/ordin.git
cd ordin
python -m pip install -e ".[dev]"
pre-commit install
```

Run the shared local/CI quality gate with:

```bash
pre-commit run --all-files
```

Run the full behavioral test suite separately:

```bash
pytest -q
```

The pre-commit gate covers Ruff lint and formatting, staged mypy checks, Python compilation, `ordin doctor`, and repository namespace integrity. CI executes the same pre-commit configuration before its Python-version, package, and Linux compatibility jobs.

## Supported Python and Linux validation

Ordin supports Python 3.10 through 3.13. CI tests every supported Python version and also performs isolated installed-CLI smoke tests on Debian and Fedora in addition to the standard Ubuntu runner.
