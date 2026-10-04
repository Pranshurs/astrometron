# Independent review log

## Review 1: cold review, 2026-10-04

**Reviewer.** A separately spawned agent with no prior project context. It had read-only
access to the repository and wrote scratch reproductions outside it. It never edited the
code: repairs were made by the maintainer only.

**Scope.** Commit `33d8cf6`, covering:

- denominator invariants;
- dataset immutability;
- receipt binding;
- failure taxonomy;
- reproduction;
- comparison semantics;
- routing evidence.

**Result.** 0 BLOCKER, 3 HIGH, 6 MEDIUM and several LOW findings. Every HIGH and MEDIUM
finding was reproduced independently by the maintainer, by re-running the reviewer's
scripts, before any repair.

| # | Severity | Finding | Disposition | Guard |
|---|---|---|---|---|
| 1 | HIGH | Index columns (task, model, pass rate…) were not checked against the receipt. An index-only edit could make a run evidence for another task in the router. | **Fixed.** `verify_run` re-derives every indexed column from the receipt. | Scenario A7, test, mutant M49 |
| 2 | HIGH | `compare` used only the self-verifying receipt check, so a re-sealed forged file got a comparability label. | **Fixed.** `compare_runs` requires full store verification, and the CLI uses it. | C4, M51 |
| 3 | HIGH | One non-finite value (NaN cost or score) aborted the whole run at seal time. | **Fixed.** Each case record is validated, and a failure becomes HARNESS_ERROR (`stage: record`) for that case only. The HTTP adapter drops non-finite costs. | E3, M52 |
| 4 | MEDIUM | `reproduce` said REPRODUCED for runs that scored nothing (all outages). Python providers were pinned by default. | **Fixed.** New `NO_SCORED_CASES` verdict; python providers unpinned unless declared; served models compared; `--repeats N`. | I2, M55, M61, M63 |
| 5 | MEDIUM | An INCOMPLETE run could be STRONGLY_COMPARABLE, and CANCELLED or outage cases counted as regressions. | **Fixed.** INCOMPLETE caps the label at PARTIAL; paired statistics use only cases scored in both runs. | C5, M56, M57 |
| 6 | MEDIUM | Cherry-picked exclusions could inflate router evidence, and excluded cases counted toward `min_cases`. | **Fixed.** Exclusions are part of the routing basis, `max_excluded_fraction` defaults to 0, and `min_cases` uses the headline denominator. | R5, M58 |
| 7 | MEDIUM | A solver that swallowed a tool failure got PASS or TASK_FAILURE. | **Fixed, with a stated rule.** A failed tool with no later successful model call → TOOL_ERROR (shared rule used by runner and verifier). If the model answered *after* the failure, its verdict stands (working around a broken tool is agent behaviour) but the case is marked `degraded`. | G2, M53, M54, M65 |
| 8 | MEDIUM | `openai-compat` identity ignored `base_url`, so evidence from one deployment qualified another. | **Fixed.** A credential-free endpoint is part of provider identity; the router reports DIFFERENT_DEPLOYMENT. | R6, test, M59 |
| 8b | MEDIUM (reasoned) | With no `max_evidence_age_days`, evidence of any age was eligible. | **Fixed.** Default is 30 days; `null` disables it explicitly. | test, M60 |
| 9 | MEDIUM | An invalid row's reason embedded the file name and line number, so its digest depended on path and order. Unmapped reserved columns were not hashed. | **Fixed.** The reason carries no location; every column not consumed as a field goes into `metadata`. | tests, M62 |
| L1 | LOW | A timed-out case's thread keeps running after the TIMEOUT record is written. | **Documented** (THREAT_MODEL: Python threads cannot be killed). The record is correct. | – |
| L2 | LOW | The router does not notice that the on-disk dataset changed since the evidence, unless the policy pins `dataset_revision`. | **Documented** (ROUTING). Pin the revision to bind routing to current data. | – |
| L3 | LOW (reasoned) | `IncompleteRead` became HARNESS_ERROR; an HTML proxy page became MODEL_ERROR. | **Fixed.** Both are now INFRA_ERROR. | provider tests |
| L4 | LOW (reasoned) | The numeric scorer accepted `float()` quirks (`1_0`, Unicode digits, `nan`). | **Fixed.** Strict ASCII numeric grammar. | test, M64 |
| L5 | LOW (reasoned) | Run-id prefix lookup used an unescaped `LIKE`. | **Fixed.** Literal prefix comparison. | test |
| – | Note | STRONGLY_COMPARABLE ignores prompt/params/solver differences. | **By design**: these are treatments. They are listed in `changed_treatments` and warned about when more than one changed. | – |

**Bounded re-check by the same reviewer (HEAD `b36b10e`).** The reviewer re-ran its
original reproductions plus a new script (`r8.py`, which the maintainer re-ran
independently with matching output).

- Every original HIGH and MEDIUM finding is RESOLVED.
- No CLAIM-INVALIDATING counterexample was found.
- Items marked UNVERIFIED in the re-check (8b, L3–L5) are covered by the maintainer's
  tests and mutants M60 and M64.

Two NEW-OBSERVATIONS, handled as a separate change:

- The router ignored tool-`degraded` cases. Evidence now reports
  `scored_after_tool_failure`, and the optional `max_degraded_fraction` gate rejects it
  above a limit (`DEGRADED_EVIDENCE`; test, mutant M67).
- The router's reliability gate did not count evaluation-side errors, so evidence that
  was all HARNESS_ERROR relied on the quality floor. New `BROKEN_EVALUATION` gate:
  (HARNESS + JUDGE + INVALID_CASE) / requested must be ≤ `max_evaluation_error_rate`,
  default 0, so it fails closed (scenario R7, mutant M66).

**Re-verification after repair.**

- The reviewer's scripts were re-run against the repaired code, and every finding now
  behaves as fixed.
- Test suite: 147 tests pass on Python 3.10, 3.12 and 3.13.
- `selfcheck`: 41/41 scenarios.
- Mutation gate: 66 mutants, 65 killed, 0 survived, 0 invalid, 1 equivalent (justified).
- Benchmark: re-run because the hot path changed. Throughput fell from about 6,300 to
  about 5,600 cases/s (zero-latency provider); see PERFORMANCE.md.
