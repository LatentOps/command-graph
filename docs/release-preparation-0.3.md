# v0.3 release preparation

## Reviewed dependency baseline

All five initial Dependabot updates were independently merged with all 15
required checks passing on their exact heads. The machine-readable
[review record](release-evidence/dependencies-0.3.json) retains those heads,
squash commits, and check outcomes. No update was deferred.

| Update | Final constraint | Compatibility decision |
| --- | --- | --- |
| [#127](https://github.com/LatentOps/ordin/pull/127) | `sentence-transformers>=6.0.1,<7` | Optional only; explicit local model path and `local_files_only=True` remain required. |
| [#128](https://github.com/LatentOps/ordin/pull/128) | `mypy>=2.3.1,<3` | All maintained typed boundaries pass; no coverage or strictness was removed. |
| [#129](https://github.com/LatentOps/ordin/pull/129) | `pre-commit>=4.6.2,<5` | Local system-language hooks keep the same checks and run in CI. |
| [#130](https://github.com/LatentOps/ordin/pull/130) | `ruff>=0.16.6,<1` | Existing selected rules, Python target, and formatting policy remain unchanged. |
| [#131](https://github.com/LatentOps/ordin/pull/131) | `pytest>=9.1.1,<10` | Full Python/platform suites pass, including parametrized fixtures. |

Sentence Transformers 6 requires newer PyTorch/Transformers versions; these
remain outside the deterministic core. Ordin uses the documented
[`SentenceTransformer` constructor and `encode` API](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html),
not the new multi-vector or training paths. The [6.0.1 release](https://github.com/huggingface/sentence-transformers/releases/tag/v6.0.1)
changes those additional paths without removing the local single-vector API.
Loading checks cover the explicit offline flag. Model quality, GPU compatibility,
and arbitrary saved models are not certified by the core test matrix.

The review also covers [mypy 2's local partial type/config discovery changes](https://mypy.readthedocs.io/en/stable/changelog.html),
[pre-commit's backend fixes](https://github.com/pre-commit/pre-commit/releases/tag/v4.6.2),
[Ruff's rule/formatter changes](https://github.com/astral-sh/ruff/releases/tag/0.16.6),
and [pytest's parametrization fixes](https://github.com/pytest-dev/pytest/releases/tag/9.1.1).
No safety or latency threshold changed. Twine's development constraint is
aligned with the already-tested release toolchain (`>=7,<8`).

A compatibility check found that NaN similarity scores could be clamped to a
confident ranking value. Non-finite scores now fail validation; finite scores
retain the existing bounded influence. The core still declares zero required
dependencies and never imports an ML backend during deterministic review.

## Reproduce the development environment

`requirements/dev-baseline.txt` records universal, hashed development and
release-tool versions for Python 3.10 and newer. It excludes the optional ML
stack. The supported matrix remains Python 3.10–3.13 on Linux and Python 3.13
on macOS; universal resolution is not a claim of extra platform certification.

```bash
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/dev-baseline.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/pre-commit run --all-files
.venv/bin/python -m pytest -q
```

Regenerate deliberately with `uv pip compile pyproject.toml requirements/release.txt
--extra dev --universal --python-version 3.10 --generate-hashes --output-file
requirements/dev-baseline.txt`. Review lock changes with the declarations;
do not update the candidate environment silently. Release tooling is also
pinned independently in `requirements/release.txt`.

Subsequent sections and evidence records identify the audited API, documented
quickstarts, exact release candidate, and final release commit as those gates
complete. Dependency approval alone is not release approval.

The [public compatibility audit](public-compatibility-0.3.md) and
[installed-wheel quickstarts](quickstart.md) are the next completed prerequisites.
Follow the [exact candidate gate](release-candidate-gate.md) to produce the
release-facing evaluation snapshot and record the accepted SHA.
