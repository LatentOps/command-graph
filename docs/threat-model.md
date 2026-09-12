# Ordin threat model

Ordin reviews proposed actions and supplies integration decisions. It is not a
sandbox, credential broker, authorization server, or proof that a tool's
implementation is safe. An `allow` means the supplied evidence passed the
configured review; the execution host must still enforce permissions.

## Trust boundaries

Agent/model output, proposed tool arguments, MCP client requests, upstream
responses, and discovery descriptions are untrusted. Parser limits, exact
identities, and data-only schemas reduce what these inputs can influence.
Discovery supplies contracts to review; it never grants semantics by itself.

The host must protect Ordin's installation/configuration, policy and semantics
files, reviewed contract pins, shell startup environment, filesystem controls,
and release-verification trust roots. Caller context, session/history state,
and post-action observations are trusted evidence only when the caller obtains
and isolates them correctly. A tool can lie about its effects; typed observation
fields are not independent measurement. Integration IDs correlate evidence but
do not authenticate the process supplying it.

Release trust includes the repository maintainers, protected source history,
GitHub Actions platform, selected action commits, Python build dependencies,
and artifact attestation identity. Signed provenance identifies a build; it
does not establish that all those components were uncompromised.

## Threat coverage

| Threat | Coverage and residual responsibility |
| --- | --- |
| Agent proposes destructive actions | **Prevents known blocked proposals** through correctly wired gates; unknown semantics generally require approval. Coverage is conservative and incomplete. |
| Prompt/tool injection | **Reduces** impact by reviewing resulting actions. It does not solve prompt injection or understand every malicious instruction. |
| Trusted identity changes contract | **Detects** drift when reviewed pins and correlated complete discovery are enabled. A server can preserve a schema while changing its implementation. |
| Agent modifies policy/semantics/configuration | **Does not solve** same-user configuration tampering. Protect files outside the agent's write scope; review changes and configuration digests. |
| Review/execution TOCTOU | **Does not solve** changes after review. The host must bind the reviewed action to actual execution and enforce resource access at use time. |
| Symlink or path target changes | **Reduces** accidental aliasing in private stores and retains literal policy identities. Lexical path matching is not filesystem isolation. |
| Malicious upstream MCP result | **Reduces** protocol abuse with bounded parsing, correlation, deadlines, and session isolation. It cannot establish truthful output or undo server-side effects. |
| Malformed/ambiguous protocol | **Prevents** rejected framing and duplicate/ambiguous JSON at strict MCP boundaries. Host hook formats have their separately documented contracts. |
| Cross-session contamination | **Prevents** mismatched retained identities/configurations through maintained session APIs; **does not solve** forged evidence from a compromised trusted caller. |
| Approval bypass through bad installation | **Does not solve** bypassed hooks or unguarded execution paths. Codex unresolved pre-tool decisions deny; host hooks must be installed and trusted. Normal shell Enter remains unwrapped. |
| Secrets in logs, audit, or diagnostics | **Reduces** exposure through default redaction and opt-in capture. Policy labels and explicitly included fields may still be private; raw capture is unsafe to share. |
| Compromised action/build dependency | **Reduces** exposure with immutable action pins, minimal permissions, dependency review, CodeQL, reviewed updates, checksums and provenance. These are not a proof of safe source. |
| Local audit modification/reordering | **Detects** inconsistent hashes, linkage, ambiguous JSON and incomplete records during verification. Trusted external checkpoints detect loss of the recorded tail. |
| Whole-file replacement or forged audit history | **Does not solve** an attacker rewriting a complete chain and any colocated checkpoint. No trusted external timestamp or signer is implied. |

## Local evidence

`JsonlAuditSink(hash_chain=True)` uses a private regular file and an advisory
writer lock. Cooperating instances/processes read the current chain head while
holding that lock. An unchanged file can reuse its verified head; changed file
metadata causes revalidation. Locks do not constrain a malicious process that
ignores them. The host must provide a trusted local directory; shared/network
filesystems with different lock semantics are outside this guarantee.

The verifier bounds file/line sizes and rejects duplicate keys, non-finite
values, excessive nesting, broken links, altered event hashes, and incomplete
lines. It verifies internal consistency, not truthfulness of decisions or
timestamps. An empty chain or a valid truncated prefix can be internally valid.
Retain the last hash outside the writer's control and pass
`expected_last_hash=...` to detect divergence from that checkpoint.

Audit and capture stores are separate from session-state transactions. A
persisted proposal is not proof that the host executed it. See
[trace capture](trace-capture.md) and [audit evidence](audit-and-provenance.md).

## Repository and release controls

- `main` requires passing CI and PR merges; force pushes and deletion are disabled.
- Workflow actions are pinned to full commit SHAs with version comments;
  checkout does not persist credentials. A permanent guard rejects floating
  actions, broad defaults, privileged PR triggers, and inherited secrets.
- CodeQL analyzes Python on PRs, main, and a schedule. Dependency review checks
  newly introduced vulnerabilities in runtime/development dependencies.
  Dependabot maintains actions, package declarations, and release-tool pins.
- Private vulnerability reporting, security updates, secret scanning, and push
  protection are enabled through GitHub repository settings. These settings
  are not controlled by YAML; maintainers should recheck them after transfers
  or organization policy changes. Provider validity checks are not enabled.
- PR builds only have read permissions. Tag/manual attestation and tag-only
  publishing jobs receive their specific signing or release permissions. Publication downloads
  the already tested artifact by immutable artifact ID and never rebuilds it.

The workflow guard and protected-branch review are the repository posture
checks here. A separate Scorecard publishing action is omitted: it would add
another workflow/token surface without replacing these explicit checks or the
deterministic safety benchmark. Dependency review may display upstream
Scorecard signals; they are advisory.

Checksums detect changed bytes. GitHub attestations bind the checksums and
distribution assets to the workflow/source identity. The release workflow
checks version/tag/source equality, verifies the remote tag again before
publication, and refuses to replace an existing release. Maintainers still
must protect tags/accounts and review dependency updates. See
[releasing](releasing.md) for verification commands and the SBOM's scope.
