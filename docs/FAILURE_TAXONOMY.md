# Failure taxonomy

Every requested case resolves to exactly one outcome. None of them disappears from the
denominator.

| Outcome | Meaning | Attributed to model? | Example |
|---|---|---|---|
| `PASS` | The model answered and the scorer accepted it. | – | Correct label. |
| `TASK_FAILURE` | The model answered and the scorer rejected it. Malformed answers count here: not a number, not JSON, a label outside the label set. | **Yes** | Answered "billing" for a shipping ticket. |
| `MODEL_ERROR` | The provider was reached but rejected the request or returned something unusable for a model-side reason. | Reported separately, never folded into TASK_FAILURE | Context length exceeded; content filter; empty or malformed payload; HTTP 400/404/413/422. |
| `INFRA_ERROR` | The provider was unavailable. | No | Connection refused; DNS; HTTP 5xx; 429 rate limit; 401/403 auth; 408. |
| `TOOL_ERROR` | A tool used by the solver failed and the solver did not recover. | No | Order database unreachable. |
| `HARNESS_ERROR` | Evaluation-side code failed. | No | Solver exception; scorer exception; prompt template can't render; unknown tool requested by the solver; solver returned a non-string. |
| `JUDGE_ERROR` | The judge call failed, or its output did not contain exactly one well-formed final verdict line. | No | Judge 503; "8/10, probably fine". |
| `TIMEOUT` | The case exceeded `run.timeout_s`, or the provider timed out. | Unattributed | Hung request. |
| `CANCELLED` | The run was interrupted before the case finished. | No | Ctrl-C. |
| `INVALID_CASE` | The dataset row itself is unusable. | No | Missing input. |
| `EXCLUDED` | Explicitly excluded in config, with a mandatory reason. | No | Disputed label. |

## Attribution rules

These rules are applied in one place, `runner._CaseRunner._execute`:

1. An uncaught `ProviderError` takes its kind: `INFRA` → `INFRA_ERROR`, `MODEL` →
   `MODEL_ERROR`, `TIMEOUT` → `TIMEOUT`.
2. **Swallowed outages are still outages.** If the solver returns normally but its last
   model call failed with no later successful call, the case takes that failure's
   outcome. A harness that catches a 503 and returns `""` cannot turn an outage into a
   `TASK_FAILURE`.
3. An uncaught `ToolFailure` → `TOOL_ERROR`. A tool failure the solver recovers from is
   kept in the trajectory as evidence but does not decide the outcome.
4. `JudgeError` → `JUDGE_ERROR`, never a score of 0.
5. Anything else raised by solver or scorer code → `HARNESS_ERROR` with `stage`.
6. Retries (`run.retries`) apply only to retryable provider errors. Every attempt is a
   trajectory step.

## Verifier rules that protect the taxonomy

`verify_receipt` rejects:

- `PASS`/`TASK_FAILURE` without a score, or with a score that contradicts it;
- `PASS`/`TASK_FAILURE` with a recorded error;
- `PASS`/`TASK_FAILURE` whose final model call failed (relabelled outage);
- a judge-scored verdict without a parsed judge verdict (relabelled judge failure);
- an error outcome without an error record, or carrying a score;
- `EXCLUDED` without a configured exclusion, or a configured exclusion with another outcome;
- unknown outcome strings.

## Metrics

- **Headline**: `PASS / requested` (minus named explicit exclusions), with a Wilson 95% CI.
- **Conditional**: `PASS / (PASS + TASK_FAILURE)`. This is reported with its denominator
  and never as the headline.
- **Router reliability**: `(INFRA_ERROR + MODEL_ERROR + TIMEOUT) / attempted`.
