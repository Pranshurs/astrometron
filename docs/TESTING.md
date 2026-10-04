# How EvalLedger tests itself

EvalLedger is evidence tooling, so "the tests pass" is not enough. Every critical claim
has a check that has been shown to fail when the claim is broken.

## 1. Unit and integration tests (`pytest`)

Datasets, digests, every failure class end to end, the provider error mapping against a
local stub HTTP server, compare, reproduce, store migrations, the router's gates, and
the CLI end to end.

## 2. Adversarial integrity suite (`evalledger selfcheck`)

40 scenarios in `src/evalledger/integrity.py`, run as part of `pytest`. Each scenario
has two arms:

- **an honest control arm** that must verify cleanly, so a verifier that rejects
  everything fails the suite;
- **an attack arm** that must be detected by a *named* rule, not by any error at all.

| Group | Scenarios |
|---|---|
| Denominator | A1 deletion · A2 deletion + resealed aggregate · A3 deletion + rewritten selection + full reseal · A4 skipping runner · A5 duplicate PASS · A6 edited headline |
| Dataset | B1 changed data under a pin · B2 tampered snapshot · B3 swapped row digest |
| Comparison | C1 judge substitution · C2 scorer substitution · C3 scorer code edit, same version |
| Model identity | D1 renamed model · D2 served-model substitution · D3 swapped implementation |
| Taxonomy | E1 harness crash · E2 crash relabelled · F1 outage · F2 outage swallowed by harness · F3/F4 outage relabelled TASK_FAILURE / PASS · G1 tool crash · J1 judge failures · J2 judge failure relabelled |
| Staleness | H1 config edited · H2 dataset edited · H3 crashed run as evidence |
| Reproduction | I1 deterministic repeat (control: a different seed must *not* match) |
| Routing | R1 stale evidence (age, revision drift) · R2 no eligible model · R3 forged evidence · R4 unknown cost under a limit · R5 cherry-picked exclusions · R6 another deployment's evidence |
| Added after review | A7 index-only tamper · C4 compare offered a resealed forgery · C5 interrupted run compared · E3 non-finite provider value · G2 swallowed tool failure · I2 all-outage rerun |

## 3. Mutation gate (`python tools/mutation_gate.py`)

64 hand-written mutants, each breaking one critical invariant: omitting failed cases
from the denominator, accepting a wrong dataset digest, ignoring scorer changes,
accepting stale receipts, turning harness or judge errors into task failures, treating a
provider failure as PASS, unbinding selected case ids, dropping tool-version provenance,
and router gates. Results are in `eval/results/mutation.json`.

The gate:

- requires a passing baseline;
- asserts the mutated tree is the imported one;
- disables bytecode caching;
- fails on SURVIVED or INVALID (FIND text not present exactly once);
- re-runs the baseline after restoring.

Current result: **64 mutants, 63 killed, 0 survived, 0 invalid, 1 equivalent.**

The equivalent mutant is M04, which removes the summary `accounted != requested` check.
It is verdict-equivalent because accounted ≠ requested implies a missing, extra,
duplicate or unknown-outcome record, each of which raises its own problem. The full
justification is in the gate's `EQUIVALENT` table.

History, kept in the git log:

- **First run (48 mutants):** 37 killed, 7 survived, 3 invalid. The survivors were closed
  with behavioural tests, not by weakening mutants.
- **After the independent review:** 16 mutants were added for the repaired rules. Three
  survived at first:
  - one guarded a router check that the repaired `verify_run` made unreachable, so the
    dead check was removed;
  - two were closed with new tests.
- **Retargeting:** two mutants whose FIND text the repair deleted (M23, M24) were moved to
  the new code rather than dropped, and M65 targets the shared attribution rule directly.

## 4. Benchmarks (`python bench/bench_overhead.py`)

See [PERFORMANCE.md](PERFORMANCE.md).
