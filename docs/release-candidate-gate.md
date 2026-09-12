# Exact release-candidate acceptance

Complete the dependency baseline, public contract audit and onboarding refresh
before choosing a candidate. The candidate is a full commit SHA, never a moving
branch name. Known correctness or security failures stop this gate.

The release workflow builds wheel/source archives with the hashed toolchain,
validates metadata, installs the wheel in isolation, and runs
`scripts/run_release_candidate.py`. It refuses dirty/mismatched source, editable
imports, version drift, unexpected core dependencies and reused output folders.
It runs all maintained safety, trajectory, failure/extended regression,
captured conformance, live-session/integration, executable runtime and wheel
quickstart workloads. Existing runners retain their safety and latency limits;
there is no skip or relaxed-threshold mode.

The `ordin-candidate-<SHA>` artifact contains `candidate.json`/`candidate.md`,
exact source and wheel/builder environments, tool versions, workload exits and
timings, hashes, detailed JSON reports, integration/runtime Markdown and logs.
Those reports hold workload/decision/error counts and latency distributions.
They describe finite local fixtures, not production prevalence or complete
model/agent latency. Failures retain available logs and prevent attestation.

Reports live outside the checkout so integrity validation still sees clean
source. They are separate from the fixed distribution bundle, whose checksums,
SBOM and provenance are independently generated and verified.

## Acceptance sequence

1. Merge the gate PR only after all 15 checks pass on its exact head.
2. Choose its squash commit on `main` as candidate `S`; wait for the applicable
   main CI/security/platform matrix to pass independently.
3. Run `gh workflow run release.yml --ref main` while `main` still identifies
   `S`. Verify the run's `head_sha == S`; otherwise select the new SHA and repeat.
   Manual validation cannot publish a release.
4. Require successful build, candidate workloads, checksums/SBOM, and attestation
   verification. Dependency review applies to the PR; its skipped main job is
   explicitly inapplicable, not a waived failure.
5. Record `S`, CI/manual run links and artifact IDs in the issue/PR receipt.
   An external receipt avoids a self-referential commit hash; the artifact
   contains the same exact `S`.

The workload report does not claim that other concurrent CI jobs passed. Check
GitHub after all jobs finish. If any gate fails, fix through a focused PR with
regressions, select a new candidate and rerun the complete sequence.

The final-version issue then changes the version to `0.3.0`, reruns CI and manual
validation, and tags only the independently green final squash commit.
Candidate acceptance does not publish or permit replacement of a release.
See [releasing](releasing.md).
