# Routing on verified evidence

The router picks a model for a task class from stored, verified evaluation receipts.
It never asks a model which model is best, and every decision cites its evidence.

```
astrometron route route.yaml
```

```yaml
task: classification
dataset: support-routing-v3        # optional: evidence must be on this dataset id
dataset_revision: sha256:…         # optional: pin the routing basis
requirements:
  min_success_rate: 0.85           # checked against quality_bound
  quality_bound: wilson_lower      # wilson_lower (default) | point
  max_p95_ms: 1500                 # model latency p95
  max_cost_per_case_usd: 0.01      # UNKNOWN cost fails closed when set
  max_error_rate: 0.05             # (INFRA + MODEL + TIMEOUT) / attempted
  min_cases: 100
  max_evidence_age_days: 30        # default 30; null disables (explicitly)
  max_excluded_fraction: 0.0       # default 0: evidence with explicit exclusions fails
  require_pinned: false
  max_evaluation_error_rate: 0.0   # default 0: HARNESS/JUDGE/INVALID_CASE errors break evidence
  max_degraded_fraction: 0.1       # optional: cases scored after a recovered tool failure
objective: cheapest                # cheapest | best_quality | fastest
candidates:                        # same shape as an eval's provider block
  - {kind: openai-compat, base_url: http://127.0.0.1:8080/v1, model: qwen-7b, probe: true}
  - {kind: mock, model: mock-large, accuracy: 0.97, unavailable: true}
```

## Gates

Each candidate passes through every gate. All failures are listed, and the first one
names the status.

| Gate | Status on failure |
|---|---|
| Candidate builds from config | `CONFIG_ERROR` |
| A COMPLETE, fully verified run exists for (task, provider/model). Tampered and crashed runs are skipped and listed under `skipped_invalid_evidence`. | `NO_EVIDENCE` |
| Evidence is on the routing basis: same dataset revision, selection, exclusions, scorer and judge. The basis is the pinned revision, or else the newest verified run's. | `INCOMPARABLE_EVIDENCE` |
| Evidence was gathered against the candidate's endpoint (credential-free URL) | `DIFFERENT_DEPLOYMENT` |
| Evidence age ≤ `max_evidence_age_days` (default 30 days) | `STALE_EVIDENCE` |
| Evaluated model revision = candidate's current revision | `MODEL_REVISION_CHANGED` |
| `require_pinned` | `UNPINNED_MODEL` |
| Explicitly excluded / requested ≤ `max_excluded_fraction` (default 0) | `EXCESSIVE_EXCLUSIONS` |
| Cases in the headline denominator ≥ `min_cases` | `INSUFFICIENT_SAMPLE` |
| Not `unavailable`, and the `probe` call succeeds | `UNAVAILABLE` |
| Quality bound ≥ `min_success_rate` | `BELOW_QUALITY_FLOOR` |
| Error rate ≤ `max_error_rate` | `UNRELIABLE` |
| (HARNESS + JUDGE + INVALID_CASE) / requested ≤ `max_evaluation_error_rate` (default 0). The measurement itself was broken. | `BROKEN_EVALUATION` |
| Cases scored after a recovered tool failure / cases ≤ `max_degraded_fraction` (only if set) | `DEGRADED_EVIDENCE` |
| p95 ≤ `max_p95_ms` | `TOO_SLOW` |
| Cost known and ≤ `max_cost_per_case_usd` | `COST_UNKNOWN` / `TOO_EXPENSIVE` |

Decision status:

- `SELECTED`: the best eligible candidate by the objective. Ties break by quality, then
  latency, then name.
- `NO_ELIGIBLE_MODEL`: nothing passes. Astrometron never picks something anyway.
- `ROUTING_EVIDENCE_STALE`: nothing passes, and at least one candidate failed *only* on
  freshness (age or revision drift). Re-evaluate before routing.

The quality floor defaults to the Wilson 95% lower bound, so 20/20 does not beat a 0.9
floor (lower bound ≈ 0.84) while 1130/1200 does (≈ 0.93). Use `quality_bound: point`
for the raw rate.

## Decision record

Every decision is stored content-addressed (`objects/sha256/…`) and indexed in the
`routing_decisions` table. It contains:

- the policy digest and requirements;
- the routing basis;
- each candidate's status, reasons, and evidence (run id, receipt digest, age, cases,
  success and CI, error rate, p95, cost per case, evaluated revision);
- invalid evidence that was skipped.

## Cost

Cost comes from the provider's own usage metadata, from an explicit price table with
`source` and `updated_at` that is embedded in the receipt, or it is `UNKNOWN`. The
router uses the cost recorded at evaluation time, under the price assumptions recorded
then.
