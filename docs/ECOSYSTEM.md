# Ecosystem review (2026-10-04)

A short review of existing evaluation and routing tools against the things EvalLedger
cares about. Sources are the projects' own docs. **UNVERIFIED** marks a claim not
confirmed from a primary source during this review. Treat it as a question to check,
not a fact.

## What existing systems already solve

- **Inspect AI** (UK AISI): [eval logs](https://inspect.aisi.org.uk/eval-logs.html). This
  is the strongest existing run-log design.
  - The `EvalLog` header records task, model, solver plan, generate config, results,
    token stats and status.
  - Per-sample errors are recorded, and completed samples are preserved in failed runs.
  - `inspect eval-retry` resumes runs, which needs stable sample ids.
  - UNVERIFIED: how errored samples enter score denominators, and whether dataset
    content digests or package versions are captured.
- **promptfoo**: [CLI docs](https://www.promptfoo.dev/docs/usage/command-line/). It
  explicitly distinguishes ERROR (provider, network, rate-limit) from FAIL (assertion),
  and `--retry-errors` re-runs only errors. UNVERIFIED: its pass-rate arithmetic
  (a search snippet suggests errors are outside the denominator).
- **MLflow GenAI evaluation**:
  [eval-monitor](https://mlflow.org/docs/latest/genai/eval-monitor/),
  [datasets](https://mlflow.org/docs/latest/genai/datasets/). Scorers, LLM judges,
  managed evaluation datasets with source types, and tracing with latency and tokens.
  UNVERIFIED: dataset digests and errored-row aggregation.
- **W&B Weave**:
  [evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations). Publishing
  versions datasets, code and scorers, and `trials` repeat examples. `EvaluationLogger`
  ingests externally computed results. UNVERIFIED: error and denominator semantics.
- **LangSmith**: [evaluation](https://docs.langchain.com/langsmith/evaluation). Datasets,
  experiments, evaluators (code, LLM judge, pairwise) and experiment comparison.
  UNVERIFIED in this review: version-tag pinning and error semantics.
- **Arize Phoenix**:
  [experiments](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments/running-experiments).
  Datasets, experiments and evaluators. Tracks error rate, latency, cost and tokens.
- **DeepEval**:
  [error config](https://deepeval.com/docs/evaluation-flags-and-configs). The
  `ignore_errors` / `skip_on_missing_params` options mean "the result will be missing",
  which is exactly the denominator problem when enabled.
- **Ragas**: [metrics](https://docs.ragas.io/en/stable/concepts/metrics/overview/). A
  metric library. UNVERIFIED: NaN handling for failed metric computations.
- **OpenAI Evals**: [repo](https://github.com/openai/evals). A YAML/JSONL registry; the
  project steers users to hosted dashboard evals.
- **Routing**:
  - [OpenRouter Auto Router](https://openrouter.ai/docs/features/model-routing) ranks by
    community usage share per task type.
  - [RouteLLM](https://github.com/lm-sys/RouteLLM) trains strong/weak routers on public
    preference data.
  - [LiteLLM Router](https://docs.litellm.ai/docs/routing) routes on operational signals
    (latency, usage, cost map, availability).
  - [Not Diamond](https://docs.notdiamond.ai/docs/what-is-not-diamond) offers custom
    routers trained on your evaluation data. UNVERIFIED: mechanism and evidence
    handling. Martian was not reviewed.

## Integrate, don't reinvent

- **Run evaluations in Inspect, promptfoo, MLflow or your own harness.** EvalLedger's
  runner is deliberately small. The receipt format is framework-neutral, and import
  adapters are the natural next step:
  - Inspect `EvalLog` → receipt, mapping `sample.error` to the taxonomy;
  - promptfoo JSON, mapping ERROR/FAIL to INFRA_ERROR/TASK_FAILURE.
- **Export receipts** as run artifacts or tags to MLflow, Weave or Phoenix. Do not build
  a dashboard.
- **Consume traces** (OpenTelemetry / OpenInference) for trajectories rather than
  building a tracing system.
- **Use LiteLLM, or any OpenAI-compatible gateway, as the call layer.** EvalLedger only
  needs typed errors and the served model name back.
- Treat other routers' outputs as candidates. EvalLedger supplies the evidence and
  freshness gate around them.

## What remains poorly solved

Each item is based on what we could confirm, not on a claim that nobody does it.

1. **Strict denominator preservation as a verifiable invariant.** Errors are commonly
   tracked, but we found no tool that *re-derives* "requested = accounted" from a stored
   artifact and rejects it otherwise, or that keeps errored rows in the headline
   denominator by default.
2. **A typed taxonomy beyond error vs fail.** HARNESS, JUDGE, TOOL, MODEL and INFRA kept
   apart, with a rule that a swallowed provider failure is still a provider failure.
3. **Content-addressed dataset revisions bound into every result**, with selection
   re-derivable from a stored snapshot.
4. **Explicit comparability labels** between runs, based on dataset, scorer and judge
   identity.
5. **Reproduction verdicts** that say when exactness is impossible (unpinned provider)
   instead of just "re-run".
6. **Routing that cites verified evidence and fails closed** on stale, incomparable,
   unverifiable or cost-unknown evidence.

## Differentiation, honestly

EvalLedger is not a better eval runner or a better dashboard. Its value is the
**evidence contract**:

- a sealed, re-verifiable receipt with a denominator guarantee and typed failures;
- an adversarial self-test proving that its checks catch the failures they claim to
  catch;
- routing that is only as confident as that evidence allows.

If Inspect or promptfoo already run your evals, EvalLedger should sit beside them, not
replace them.
