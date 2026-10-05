# Performance

How much does Astrometron itself cost, separately from the model? These are measured numbers
from `bench/bench_overhead.py`. They are not tuned and not cherry-picked. The raw output
is in [`eval/results/bench.json`](../eval/results/bench.json). Re-run it on your
machine:

```
python bench/bench_overhead.py --cases 1000 --reps 30
```

**Machine:** Apple M1, 8 cores, 16 GiB RAM, macOS 27 (Darwin 27.0.0, arm64), CPython
3.12.13, Astrometron 0.1.0. Dataset: 1,000 cases. The mock provider has a 2% injected
infra error rate.

## Per case

| Measurement | n | p50 | p95 | p99 |
|---|---|---|---|---|
| Astrometron overhead per case (total − model − tool − scoring), zero-latency provider | 1000 | 0.018 ms | 0.022 ms | 0.042 ms |
| Model latency, 5 ms provider (concurrency 8) | 1000 | 6.300 ms | 6.756 ms | 7.405 ms |
| Astrometron overhead, same run | 1000 | 0.077 ms | 0.138 ms | 0.267 ms |

Under concurrency the per-case overhead rises (GIL contention between worker threads),
but it stays roughly 1–2% of even a 5 ms model call. Real provider calls take hundreds
of milliseconds. The per-case figure is measured inside the case. The check that each
finished record can be canonically serialised runs after the timing is recorded, so it
appears only in the throughput numbers below.

## Per run (1,000-case receipt)

| Operation | n | p50 | p95 | p99 |
|---|---|---|---|---|
| Aggregate computation | 30 | 2.55 ms | 2.61 ms | 2.80 ms |
| Receipt generation (`seal`: three digests over the full receipt) | 30 | 17.6 ms | 17.9 ms | 18.0 ms |
| Receipt self-verification | 30 | 21.5 ms | 21.8 ms | 22.5 ms |
| Full store verification (receipt + index columns + dataset + selection) | 10 | 41.1 ms | 44.7 ms | 44.7 ms |
| Comparison of two receipts | 30 | 44.6 ms | 46.8 ms | 48.8 ms |
| Dataset load + digest (JSONL) | 30 | 15.4 ms | 25.8 ms | 51.7 ms |
| Dataset snapshot verification | 30 | 12.3 ms | 13.0 ms | 16.5 ms |
| Persistence (atomic write + fsync + index transaction) | 30 | 30.4 ms | 32.5 ms | 33.1 ms |

Receipt size: about 1.55 MB per 1,000 cases with outputs and trajectories recorded.

## Throughput (zero-latency provider, everything included)

| Concurrency | Runs | Median wall time | Cases/s |
|---|---|---|---|
| 1 | 5 | 0.178 s | 5,619 |
| 4 | 5 | 0.175 s | 5,704 |

This includes dataset loading, snapshot registration, execution, per-case record
validation, sealing, self-verification and durable persistence. With a zero-latency
provider the work is CPU-bound in one interpreter, so concurrency barely helps. With
real providers, concurrency hides network latency.

Before the review fixes the measured figure was about 6,300 cases/s. Per-case record
validation (finding HIGH-3: one non-finite value must not abort a whole run) costs about
11% of throughput when the provider takes zero time. With any real provider the cost is
negligible.

## Caveats

- One machine, one run of the benchmark. Expect ±10–20% between runs.
- Timings are wall clock (`time.perf_counter`).
- The 5 ms mock uses `time.sleep`, which overshoots on macOS. That shows up as model
  latency, not ledger overhead, which is the point of keeping them separate.
