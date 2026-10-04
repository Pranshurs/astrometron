# Performance

How much does the ledger cost, separately from the model? These are measured numbers
from `bench/bench_overhead.py`. They are not tuned and not cherry-picked. The raw output
is in [`eval/results/bench.json`](../eval/results/bench.json). Re-run it on your
machine:

```
python bench/bench_overhead.py --cases 1000 --reps 30
```

**Machine:** Apple M1, 8 cores, 16 GiB RAM, macOS 27 (Darwin 27.0.0, arm64), CPython
3.12.13, EvalLedger 0.1.0. Dataset: 1,000 cases. The mock provider has a 2% injected
infra error rate.

## Per case

| Measurement | n | p50 | p95 | p99 |
|---|---|---|---|---|
| Ledger overhead per case (total − model − tool − scoring), zero-latency provider | 1000 | 0.018 ms | 0.022 ms | 0.043 ms |
| Model latency, zero-latency mock | 1000 | 0.004 ms | 0.008 ms | 0.009 ms |
| Model latency, 5 ms provider (concurrency 8) | 1000 | 6.312 ms | 6.948 ms | 8.126 ms |
| Ledger overhead, same run | 1000 | 0.075 ms | 0.166 ms | 0.270 ms |

Under concurrency the per-case overhead rises (GIL contention between worker threads),
but it stays roughly 1–2% of even a 5 ms model call. Real provider calls take hundreds
of milliseconds.

## Per run (1,000-case receipt)

| Operation | n | p50 | p95 | p99 |
|---|---|---|---|---|
| Aggregate computation | 30 | 2.27 ms | 2.63 ms | 2.77 ms |
| Receipt generation (`seal`: three digests over the full receipt) | 30 | 18.3 ms | 18.6 ms | 18.7 ms |
| Receipt self-verification | 30 | 22.0 ms | 22.8 ms | 23.3 ms |
| Full store verification (receipt + index + dataset + selection) | 10 | 41.9 ms | 46.4 ms | 46.4 ms |
| Comparison of two receipts | 30 | 44.6 ms | 45.8 ms | 49.4 ms |
| Dataset load + digest (JSONL) | 30 | 15.2 ms | 15.6 ms | 19.9 ms |
| Dataset snapshot verification | 30 | 12.5 ms | 12.9 ms | 16.8 ms |
| Persistence (atomic write + fsync + index transaction) | 30 | 30.5 ms | 31.2 ms | 31.3 ms |

Receipt size: about 1.55 MB per 1,000 cases with outputs and trajectories recorded.

## Throughput (zero-latency provider, everything included)

| Concurrency | Runs | Median wall time | Cases/s |
|---|---|---|---|
| 1 | 5 | 0.159 s | 6,305 |
| 4 | 5 | 0.151 s | 6,644 |

This includes dataset loading, snapshot registration, execution, sealing,
self-verification and durable persistence. With a zero-latency provider the work is
CPU-bound in one interpreter, so concurrency barely helps. With real providers,
concurrency hides network latency.

## Caveats

- One machine, one run of the benchmark. Expect ±10–20% between runs.
- Timings are wall clock (`time.perf_counter`).
- The 5 ms mock uses `time.sleep`, which overshoots on macOS. That shows up as model
  latency, not ledger overhead, which is the point of keeping them separate.
