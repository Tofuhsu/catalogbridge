# Benchmark results

Measured 2026-10-23 (America/Los_Angeles) against the deployed stack. Runner:
`scripts/benchmark.py`; raw data: `docs/evidence/bench-<config>-*.json`.

## Setup (fixed across configurations)

| Item | Value |
| --- | --- |
| Region | us-west-2 |
| Dataset | `fixtures/rows_500.csv` (500 valid rows, product limit per job) |
| Load per run | 4 independent 500-row jobs started together = 2,000 rows |
| Runs per configuration | 3, sequential, in one window per configuration |
| Worker | Python 3.13, arm64, 256 MB, timeout 30 s, same code (CodeSha256 `jheCYgbI...` in all runs) |
| Retry settings | WorkQueue visibility 180 s, maxReceiveCount 3; worker retries `TransactionConflict` up to 5 times in-process |
| Varied | Worker SQS mapping `batchSize` (1, 10) and `maximumConcurrency` (2, 5), changed with `cdk deploy -c ...` |
| Warm/cold | Not separated. Changing the mapping does not redeploy function code, so run 1 of a configuration is not guaranteed cold; numbers include whatever cold starts occurred. |
| Stop conditions | Max 2,000 rows per run; abort a run if any job is not terminal after 15 minutes (never triggered) |

Metric definitions:

- **rows/s** = 2,000 / (time of the last row's result − earliest run request of the 4 jobs).
  This is end-to-end and includes dispatch, queueing, batching and retries, so a single delayed row
  lowers it sharply.
- **Job end-to-end** = `completedAt − runRequestedAt` per job (12 jobs per configuration).
- Counts of transaction conflict retries, row system failures and duplicates come from the
  worker's CloudWatch EMF metrics over the configuration's window.

## Results

| Batch | Max conc. | rows/s per run | Median rows/s | Job e2e p50 | Job e2e p95 | Job e2e max | Tx conflict retries | Row system failures | Duplicate results | Worker invocations |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2 | 36.9, 37.5, 37.4 | 37.4 | 42.5 s | 55.0 s | 55.6 s | 228 | 0 | 0 | 6,000 |
| 10 | 2 | 53.2, 59.0, 53.9 | 53.9 | 34.3 s | 38.4 s | 38.7 s | 307 | 0 | 0 | 607 |
| 1 | 5 | 9.6, 10.1, 73.4 | 10.1 | 21.5 s | 202.4 s | 208.4 s | 1,338 | 2 | 0 | 5,984 |
| 10 | 5 | 91.4, 92.8, 10.1 | 91.4 | 19.8 s | 102.3 s | 199.5 s | 1,083 | 1 | 0 | 617 |

All 12 runs ended with every job consistent (counters == results == 500 per job).

## What the numbers show (and do not show)

1. **Batch 10 vs 1 at max concurrency 2:** 53.9 vs 37.4 median rows/s and 34.3 s vs 42.5 s job
   p50, with ~10x fewer invocations (607 vs 6,000). Batching amortized per-invocation overhead.
   Three runs per configuration is a small sample; this is an observation, not a significance claim.
2. **Max concurrency 5 vs 2:** in clean runs, throughput rose (91–93 rows/s for batch 10, 73 for
   batch 1) and job p50 dropped to ~20 s. But 3 of the 6 concurrency-5 runs contained a row whose
   transaction was cancelled after 5 in-process `TransactionConflict` retries. That row became a
   system failure, waited for the 180 s visibility timeout, and then succeeded (deduplicated),
   so its job took ~200 s and that run's rows/s fell to ~10. Conflict retries grew from 228–307
   (concurrency 2) to 1,083–1,338 (concurrency 5).
3. **Cause:** every row's transaction also updates the same Job item (the counter hot spot in
   decisions D7). More concurrent writers to one item means more conflicts. Raising concurrency
   improved typical latency but made the tail worse; it did not cause wrong results.
4. **Observed but not explained:** the worker's 1-minute `ConcurrentExecutions` maximum reached 3
   (configured 2), 6 and 7 (configured 5). Possible metric-aggregation effect; not verified.

## Capacity estimate vs measurement

A rough model is rows/s ≈ concurrency × effective batch size / batch duration. Here it overstates
throughput because the shared Job item serializes part of every transaction; measured numbers
above are the ones to quote.

## Possible next steps (not done)

- Sharded or per-batch aggregated counters (one Job update per batch instead of per row), or
  derive completion from Results counts, to remove the single-item hot spot.
- Longer/jittered conflict backoff or more in-process attempts before returning a row to SQS.
- Shorter WorkQueue visibility for faster retry of the rare failed row (must stay ≥ 6× timeout).
- Separate cold vs warm runs and more runs per configuration.

## Cost estimate method

Not measured from the bill (Cost Explorer data lags ~24 h). Per 500-row job the main billable
operations are ~500 SQS sends + ~500 receives/deletes (batched), ~500 DynamoDB transactions
(2 items each), 50–500 worker invocations at 256 MB for well under a second each, and 2 S3 report
puts. At on-demand list prices this is a small fraction of one US cent per job; the US$20 budget
alarm is the real guardrail.

Observed: on 2026-10-25, AWS Budgets and Cost Explorer reported US$0.00 month-to-date (marked
estimated) after deployment, all experiments, the benchmarks and the AI evaluation. This is likely
new-account free tier; Bedrock (Anthropic) usage is billed via AWS Marketplace and can appear later.
Not a claim that the system runs for free.
