# Failure experiments

All experiments ran against the deployed stack in us-west-2 on 2026-10-23 (America/Los_Angeles),
through the public API with throwaway Cognito users, using test-only fault injection
(`scripts/faults.py`, SSM parameter, deployed with `-c faultInjection=true`). Runner:
`scripts/experiments.py`. Raw results: `docs/evidence/exp-*.json` (run 2) and
`docs/evidence/run1-before-txconflict-fix/` (run 1).

Each job uses `fixtures/rows_500.csv` (500 valid rows) unless noted. "Consistent" means, read back
from DynamoDB with consistent reads after the run: `processedCount == expectedCount ==` number of
Results items `==` number of distinct `rowNumber`s, and `validCount`/`invalidCount` match the
Results. Worker settings during experiments: batch size 10, max concurrency 2, 256 MB, timeout 30 s,
WorkQueue visibility 180 s, maxReceiveCount 3; IngestQueue visibility 360 s.

## Summary

| Experiment (spec §11.1) | Run | Final status | Rows counted / results / distinct | Duplicate results | End-to-end |
| --- | --- | --- | --- | --- | --- |
| Duplicate delivery (500 extra messages, new messageIds) | 1 | COMPLETED | 500 / 500 / 500 | 0 | 23.1 s |
| | 2 | COMPLETED | 500 / 500 / 500 | 0 | 29.5 s |
| Crash after commit (rows 3, 50, 120, 333, 499) | 1 | COMPLETED | 500 / 500 / 500 | 0 | 364.7 s ⚠ |
| | 2 | COMPLETED | 500 / 500 / 500 | 0 | 12.9 s |
| Crash halfway through dispatch (after 200 of 500) | 1 | COMPLETED | 500 / 500 / 500 | 0 | 378.7 s |
| | 2 | COMPLETED | 500 / 500 / 500 | 0 | 378.3 s |
| Permanent system failure (rows 5, 250) → DLQ → fix → redrive | 1* | COMPLETED after redrive | 500 / 500 / 500 | 0 | 599.2 s |
| | 2 | COMPLETED after redrive | 500 / 500 / 500 | 0 | 759.1 s |
| Crash after report write (12-row file with 5 invalid) | 2 | COMPLETED_WITH_ERRORS | 12 / 12 / 12 | 0 | 2.1 s |
| Slow downstream (+2 s per worker batch) | 2 | COMPLETED | 500 / 500 / 500 | 0 | 98.6 s |

Across the three duplicate-producing scenarios (duplicate delivery, crash after commit, crash
mid-dispatch) there were **6 runs and 0 duplicate results**; across all 10 experiment runs, 0.
This is a measured count on these runs, not a proof of exactly-once processing. The guarantee is
scoped to DynamoDB results and counters within one run.

\* Run 1 of the permanent-failure experiment: the automated runner stopped midway (a credential
refresh bug in the local script, unrelated to the product). Recovery was then done by hand with the
runbook, which is also recorded.

## Details

### Duplicate delivery
- **Injected:** while the job was running (worker slowed by 300 ms per batch), a second
  `DISPATCH_ROWS` re-sent all 500 rows: same business keys, new SQS messageIds.
- **Observed (run 2):** worker log outcomes for the job: 500 `created`, 500 `duplicate`; counters
  500; Results 500 distinct rows.
- **Run 1 side finding:** the Lifecycle function failed once with `TransactionConflictException`
  while moving the job to `REPORTING` (duplicate worker transactions were touching the same Job
  item). The stream retried from that record and the job completed correctly. Fixed in run 2 by
  retrying that write on conflict.

### Crash after commit
- **Injected:** for 5 rows, the worker commits the transaction and then reports the message as
  failed on its first receive (as if the function died before SQS learned of the success).
- **Observed (run 2):** 495 `created` + 5 failures on first receive; after the 180 s visibility
  timeout the 5 messages were redelivered and all 5 were recognised as `duplicate` (post-drain
  count). Counters stayed at 500.
