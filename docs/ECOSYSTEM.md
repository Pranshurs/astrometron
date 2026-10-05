# Ecosystem review

A short review of existing evaluation and routing tools against the things Astrometron
cares about. Every statement below was checked against the project's own documentation
or source code on 2026-10-05. Line references are to each project's default branch on
that date. Behaviour changes between releases, so treat this as a dated snapshot.
Anything we could not confirm is marked **not documented**.

## How errored cases are counted elsewhere

This is the question Astrometron is most opinionated about. When an evaluation case
errors, rather than simply getting a wrong answer, does it stay in the headline
denominator?

| Tool | Errored case in headline metric | Source |
|---|---|---|
| Inspect AI | Excluded: no score, recorded in `sample.error`. With `score_on_error` it is scored and included. | [scoring policy](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/docs/scoring-policy.qmd), [handling errors](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/docs/handling-errors.qmd) |
| promptfoo | Counted separately (`testErrorCount`). The web UI pass rate divides by pass + fail only. | `src/evaluator.ts` (`updatePromptResultCounts`), `src/app/src/pages/eval/components/hooks.ts` |
| MLflow GenAI | Predict and scorer exceptions are recorded per row. Errored scorer results are excluded from aggregation, with a warning summary. | `mlflow/genai/evaluation/harness.py`, `mlflow/genai/scorers/aggregation.py` |
| W&B Weave | A model exception gives empty scores. Numeric means exclude the row; boolean fractions keep it in the denominator. | `weave/evaluation/eval.py`, `weave/flow/scorer.py` (`auto_summarize`) |
| Ragas | A failed metric is NaN (unless `raise_exceptions=True`), and NaN is dropped from the mean (`np.nanmean`). | `src/ragas/evaluation.py`, `src/ragas/utils.py` (`safe_nanmean`) |
| LangSmith | `error_handling="log"` (default) keeps errored runs in the experiment; `"ignore"` drops them. Metric denominator rules are not documented. | `python/langsmith/evaluation/_runner.py` |
| DeepEval | `ignore_errors` skips exceptions during metric execution. `skip_on_missing_params` skips metrics, and "the result will be missing". The effect on aggregates is not documented. | [flags and configs](https://deepeval.com/docs/evaluation-flags-and-configs) |

None of these choices is wrong: excluding errors answers "how good are the answers the
model gave?". Astrometron's headline answers a different question, "what fraction of the
requested cases passed?", and reports the conditional rate separately with its
denominator named.

## What existing systems already solve

- **Inspect AI** (UK AISI): the strongest existing run-log design.
  - `EvalLog` records the task, model, solver plan, generate config, results, token stats
    and status.
  - `EvalSpec.revision` records the git commit and dirty flag; `EvalSpec.packages`
    records package versions.
  - The dataset is recorded by name, location and sample ids, with **no content digest**
    (`EvalDataset` in `src/inspect_ai/log/_log.py`).
  - `fail_on_error`, `retry_on_error` and `inspect eval-retry` make error handling
    explicit.
- **promptfoo**: separates ERROR from FAIL, and `--retry-errors` re-runs only errors
  ([CLI docs](https://www.promptfoo.dev/docs/usage/command-line/)).
- **MLflow GenAI evaluation**: evaluation datasets carry a content `digest`
  (`mlflow/genai/datasets/evaluation_dataset.py`). Also provides scorers, LLM judges and
  tracing with latency and tokens.
- **W&B Weave**: versioned datasets, code and scorers; `trials` repeat examples;
  `EvaluationLogger` ingests externally computed results
  ([evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations)).
- **LangSmith**: dataset versions can be tagged, and an experiment can be pinned to a tag
  (`list_examples(as_of=<tag>)`;
  [managing datasets](https://docs.langchain.com/langsmith/manage-datasets)).
- **Arize Phoenix**: datasets, experiments and evaluators, tracking error rate, latency,
  cost and tokens
  ([experiments](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments/running-experiments)).
  Checked on 2026-10-04 only.
- **OpenAI Evals**: a YAML/JSONL registry. Its README says it is "currently not accepting
  evals with custom code" and points users to evals in the OpenAI Dashboard
  ([repo](https://github.com/openai/evals)).
- **Routing**:
  - [OpenRouter Auto Router](https://openrouter.ai/docs/features/model-routing) ranks
    models by trailing-7-day community *spend* per classified task type, then applies a
    price tier.
  - [RouteLLM](https://github.com/lm-sys/RouteLLM) trains strong/weak routers on Chatbot
    Arena human-preference data, augmented with GPT-4-judged labels.
  - [LiteLLM Router](https://docs.litellm.ai/docs/routing) routes on operational
    signals: shuffle/weights, latency, usage, least-busy and cost, with fallbacks.
  - [Not Diamond](https://docs.notdiamond.ai/docs/router-training-quickstart) trains
    custom routers on your prompts, each candidate model's responses and numeric
    evaluation scores. The training mechanism is not documented. Martian was not
    reviewed.

## Integrate, don't reinvent

- **Run evaluations in Inspect, promptfoo, MLflow or your own harness.** Astrometron's
  runner is deliberately small. The receipt format is framework-neutral, and import
  adapters are the natural next step:
  - Inspect `EvalLog` → receipt, mapping `sample.error` to the taxonomy and reusing
    `EvalSpec.revision` / `packages`;
  - promptfoo JSON, mapping ERROR/FAIL to INFRA_ERROR/TASK_FAILURE.
- **Export receipts** as run artifacts or tags to MLflow, Weave or Phoenix. Do not build
  a dashboard.
- **Consume traces** (OpenTelemetry / OpenInference) for trajectories rather than
  building a tracing system.
- **Use LiteLLM, or any OpenAI-compatible gateway, as the call layer.** Astrometron only
  needs typed errors and the served model name back.
- Treat other routers' outputs as candidates. Astrometron supplies the evidence and
  freshness gate around them.

## What remains poorly solved

Each item is based only on what we could confirm in the tools above, not on a claim that
nobody does it.

1. **The denominator as a verifiable invariant.** The tools reviewed exclude errored
   cases from at least some headline metrics. None we reviewed *re-derives*
   "requested = accounted" from a stored artifact and rejects the artifact otherwise.
2. **A typed taxonomy beyond error vs fail.** HARNESS, JUDGE, TOOL, MODEL and INFRA are
   kept apart, with one rule that a swallowed provider or tool failure is still that
   failure.
3. **Dataset identity bound into verification, not just recorded.** MLflow records a
   dataset digest and Inspect records sample ids. Astrometron re-derives the case
   selection from a stored snapshot and rejects receipts whose rows or selection no
   longer match.
4. **Explicit comparability labels** between runs, based on dataset, selection, scorer
   and judge identity.
5. **Reproduction verdicts** that say when exactness is impossible (unpinned provider)
   instead of just "re-run".
6. **Routing that cites verified evidence and fails closed** on stale, incomparable,
   unverifiable, broken or cost-unknown evidence.

## Differentiation, honestly

Astrometron is not a better eval runner or a better dashboard. Its value is the
**evidence contract**:

- a sealed, re-verifiable receipt with a denominator guarantee and typed failures;
- an adversarial self-test proving that its checks catch the failures they claim to
  catch;
- routing that is only as confident as that evidence allows.

If Inspect or promptfoo already run your evals, Astrometron should sit beside them, not
replace them.
