# The EvaluationReceipt (`evalledger.receipt/v1`)

Every run writes one receipt to `.evalledger/runs/<run_id>/receipt.json`. It is
framework-neutral JSON: any tool can read it, and `evalledger verify` can check it.

## Top-level fields

| Field | Content |
|---|---|
| `schema` | `"evalledger.receipt/v1"` |
| `run_id` | ULID (time-sortable). |
| `status` | `COMPLETE` or `INCOMPLETE` (interrupted; unfinished cases are `CANCELLED`). |
| `reproduces` | Run id this run reproduces, or null. |
| `eval` | `name`, `task` (the routing task class), `description`. |
| `config`, `config_digest` | The validated eval config exactly as run. |
| `config_source` | Config file path, its byte digest at run time, and the base directory. |
| `dataset` | `id`, `revision`, `schema`, `case_count`, `provenance`, `selection` (spec + seed), `selected_case_ids`, `selected_digest`, `excluded` (`case_id` + `reason`). |
| `code` | `evalledger_version`, `git_commit`, `git_dirty`, `cwd`. |
| `provider` | `identity` {`provider`, `model`, `revision`, `pinned`}, request `params`, `served_models` (what the provider said it served). |
| `prompt` | `digest`, `template`, `system`. |
| `solver` | Name and code digest. |
| `tools` | Each tool's `name`, `version`, `code_digest`. |
| `scorer` | `name`, `version`, `params`, `code_digest` (+ `function_digest` for Python scorers; judge identity for judges). |
| `judge` | Judge provider identity, or null. |
| `environment` | Python version and implementation, platform, machine, package versions. |
| `run` | `seed`, `concurrency`, `timeout_s`, `retries`. |
| `pricing` | The full price table used (source, `updated_at`, prices), or null. |
| `timing` | `started_at`, `finished_at`, `wall_ms`. |
| `cases` | One record per selected case (below). |
| `aggregate` | Derived numbers (below). |
| `warnings` | Model substitution, unpinned provider, dirty code tree, unknown cost. |
| `inputs_digest` | Digest of the controlled inputs that determine outcomes. |
| `outcomes_digest` | Digest of `[case_id, outcome, output_digest, passed]` for every case. |
| `receipt_digest` | Digest of everything above. |

## Case record

```json
{
  "case_id": "tkt-0042",
  "row_digest": "sha256:…", "input_digest": "sha256:…", "expected_digest": "sha256:…",
  "outcome": "INFRA_ERROR",
  "output": "billing", "output_digest": "sha256:…",
  "score": {"passed": true, "value": 1.0, "label": "billing", "evidence": {…judge…}},
  "error": {"stage": "model", "kind": "INFRA", "message": "HTTP 503: …"},
  "latency_ms": {"total": 412.1, "model": 410.7, "tool": 0.0, "scoring": 0.02, "overhead": 1.4},
  "usage": [{"input_tokens": 120, "output_tokens": 3, "reported_cost_usd": null}],
  "cost_usd": 0.00006, "cost_basis": "price_table",
  "served_models": ["gpt-x-2026-05-01"],
  "trajectory": [
    {"type": "model_call", "index": 0, "attempt": 1, "status": "OK", "request_digest": "…",
     "response_digest": "…", "served_model": "…", "usage": {…}, "latency_ms": 410.7},
    {"type": "tool_call", "index": 1, "tool": "order_status", "tool_version": "1.2.0",
     "tool_code_digest": "…", "args_digest": "…", "result_digest": "…", "status": "OK",
     "latency_ms": 3.1}
  ]
}
```

`output` is stored unless `record_outputs: false`. The digest is always stored.

## Aggregate

```json
{
  "requested": 200, "attempted": 200, "scored": 184, "excluded": 0, "accounted": 200,
  "counts": {"PASS": 158, "TASK_FAILURE": 26, "MODEL_ERROR": 4, "INFRA_ERROR": 12, …},
  "pass_rate": {"value": 0.79, "numerator": 158, "denominator": 200,
                "denominator_basis": "requested", "ci95_wilson": [0.728, 0.841]},
  "pass_rate_of_scored": {"value": 0.8587, "denominator": 184,
                          "denominator_basis": "conditional: PASS / (PASS + TASK_FAILURE)"},
  "latency_ms": {"model": {"n", "p50", "p95", "p99", "max"}, "total": {…}, "ledger_overhead": {…}},
  "cost": {"total_usd_known": 0.0037, "known_cases": 200, "unknown_cases": 0,
           "per_case_usd": 0.0000186, "complete": true},
  "labels": {"billing": {"predicted": 51, "correct": 47}, …}
}
```

Percentiles are nearest-rank. The headline `pass_rate` uses all requested cases (minus
explicit exclusions, which are named). The conditional rate is always labelled with its
denominator.

## Digests

- `receipt_digest = sha256(canonical(receipt without receipt_digest))`
- `inputs_digest` covers dataset revision, `selected_digest`, exclusions, provider
  identity (provider, model, revision), request params, prompt digest, solver, tools,
  scorer, seed, timeout and retries. Concurrency and the environment are recorded but not
  included. They are reported as differences on reproduction.
- The store's index records `receipt_digest` when the run finishes. A receipt file that
  no longer matches is reported as tampered, even if its internal digests were
  recomputed.

## Verification (`evalledger verify RUN`)

`VALID`, `STALE` (the config or dataset source on disk changed after the run) or
`INVALID`, with every problem listed. `INVALID` covers:

- a digest mismatch;
- requested ≠ accounted;
- duplicate or extra records;
- an aggregate that disagrees with the records;
- an outcome that contradicts its score or error;
- a scored outcome after a failed final model call;
- a judge verdict without parsed judge evidence;
- an unexplained exclusion;
- dataset or selection binding failure;
- index digest mismatch;
- a run that never finished.
