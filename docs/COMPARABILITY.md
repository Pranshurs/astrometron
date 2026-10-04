# Comparability

```
evalledger compare RUN_A RUN_B
```

Two runs differ in *measurement* factors (what was measured, and how) and *treatment*
factors (what you are trying to compare). The label is decided only by measurement
factors. Changed treatments are listed, and you are warned when more than one changed.

## Labels

| Label | Rule | What you can conclude |
|---|---|---|
| `STRONGLY_COMPARABLE` | Same dataset revision, same selected case set, same exclusions, same scorer identity (name, version, params, code digest), same judge (or both none). Both receipts verify. | Differences come from the treatments listed in `changed_treatments`. |
| `PARTIALLY_COMPARABLE` | Same dataset id, but the revision, selection, exclusions, scorer or judge differ, or either run is `INCOMPLETE`. | Paired statistics use only cases present in both runs with identical row digests. Score differences may come from measurement, not the model. Reasons are listed. |
| `NOT_DIRECTLY_COMPARABLE` | Different dataset id, no shared unchanged cases, or either run fails **full store verification** (receipt digest, index binding, dataset and selection binding). A receipt that is only self-consistent is not enough. | Nothing. Headline numbers are not shown against each other. |

Treatment factors: model (provider/model/revision), request params, prompt, solver,
tools, seed, retries/timeout. Code revision, EvalLedger version and Python version are
also listed but don't trigger the single-factor warning.

Warnings:

- `judge model also changed: not a clean model-only comparison`
- `scorer changed: score differences may be measurement, not model`
- `more than one treatment changed (…): this is not a clean single-factor comparison`
- either run `INCOMPLETE`

## Statistics

- Headline pass rates are shown on each run's own full denominator.
- **Paired analysis** on cases that are shared, unchanged, not excluded, **and scored
  in both runs**:
  - `improved` = TASK_FAILURE → PASS. `regressed` = PASS → TASK_FAILURE.
  - Outages and cancellations are not model verdicts. They are reported as
    `not_scored_in_both`, `new_infra_failures` and outcome transitions, and they are
    never counted as regressions.
  - Delta with a 95% CI for paired proportions:
    `d = (b − c)/n`, `var = ((b + c) − (b − c)²/n)/n²`.
  - Exact two-sided McNemar p-value on the discordant pairs.
  - All outcome transitions (e.g. `INFRA_ERROR -> PASS: 12`).
- Latency p50/p95/p99 (model latency) and cost per case, shown as `UNKNOWN` when either
  run lacks complete cost metadata.
- A flattened diff of the two configs.
