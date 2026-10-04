# Failure taxonomy

Every requested case resolves to exactly one outcome. None of them disappears from the
denominator.

| Outcome | Meaning | Attributed to model? | Example |
|---|---|---|---|
| `PASS` | The model answered and the scorer accepted it. | – | Correct label. |
| `TASK_FAILURE` | The model answered and the scorer rejected it. Malformed answers count here: not a number, not JSON, a label outside the label set. | **Yes** | Answered "billing" for a shipping ticket. |
| `MODEL_ERROR` | The provider was reached but rejected the request or returned something unusable for a model-side reason. | Reported separately, never folded into TASK_FAILURE | Context length exceeded; content filter; empty or malformed payload; HTTP 400/404/413/422. |
| `INFRA_ERROR` | The provider was unavailable. | No | Connection refused; DNS; HTTP 5xx; 429 rate limit; 401/403 auth; 408; truncated HTTP; a non-JSON body (e.g. a proxy error page). |
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
2. **Swallowed failures are still failures.** There is one rule
   (`solver.unattributed_failure`), shared by the runner and the verifier. Walk the
   trajectory backwards. If a failed model call or failed tool call comes before any
   successful model call, the answer is not the model's verdict:
   - a failed model call → `MODEL_ERROR` / `INFRA_ERROR` / `TIMEOUT`;
   - a failed tool call → `TOOL_ERROR`.

   A harness that catches a 503 and returns `""` cannot turn an outage into a
   `TASK_FAILURE`.
3. An uncaught `ToolFailure` → `TOOL_ERROR`. If the model is called again *after* a tool
   failure and answers, that answer is scored. Working around a broken tool is agent
   behaviour. The case is marked `degraded: ["tool:<name>"]`, and the aggregate counts
   `scored_after_tool_failure`.
4. A case record that cannot be canonically serialised (e.g. a provider reports a NaN
   cost, or a scorer returns NaN) becomes `HARNESS_ERROR` with `stage: record` for that
   case only. The run completes.
5. `JudgeError` → `JUDGE_ERROR`, never a score of 0.
6. Anything else raised by solver or scorer code → `HARNESS_ERROR` with `stage`.
7. Retries (`run.retries`) apply only to retryable provider errors. Every attempt is a
   trajectory step.

## Verifier rules that protect the taxonomy

`verify_receipt` rejects:

- `PASS`/`TASK_FAILURE` without a score, or with a score that contradicts it;
- `PASS`/`TASK_FAILURE` with a recorded error;
- `PASS`/`TASK_FAILURE` whose final model call failed, or with a tool failure after the
  last successful model call (relabelled outage or tool failure);
- a judge-scored verdict without a parsed judge verdict (relabelled judge failure);
- an error outcome without an error record, or carrying a score;
- `EXCLUDED` without a configured exclusion, or a configured exclusion with another outcome;
- unknown outcome strings.

## Metrics

- **Headline**: `PASS / requested` (minus named explicit exclusions), with a Wilson 95% CI.
- **Conditional**: `PASS / (PASS + TASK_FAILURE)`. This is reported with its denominator
  and never as the headline.
- **Router reliability**: `(INFRA_ERROR + MODEL_ERROR + TIMEOUT) / attempted`.
