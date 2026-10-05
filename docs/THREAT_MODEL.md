# Threat model

**Asset:** the meaning of an evaluation number, and the routing decisions built on it.

**Trust boundary:** Astrometron trusts the Python process it runs in and the code the
user configures (solvers, scorers, tools, providers). It does not trust the dataset
file, model outputs, the judge's output, receipts read back from disk, or the evidence
store's contents.

V1 has no cryptographic signatures. Integrity comes from digests that are re-derived on
every read, an index-bound receipt digest, and dataset/selection bindings. An attacker
who can rewrite every file *and* the index can forge history. Signing is on the roadmap.

| Threat | Status | Mechanism / limit |
|---|---|---|
| **Dataset tampering** (editing the data file between runs) | MITIGATED | Content revision; `dataset.revision` pin refuses mismatched data; staleness reports a changed source; `datasets verify` flags unregistered content. |
| Dataset tampering in the store (editing a snapshot) | MITIGATED | Objects are content-addressed and re-hashed on read; the snapshot's revision is recomputed. |
| **Result deletion** (dropping failed rows) | MITIGATED | Accounting identity re-derived from records; selection digest; selection re-derived from the snapshot; index-bound receipt digest (selfcheck A1–A5). |
| Inflated headline number | MITIGATED | Aggregate recomputed from per-case records on every verification (A6). |
| Cherry-picked exclusions | MITIGATED (routing) / visible (reports) | Every exclusion needs a reason, and the denominator names it. The router rejects evidence with exclusions above `max_excluded_fraction` (default 0) (R5). Comparisons with different exclusions are PARTIAL. |
| **Scorer manipulation** (changing scorer code or params) | MITIGATED | Scorer identity includes a code digest and a Python function digest; `reproduce` reports INPUT_MISMATCH; `compare` reports PARTIALLY_COMPARABLE (C2, C3). Helper modules imported by the scorer are covered only by git commit/dirty: **PARTIAL** for that sub-case. |
| **Judge substitution** | MITIGATED | Judge identity and prompt digest in the scorer identity; comparability warning (C1). |
| Judge failure hidden as a score of 0 | MITIGATED | JUDGE_ERROR; verifier requires parsed judge evidence on judge-scored verdicts (J1, J2). |
| **Provider / model drift** | PARTIAL | `served_model` recorded and substitution warned (D2), and reproduce compares served models; the endpoint is part of provider identity, so evidence from one deployment is not used for another (R6); python providers are pinned only on request; the router rejects revision drift (R1). Hosted models behind a stable name can change silently, and Astrometron only sees what the provider reports. |
| Model identity edited in a receipt | MITIGATED | Receipt digest + index digest (D1); reproduction refused. |
| **Stale evidence** used for routing | MITIGATED | Age gate, revision-drift gate, ROUTING_EVIDENCE_STALE (R1). |
| Stale receipt (config/data changed afterwards) | MITIGATED | `verify` → STALE (H1, H2). |
| **Manifest tampering**, partial (file only) | MITIGATED | Receipt digest, then the index digest if the attacker reseals the file (A3, B3). |
| Index-only tampering (editing `index.sqlite` columns such as task, model, pass rate) | MITIGATED | Every indexed column is re-derived from the receipt in `verify_run`. The router and `compare` use only fully verified runs (A7, C4). |
| Manifest tampering with full write access to the store and index | OUT OF SCOPE (V1) | Needs signatures or an external transparency log. |
| **Corrupted local store** | MITIGATED | Objects re-hashed on read; foreign or newer SQLite schema refused; RUNNING runs never treated as evidence (H3); atomic writes with fsync. |
| **Malicious evaluation case** (e.g. a huge input, or a case designed to crash the harness) | PARTIAL | A crash becomes a recorded HARNESS_ERROR or TIMEOUT, never a silent drop. No resource limits beyond `timeout_s`. A timed-out thread cannot be killed and keeps running in the background. |
| **Prompt injection into judge input** | PARTIAL | The candidate answer is fenced between per-case markers; the verdict must be the last line and appear exactly once, so an echoed injected verdict becomes JUDGE_ERROR rather than PASS. A judge that obeys injected instructions and emits one clean verdict is not detectable by parsing. Use deterministic scorers where possible. |
| **Compromised runner** (malicious solver/scorer code, or a modified Astrometron) | OUT OF SCOPE | Configured code runs with full privileges and can write any receipt it wants. Mitigation is code review and committing eval code (`git_commit` / `git_dirty` are recorded). |
| **Compromised external provider** (returns crafted outputs or false usage/cost) | PARTIAL | Outputs are evidence, not trusted; malformed responses are MODEL_ERROR; provider-reported cost is labelled `cost_basis: provider`. A provider lying consistently is indistinguishable from an honest one. |
| Credential leakage | MITIGATED | Literal keys refused in config and routing policies; keys read from env by name only; never written to receipts or logs (tested). |
| Sensitive data in receipts | PARTIAL | `record_outputs: false` keeps only digests of outputs. Dataset snapshots necessarily contain inputs; the store is local and should be treated as sensitive as the dataset. |

## Out of scope for V1

- Signing receipts.
- Multi-user access control.
- Remote stores.
- Sandboxing user code.
- Protecting against a malicious Python interpreter.
