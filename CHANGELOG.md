# Changelog

## 0.1.0 (unreleased)

First local release candidate. Not yet published.

- Content-addressed datasets (JSONL, CSV, Python) with stable case identity.
- Typed outcome taxonomy, denominator-preserving runner, sealed EvaluationReceipt v1.
- Local store: atomic writes, content-addressed objects, SQLite index (schema v2).
- `run`, `show`, `verify`, `compare`, `reproduce`, `runs`, `datasets`, `route`, `selfcheck`.
- Providers: deterministic mock, Python callable, OpenAI-compatible HTTP.
- Scorers: exact, normalized, regex, numeric, json_fields, classification, python, judge.
- Tool/agent trajectory evidence; `tool_loop` solver.
- Evidence-backed router with fail-closed gates.
- 32-scenario adversarial integrity suite; 48-mutant mutation gate.
