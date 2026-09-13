# v0.3 publication and next development cycle

Release [v0.3.0](https://github.com/LatentOps/ordin/releases/tag/v0.3.0) is immutable
at `559f55e8974ddc91c11ca3582cfc378d2fb429c7`. The
[verification receipt](https://github.com/LatentOps/ordin/pull/149) records exact
CI/manual/tag runs, downloaded checksums/SBOM/provenance, native release
attestation, a published-wheel Linux quickstart and a clean public-tag install.

`main` advances to `0.4.0.dev0`; the lifecycle guard records `0.3.0` as published.
Stable install instructions continue to use v0.3.0. Its tag, assets, release
notes and v0.3 compatibility inventory remain historical. New additive exports
may extend that inventory in the next line but must preserve its supported
baseline.

## Evidence informing the next roadmap

| Evidence | Focused follow-up |
| --- | --- |
| Cursor documents generic/file/MCP hooks, but generic `ask` is not enforced and cloud lifecycle coverage differs | [#138](https://github.com/LatentOps/ordin/issues/138): native/compatibility integration with conservative outputs and explicit identity/host limits |
| The release exposed documentation and setup friction, while no customer telemetry is collected | [#139](https://github.com/LatentOps/ordin/issues/139): privacy-first reports and sanitized failure intake |
| Maintainer/CI failures in response correlation, history pressure, HTTP deadlines, context and validation have reproducible regressions | [#140](https://github.com/LatentOps/ordin/issues/140): a versioned corpus with honest source provenance and benign controls |
| Current onboarding still requires manual hook JSON, private state paths and explicit MCP review/pinning steps | [#141](https://github.com/LatentOps/ordin/issues/141): previewable, reversible setup with bounded diagnostics |

These existing issues form the next roadmap; no speculative analyzer backlog,
hosted service or automatic telemetry is added. Real-user counts are currently
unmeasured. Maintainer/CI-derived fixtures must not be presented as production
traces or prevalence estimates.

## Release review

The candidate gate found and corrected source-shadowed runtime evaluation and
a mistaken regression-runner reference. The final release repeated the full
matrix and artifact gate rather than relying on earlier development measurements.
Private local verification needed explicit access to the Git metadata/Sigstore
cache, but no production credentials or model requests were required.

Known limits remain: finite command/trajectory coverage, optional model-dependent
reranking, Linux-specific command metadata, host approval/timeout behavior,
lexical path checks, and caller-owned evidence. Future performance work should
use the recorded core/boundary distributions, without relaxing safety thresholds
to meet a timing target. Continue the existing dependency/security maintenance
process and keep the v0.3 release immutable.
