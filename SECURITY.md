# Security policy

Report safety-sensitive vulnerabilities privately through
[GitHub private security reporting](https://github.com/LatentOps/ordin/security/advisories/new).
It is enabled for this repository. If that page is unavailable, open a public
issue asking only for a private contact method, without exploit details.

The latest `0.2.x` release is supported for security fixes. Development toward
`0.3` is reviewed on `main`; development builds are not stable releases. Older
release lines are unsupported. Security fixes may require upgrading to a new
patch version; published artifacts are never replaced in place.

Include the Ordin version/commit, platform, integration, relevant redacted
configuration, expected decision, actual outcome, and a minimal offline
reproduction. Distinguish a review bypass from host execution or approval
misconfiguration. Report reproducible false allows, protocol ambiguity,
cross-session evidence, credential disclosure, or release-integrity failures
through the private channel first.

Do not put live secrets, working production exploits, private traces, or
dangerous production details in public issues. Use synthetic resources and
revoke any credentials already exposed. Coordinate disclosure and a regression
fixture with maintainers; response times are best effort.

See the [threat model](docs/threat-model.md) for boundaries and limitations and
the [release verification guide](docs/releasing.md) for artifact checks.
