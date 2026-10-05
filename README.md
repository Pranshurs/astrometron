# Astrometron

**Reproducible measurement and evidence for AI systems.**

Your benchmark says Model A scores 87%.
Astrometron records exactly which dataset, model, prompt, tools, scorer, judge, failures
and environment produced that number, and it keeps every failed case in the accounting.

```
Without Astrometron                    With Astrometron

"92% success"                          PASS             840
                                       TASK_FAILURE      80
(80 failed API calls                   INFRA_ERROR       80
 silently dropped)                     TOTAL           1000

                                       Reported success:
                                       84.0% of requested cases
```

**No evaluation number without its evidence.**

## Five minutes, no API account

```bash
pip install astrometron                                  # https://pypi.org/project/astrometron/
git clone https://github.com/Pranshurs/astrometron.git  # for the bundled example data
cd astrometron/examples/support-routing
astrometron run eval.yaml
```

```
ASTROMETRON RUN

Run:              01M450Y35HWND01M5ZY0B0D157
Dataset:          support-routing-v3  (sha256:0b00d24e02aa…)
Cases:            200
Provider:         mock/mock-v1
Scorer:           classification-v1

PASS               158
TASK_FAILURE        26
MODEL_ERROR          4
INFRA_ERROR         12

TOTAL              200

No rows omitted.

Pass rate:        79.0% of 200 (requested)  95% CI [72.8%, 84.1%]
  conditional:    85.9% of 184 scored (PASS / (PASS+TASK_FAILURE)) - not the headline number
Cost:             $0.003727 total, $0.00001864/case (price table)

Receipt:
.astrometron/runs/01M450Y35HWND01M5ZY0B0D157/receipt.json
```

The provider outage (12 cases) is `INFRA_ERROR`, not a model failure, and it stays in
the denominator.

## What can I ask of a number?

| Question | Where the answer lives |
|---|---|
| Which exact dataset revision, and which rows? | `dataset.revision` (content digest), `selected_case_ids`, per-case `row_digest` |
| Which model, provider and version actually answered? | `provider.identity`, `served_models` (substitution is warned) |
| Which prompt, tools, scorer and judge? | `prompt.digest`, `tools[].version/code_digest`, `scorer.code_digest`, `judge` |
| Which environment and seeds? | `environment`, `code.git_commit/git_dirty`, `run.seed` |
| Which failures were included, and which were infrastructure? | `aggregate.counts` across 11 typed outcomes |
| Did any failed row silently disappear? | `astrometron verify`: requested = accounted, re-derived |
| Can it be reproduced? | `astrometron reproduce RUN_ID`, with an honest verdict |

## Reproduce, compare, verify

```bash
astrometron reproduce latest
#   Verdict: REPRODUCED              (pinned, deterministic: every case identical; --repeats N)
#   Verdict: NOT_EXACTLY_REPRODUCIBLE  EXACT REPRODUCTION NOT POSSIBLE: provider model
#            revision is not externally pinned.   (re-runs everything it controls anyway)
#   Verdict: INPUT_MISMATCH          scorer changed (function_digest)

astrometron compare RUN_A RUN_B
```

Real output from the bundled demo (mock model vs keyword classifier):

```
Comparability: STRONGLY_COMPARABLE

PASS RATE (each on its full denominator 200 / 200)
79.0% -> 95.0%   +16.0 percentage points

Paired on 184 cases scored in both runs: +8.7 pp  95% CI [+2.6, +14.8]  McNemar p=0.009041

Improved cases:        25
Regressed cases:        9
Not scored in both:    16  (errors/cancellations: not model verdicts, excluded from the paired test)

Outcome transitions:
  INFRA_ERROR -> PASS                    12
  MODEL_ERROR -> PASS                     4
  PASS -> TASK_FAILURE                    9
  TASK_FAILURE -> PASS                   25
```

The headline moved 16 points. On the 184 cases that both runs actually scored, the
model-versus-model difference is +8.7 points. Much of the rest comes from 16 cases where
run A's provider failed: they count against A's headline, but they are kept out of the
paired test because an outage is not a model verdict. If the judge, scorer or
dataset had changed too, the label would be `PARTIALLY_COMPARABLE` with the reason
listed (e.g. `judge model also changed: not a clean model-only comparison`).

