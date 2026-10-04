# Contributing

Thanks for helping. EvalLedger is evidence tooling, so the bar is: **every claim has a
check that fails when the claim is broken.**

## Setup (≈ 5 minutes)

```bash
git clone <repo> && cd evalledger
python3 -m venv .venv && . .venv/bin/activate      # Python 3.10+
pip install -e ".[dev]"
pytest                                              # unit + integration + adversarial
evalledger selfcheck                                # the integrity suite, via the CLI
```

Before sending a change:

```bash
ruff check src tests tools bench examples && ruff format --check src tests tools bench examples
mypy                                  # strict
pytest
python tools/mutation_gate.py         # if you touched receipt/runner/store/compare/reproduce/router
```

## Rules for changes

- **A new invariant needs an attacker.** Add an integrity scenario (honest arm + attack
  arm detected by a named rule) or a mutant in `tools/mutation_gate.py`, and show that it
  is killed.
- **Never weaken a mutant to make it die.** If a mutant survives, strengthen the test or
  write down why it is equivalent in `EQUIVALENT`.
- **Receipt schema changes** bump `RECEIPT_SCHEMA` and come with a note in
  `docs/RUN_MANIFEST.md`. Store schema changes add a migration plus a migration test.
- No new runtime dependencies without discussion. The core depends on PyYAML only.
- No paid API may be needed for tests or the demo.

## Good first issues

- Import adapter: Inspect AI `EvalLog` → EvaluationReceipt (map `sample.error` to the taxonomy).
- Import adapter: promptfoo JSON output → EvaluationReceipt.
- `evalledger export --mlflow` (log a receipt as run params/artifacts).
- `evalledger show --cases` filters by outcome/tag.
- More scorers: F1/token overlap, JSON Schema validation.
- `evalledger gc` for unreferenced objects in the store.
- Receipt signing (Ed25519) as an opt-in.

Commits: small and coherent, with a message that says *why*.
