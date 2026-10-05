# Reproducibility

```
astrometron reproduce RUN_ID            # re-execute and compare
astrometron reproduce RUN_ID --no-execute   # only re-identify controlled inputs
```

## What reproduce does

1. **Refuses invalid evidence.** If `verify_run` fails (tampered receipt, crashed run,
   broken dataset binding), the verdict is `REFUSED` and nothing runs.
2. **Loads the dataset snapshot** by revision from the store and verifies it. The
   current file on disk is not used. If it changed, that is reported as a note.
3. **Re-identifies every controlled component** from the receipt's recorded config and
   the code importable now, and compares each one with the recorded identity: provider
   (provider, model, revision), scorer (version, params, code digest, Python function
   digest, judge identity), prompt digest, tool versions and code digests, solver code
   digest.
4. **Notes the environment**: Python version, implementation and machine.
5. **Re-executes** with the same config and seed, stored as a new run with
   `reproduces: <original>`.
6. **Compares** `inputs_digest`, the models the provider reported serving, and every
   case's `(outcome, output_digest)`. With `--repeats N` it re-runs N times and every run
   must agree. One agreeing re-run is weak evidence of determinism, and the report says
   so.

## Verdicts

| Verdict | Meaning |
|---|---|
| `REPRODUCED` | Controlled inputs identical, provider pinned, every case outcome and output identical. |
| `NOT_EXACTLY_REPRODUCIBLE` | The provider's model revision is not pinned. Astrometron prints `EXACT REPRODUCTION NOT POSSIBLE: provider model revision is not externally pinned`, still re-runs every controlled component, and reports agreement. |
| `INPUT_MISMATCH` | A controlled component changed. Each change is listed, for example `scorer changed (function_digest)`. |
| `DIVERGED` | Inputs identical and pinned, but outcomes differ: hidden non-determinism or an undeclared dependency. The differing cases are listed, flagged when every difference involves infra errors. |
| `NO_SCORED_CASES` | Every re-run matched, but the original scored no case (e.g. all outages): there is no model behaviour to reproduce. |
| `REFUSED` | The original receipt does not verify. |
| `VERIFIED_ONLY` | `--no-execute` and no mismatches. |

## Pinning

| Provider | Pinned when |
|---|---|
| `mock` | Always. Behaviour is a pure function of its options, seed and case id. |
| `python` | The config says `pinned: true` and its source can be read. The revision is the function's source digest. The default is unpinned, because the digest pins code, not behaviour. |
| `openai-compat` | Only when the config supplies `revision` (e.g. a GGUF file digest, a dated snapshot name you trust) **and** `pinned: true`. Hosted model names are mutable, so the default is unpinned. |

Note that `served_model` is recorded on every response. If the provider reports a
different model than configured, the receipt carries a `MODEL SUBSTITUTION` warning.

## Staleness

`astrometron verify RUN` reports `STALE` when the config file or the dataset source file
changed after the receipt was generated. The receipt is still a valid record of what
ran. It no longer describes what is on disk now.

## Limits (stated, not hidden)

- Code reachable from a solver, scorer or tool but outside the function's own source is
  only covered by `code.git_commit` and `git_dirty`. A change in a helper module the
  scorer imports does not change `code_digest`. Commit your eval code.
- Hosted providers are not deterministic even at temperature 0 with a seed. Astrometron
  never claims otherwise.
- Library versions other than Astrometron and PyYAML are not recorded in V1.