```bash
astrometron verify RUN_ID      # VALID | STALE (config/data changed since) | INVALID (+ why)
```

## Does it actually catch things?

Astrometron evaluates itself. `astrometron selfcheck` runs 40 adversarial scenarios. Each
one pairs an honest control (which must verify) with a deliberately broken evaluator or
forged receipt (which must be caught by a named rule):

```
[PASS] A3  failed rows deleted from cases AND selection, fully resealed
[PASS] F2  harness swallows the outage and returns an empty answer
[PASS] J2  judge failures relabelled as TASK_FAILURE
[PASS] R3  forged better-looking evidence offered to the router
...
40/41 scenarios passed
```

A mutation gate (`tools/mutation_gate.py`) breaks each critical invariant on purpose:
66 mutants, 65 killed, 1 equivalent with a written proof. See
[docs/TESTING.md](docs/TESTING.md).

## Write an eval

```yaml
name: support-routing
task: classification                 # task class, used by the router
dataset:
  path: support-tickets.jsonl        # JSONL or CSV; content-addressed
  id: support-routing-v3
  # revision: sha256:…               # pin: refuse to run if the data changed
  # exclude: [{case_id: tkt-0042, reason: "label disputed"}]
prompt:
  system: "Answer with one word: billing, technical, account or shipping."
  template: "{input}"
provider:
  kind: openai-compat                # or: mock | python
  base_url: http://127.0.0.1:8080/v1 # llama.cpp, Ollama, LM Studio, vLLM, hosted APIs
  model: qwen2.5-7b-instruct
  api_key_env: MY_API_KEY            # literal keys are refused
scorer:
  kind: classification               # exact | normalized | regex | numeric | json_fields | python | judge
  labels: [billing, technical, account, shipping]
run: {seed: 0, concurrency: 8, timeout_s: 60, retries: 2}
pricing: prices.yaml                 # or omit: cost is then UNKNOWN, never invented
```

Agents and tools: point `solver` at `tool_loop` or your own function. Every model call
and tool call (name, version, argument digest, result digest, status, timing) is recorded
in the case's trajectory. See [examples/agent-tools](examples/agent-tools).

## Routing, but only on evidence

Once there is trustworthy evidence, `astrometron route policy.yaml` picks a model:

```
selected: python/keyword-router-v1

evidence:
  run 01M450Y3QBABGKD9KJ8FREP8HR  (0.0 days old)
  receipt sha256:07610bd5…
  success: 95.0%  (95% CI 91.0-97.3%, wilson_lower used)
  cost: $0.000000/case
  cases: 200

candidates:
   mock/mock-v1        BELOW_QUALITY_FLOOR
      - BELOW_QUALITY_FLOOR: success wilson_lower 0.728354 < 0.85
      - UNRELIABLE: error rate 0.08 > 0.05
   mock/mock-large     ELIGIBLE
 * python/keyword-router-v1  ELIGIBLE
```

The router fails closed:

- `NO ELIGIBLE MODEL` when no candidate meets the constraints;
- `ROUTING EVIDENCE STALE` when the evidence is old or the model revision changed;
- `COST_UNKNOWN` under a cost limit;
- tampered evidence is skipped. See [docs/ROUTING.md](docs/ROUTING.md).

## Docs

[Architecture](docs/ARCHITECTURE.md) ·
[Datasets](docs/DATASETS.md) ·
[Receipt schema](docs/RUN_MANIFEST.md) ·
[Failure taxonomy](docs/FAILURE_TAXONOMY.md) ·
[Reproducibility](docs/REPRODUCIBILITY.md) ·
[Comparability](docs/COMPARABILITY.md) ·
[Routing](docs/ROUTING.md) ·
[Threat model](docs/THREAT_MODEL.md) ·
[Testing](docs/TESTING.md) ·
[Performance](docs/PERFORMANCE.md) ·
[Ecosystem](docs/ECOSYSTEM.md)

## Scope

Astrometron is not:

- an observability dashboard;
- a prompt manager;
- an agent framework;
- a leaderboard;
- a hosted service.

It sits beside your eval runner (Inspect, promptfoo, MLflow, your own scripts) and makes
its numbers accountable. Its only runtime dependency is PyYAML.

Apache-2.0. Contributions welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).
