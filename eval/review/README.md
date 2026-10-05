# Pre-publication review reproductions

These scripts reproduce the findings of the pre-publication review described in
[`docs/REVIEW.md`](../../docs/REVIEW.md). Each one builds a small evaluation in a
temporary directory and prints the behaviour the finding was about. They are kept as
regression and review evidence: on the current code every script shows the *fixed*
behaviour.

Run them from a checkout with Astrometron installed (`pip install -e .`):

```bash
cd eval/review
python r1_nan.py
```

| Script | Finding | Expected output on current code |
|---|---|---|
| `r1_nan.py` | HIGH-3: a non-finite value aborted the run | both runs `OK` with `HARNESS_ERROR` per case; no run left `RUNNING` |
| `r2_route_task.py` | HIGH-1: index-only tamper made a run evidence for another task | `verify_run ... False`; route `NO_ELIGIBLE_MODEL` |
| `r3_compare.py` | HIGH-2: compare accepted a re-sealed forged receipt | CLI compare `NOT_DIRECTLY_COMPARABLE` (fails store verification) |
| `r4_repro.py` | MEDIUM-4 / MEDIUM-5: REPRODUCED with nothing scored; interrupted runs compared | `NO_SCORED_CASES`; noisy provider `NOT_EXACTLY_REPRODUCIBLE`; interrupted run `PARTIALLY_COMPARABLE` with no regressions |
| `r5_misc.py` | MEDIUM-7, 9, 8: tool failure attribution, dataset hashing, endpoint identity | (1) tool fails, then the model answers: `PASS`, marked `degraded` (stated rule); revisions order/path independent; reserved columns hashed; different endpoints give different identities |
| `r6_excl.py` | MEDIUM-6: cherry-picked exclusions passed the router | route `NO_ELIGIBLE_MODEL`; `cases` counts the headline denominator |
| `r7_timeout.py` | LOW-1 / LOW-2 (documented limits) | timeout record correct; documented behaviours visible |
| `r8_post_repair.py` | Post-repair boundary checks | tool-then-model `PASS` + `degraded`; model-then-tool-failure `TOOL_ERROR`; NaN/surrogate/infinite values become per-case `HARNESS_ERROR` |

`common.py` and `helpers.py` hold the shared fixtures. The scripts are deliberately
small and direct rather than polished; the maintained regression tests for the same
findings are in `tests/test_review_findings.py` and the integrity suite
(`astrometron selfcheck`).