- **Run 1 finding (a real bug):** the job took 364.7 s because the importer's final
  `dispatchComplete=true` write failed with `TransactionConflictException` while 500 worker
  transactions updated the same Job item. All rows had been processed within 11.4 s; the job only
  finished when SQS redelivered `DISPATCH_ROWS` after the 360 s visibility timeout and the full
  dispatch ran again (deduplicated). Correctness held; completion latency did not. Fix: retry
  Job-item writes on `TransactionConflictException` with jittered backoff. Run 2: 12.9 s,
  1 dispatch.

### Crash halfway through dispatch
- **Injected:** the dispatcher raises after sending 200 of 500 rows, on the first attempt.
- **Observed:** `dispatchComplete` stayed `false`; SQS redelivered `DISPATCH_ROWS` after the 360 s
  visibility timeout; the second attempt sent all 500 rows; worker outcomes 500 `created` +
  200 `duplicate`; counters 500.
- **Trade-off found:** end-to-end time (~378 s) is dominated by the IngestQueue visibility timeout
  (6 × the 60 s importer timeout). Dispatch of 500 rows takes seconds, so a shorter importer
  timeout (and visibility) would recover faster. Not changed yet.

### Permanent system failure, alarm, fix, redrive
- **Injected:** rows 5 and 250 always fail with a system error.
- **Observed (run 2):** each failed 3 times (6 row failures logged), then both were in WorkDLQ
  564 s after the run started. The job stayed `PROCESSING` at 498/500 (never marked complete).
  `catalogbridge-WorkDLQNotEmpty` went to ALARM 183 s after both messages were in the DLQ (SQS
  metrics arrive with delay); `catalogbridge-WorkQueueOldestAge` also alarmed. Only then was the
  cause removed and `recover.py redrive work --max 10` run: 2 messages moved back with their
  original bodies (same run identity), job COMPLETED, counters 500.
- **Partial batch response, live:** rows 5 and 250 were enqueued among healthy rows and consumed
  with worker batch size 10 (exact batch membership was not logged).
  All 498 other rows were written on their first receive and the worker logged 0 `duplicate`
  outcomes for the job, so batch-mates of the failing rows were not reprocessed: only the failed
  message IDs went back to SQS.
- **Not verified:** email delivery of the alarm. The SNS email subscription was still
  "PendingConfirmation" at that time, so the alarm fired but no email could be delivered.
- **Run 1 (manual recovery):** the stuck job was found with `recover.py status` (498/500,
  9 min without progress), `inspect` listed missing rows [5, 250], `peek work` showed both in the
  DLQ, the fault was cleared, `redrive` moved 2 messages, and the job completed 2.3 s later. The
  DLQ alarm had not fired yet in run 1 because the redrive happened ~2 min after the messages
  reached the DLQ, before the SQS metric arrived; run 2 waited for the alarm.

### Crash after report write
- **Injected:** Lifecycle writes both report files, then crashes before the final status update
  (once per job).
- **Observed:** the stream batch was retried from the failed record (1 lifecycle failure logged);
  the retry found the job in `REPORTING`, rebuilt the same deterministic report keys, and set
  `COMPLETED_WITH_ERRORS` (7 valid, 5 invalid). Byte-identical report output on re-run is covered
  by the local test `test_crash_after_report_write_recovers_same_report`; the live run did not
  compare bytes.

### Slow downstream
- **Injected:** each worker batch sleeps 2 s.
- **Observed:** job took 98.6 s; worker ConcurrentExecutions maximum stayed at 2 (the mapping's
  max concurrency) and WorkQueue oldest-message age peaked at 93 s, i.e. the backlog was visible
  and concurrency stayed capped instead of fanning out.

## Other observations

- In run 1 (duplicate-delivery window), worker `ConcurrentExecutions` showed a 1-minute maximum of
  3 while the mapping's max concurrency was 2. All run 2 windows showed at most 2. The AWS docs
  reviewed do not explain this; it might be a metric-aggregation artifact, but that is not
  verified.
- `TransactionConflict` retries inside the worker are frequent because every row updates the same
  Job item (e.g. 40 retries for 1,000 row messages in run 1's duplicate experiment). This is the
  known hot spot (decisions D7); benchmark-results.md has counts per configuration.
