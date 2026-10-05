# Architecture

Astrometron is a small library and CLI. Its one job is to make every evaluation number
traceable to exactly what produced it, and to refuse to use numbers that can't be traced.

```
            eval.yaml                       routing policy
                │                                 │
   ┌────────────▼────────────┐        ┌───────────▼───────────┐
   │ config (strict, no      │        │ router                │
   │ literal secrets)        │        │  gates: verified,     │
   └────────────┬────────────┘        │  comparable, fresh,   │
                │                     │  sample, quality,     │
   ┌────────────▼────────────┐        │  reliability, p95,    │
   │ dataset (content-       │        │  cost (fail closed)   │
   │ addressed, revisioned)  │        └───────────▲───────────┘
   └────────────┬────────────┘                    │ verified receipts only
                │ selected case ids               │
   ┌────────────▼────────────┐  per case  ┌───────┴───────────┐
   │ runner                  ├───────────►│ store             │
   │  SolverContext ─► model │  records   │  runs/<id>/       │
   │               └─► tools │            │   receipt.json    │
   │  scorer / judge         │            │  objects/sha256/  │
   └────────────┬────────────┘            │  index.sqlite     │
                │ every case: one typed   └───────┬───────────┘
                │ outcome                         │
   ┌────────────▼────────────┐            ┌───────▼───────────┐
   │ receipt: aggregate,     │  verify    │ reproduce /       │
   │ seal (digests), self-   ├───────────►│ compare / verify  │
   │ verification            │            └───────────────────┘
   └─────────────────────────┘
```

## Modules

| Module | Responsibility |
|---|---|
| `canonical.py` | Canonical JSON and `sha256:` digests. NaN, sets and bytes are rejected. |
| `dataset.py` | `Case`, `Dataset`, JSONL/CSV/Python loaders, deterministic selection, snapshots. |
| `taxonomy.py` | `Outcome` enum and provider-error-to-outcome mapping. |
| `providers/` | `ModelProvider` protocol; `mock`, `python` and `openai-compat` adapters. |
| `solver.py` | `SolverContext` (the only route to the model and tools; records trajectories), `generate`, `tool_loop`. |
| `scorers.py`, `judge.py` | Deterministic scorers; model-as-judge with provenance and strict verdict parsing. |
| `runner.py` | Executes cases, attributes failures and builds, self-verifies and stores the receipt. |
| `receipt.py` | Receipt schema, aggregation, `seal`, `verify_receipt`. |
| `store.py` | Atomic files, content-addressed objects, SQLite index, migrations, `verify_run`. |
| `reproduce.py` | Re-identification of controlled inputs, reruns, verdicts, staleness. |
| `compare.py` | Comparability labels, paired regression analysis. |
| `cost.py` | Price tables with provenance; cost is provider-reported, computed from a table, or UNKNOWN. |
| `router.py` | Evidence-backed selection; decisions stored content-addressed. |
| `integrity.py` | `astrometron selfcheck`, the adversarial suite. |
| `cli.py` | `astrometron` command. |

## Design rules

1. **Re-derive, don't trust.** Verification recomputes digests, the accounting identity,
   every aggregate number, and the case selection from the dataset snapshot.
2. **Every selected case gets exactly one record.** This holds even when a solver, scorer,
   judge or tool crashes, a case times out, or the user presses Ctrl-C (unfinished cases
   become `CANCELLED` and the run is `INCOMPLETE`). The runner verifies its own receipt
   before storing it. A receipt that fails is never written, and the run stays `RUNNING`,
   which every reader treats as incomplete.
3. **Errors are typed at the boundary that observed them.** Provider adapters raise
   `ProviderError(kind)`. The context records every call. Attribution happens once, in
   `runner._CaseRunner._execute`.
4. **Fail closed.** Unknown cost under a cost limit, stale or unverifiable evidence, and
   incomparable runs never silently pass.
5. **One dependency.** The core needs only PyYAML. No server, no cloud, no paid API.

## Storage and durability

- Files are written to a temp file in the same directory, `fsync`ed, `os.replace`d, and
  then the directory is `fsync`ed.
- The SQLite index runs in WAL mode with `synchronous=FULL`. Schema version is in the
  `meta` table. Migrations are ordered functions. A newer store refuses to open with an
  older Astrometron, and a foreign SQLite file is refused.
- Run lifecycle: `begin_run` inserts `RUNNING`, then the receipt file is written, then a
  single transaction sets the final status and the receipt digest. A crash before that
  transaction leaves `RUNNING`, which is never treated as evidence.

Documented semantics: a completed run's receipt survives power loss once
`astrometron run` has returned. A run interrupted by a crash is visible as incomplete,
never as complete.

## What is deliberately not here

No tracing system, no dashboard, no prompt management, no agent framework, no remote
registry, no hosted service. Astrometron records evidence and checks it. See
[ECOSYSTEM.md](ECOSYSTEM.md) for what to use alongside it.
