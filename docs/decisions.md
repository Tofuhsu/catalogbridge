# Decisions

Short records of choices and their trade-offs. "Why not X" entries are honest about cost, not
claims that X is worse in general.

## D1. Asynchronous pipeline for a 500-row product limit
A single synchronous Lambda could process 500 rows. The queue-based design is chosen to decouple
upload/response from processing, to bound work with concurrency limits, to recover each stage
independently, and (deliberately) to practice idempotent event processing. Cost: more components,
more states, a few seconds of extra latency, more to operate. If volumes stayed tiny and simple, a
synchronous design would be reasonable.

## D2. SQS Standard (not FIFO, Kinesis or Kafka)
Rows are independent; there is no ordering requirement, and the work is "distribute and retry",
which SQS does with per-message visibility and DLQs. FIFO would add throughput limits and message
group design for no benefit. A stream (Kinesis/Kafka) would make sense for multiple independent
consumers with replay or per-key ordering.

## D3. DynamoDB (not a relational database)
Access is by known keys (job, job+row, owner+createdAt), and the core correctness tool is a
conditional transaction. On-demand billing and no servers fit an idle-most-of-the-time demo.
Costs: no joins/ad-hoc queries, careful index design, and the Job counter hot spot (D7).

## D4. Durable run request + stream instead of "API writes DB then enqueues"
Writing DynamoDB and SQS from the API is a dual write: a crash between them loses the run. The API
writes only the run request; the Jobs stream turns the committed change into a dispatch message
(transactional outbox via change data capture). Streams can still redeliver, so dispatch is
idempotent and a stalled-job detector + recovery script exist. Stream records expire after 24 h, so
recovery must not rely on the stream alone.

## D5. Business key, not SQS messageId, for dedup
`jobId#runRequestId#fileVersion#rowNumber` is stable across redelivery and re-dispatch. A re-sent
row gets a new messageId, so messageId-based dedup would double count.

## D6. Result + counter in ONE transaction
Separate writes allow "result written, counter not" (or the reverse). A transaction makes both
happen or neither. On cancellation the worker inspects `CancellationReasons` and proves a duplicate
with a consistent read; any other cancellation (conflict, throttling, wrong job state) is a
retryable system failure.

## D7. Known hot spot: every row updates the same Job item
Fine for 500 rows at low concurrency; transaction conflicts appear as concurrency rises (measured,
see benchmark-results.md). Larger scale would need sharded counters, per-batch aggregation, or
completion tracking derived from Results instead of a single counter.

## D8. Business errors are results, system errors are retries
A negative price will never become valid by retrying, so it is committed as INVALID. Only system
failures go into `batchItemFailures`. Per-record exception handling keeps one bad row from failing
the whole batch.

## D9. Pin the uploaded object version
The raw bucket is versioned; the job binds to the event's versionId and always reads that version.
Re-uploading to the same key cannot silently change a job's input. A new file means a new job.

## D10. Corrected data = new job with parentJobId
Editing results inside an already-run job would require reversing counters and would make reports
ambiguous. A corrected job is a fresh, fully auditable run that links to its parent.

## D11. AI is optional and bounded
Bedrock structured output constrains the answer's shape (a static JSON schema so the compiled
grammar is cached); the server still validates every suggested column, drops invalid ones, and the
user must confirm. One stored suggestion per job, max 3 attempts, 12 s read timeout, 2 SDK
attempts, manual mapping always available. InvokeModel with the Anthropic body is used because it
works with the boto3 bundled in the Lambda runtime (no extra dependency packaging).

## D12. Fault injection is test-only
Faults are read from an SSM parameter that exists only when deployed with
`-c faultInjection=true`; there is no API to enable them. The final demo deployment sets it off.

## D13. Throughput settings are CDK context
`workerBatchSize`, `workerMaxConcurrency`, `workerBatchWindowSeconds` are context values so each
benchmark configuration is a reproducible `cdk deploy` rather than a console edit.

## D14. Scope trims
- SQS provisioned-poller experiment: dropped. It costs extra, and at 500-row jobs there is little
  to learn beyond batch size / max concurrency.
- Shopify output adapter: not built. Without validation in a real dev store it could only be a
  "candidate" format, and the standard schema comes first.
- No reserved concurrency: the account's concurrency quota is 10 (new account default), and Lambda
  requires 10 unreserved, so nothing can be reserved. Per-mapping `maxConcurrency` is used instead.

## D15. Region and cost guardrails
us-west-2; US$20/month AWS Budget alert (50% and 80% actual, 100% forecast). No NAT gateway, RDS,
MSK, EKS or EC2. Budgets update with delay, so the alert is a warning, not a kill switch.

## D16. CI/CD with GitHub OIDC
GitHub Actions assumes an IAM role through OIDC; the trust policy allows only
`repo:Tofuhsu/catalogbridge:ref:refs/heads/main`. The role can only assume the CDK bootstrap roles.
No AWS access keys are stored in GitHub.
