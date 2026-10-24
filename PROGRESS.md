# PROGRESS

Truthful log. Code in this repository is AI-assisted (Claude Code) and reviewed by the author.
Unchecked items are not done. Nothing here is claimed until it is verified.

## 2026-10-23 (Day 1)

Decisions confirmed by the author: us-west-2, private GitHub repo, US$20 budget alarm, no NAT
Gateway / RDS / MSK, alert email set at deploy time. The author asked for the repository to read
as a general AWS engineering project (no interview/team framing), and the provisioned-poller
experiment was dropped.

Done and verified:
- [x] Backend (API, importer, worker, lifecycle, stall check) with 53 local tests (pytest + moto),
      including duplicates, crash-after-commit, mid-dispatch crash, partial batch failure, report
      crash recovery, isolation. A deliberate mutation (removing the dedup condition) fails 5 tests.
- [x] CDK stack + 10 template assertion tests; frontend (Vue 3) builds; CI green on GitHub Actions.
- [x] Deployed to AWS (account bootstrapped by CDK). Public URL: https://d394jsxt8i7q8r.cloudfront.net
- [x] Live end-to-end: 16/16 checks (docs/evidence/e2e-20261023T203623.json), incl. 500-row job,
      business errors, corrected job, structural rejections, 1 MB S3 policy, A/B isolation (404 on
      all 8 routes), no-token 401, idempotent create.
- [x] Live AI path: one real Bedrock suggestion mapped all 4 fields of `synonyms.csv` correctly
      (one call, not an evaluation). Injected AI outage → 503 + manual mapping completes the job.
- [x] Failure experiments run 1 (docs/evidence/run1-before-txconflict-fix/): duplicates, crash
      after commit, crash mid-dispatch, permanent failure → DLQ → manual redrive. All ended with
      counters == results == 500.

Bugs found by running on real AWS (fixed):
1. Presigned POST used the global S3 endpoint → HTTP 307 for a new us-west-2 bucket. Fixed:
   regional endpoint + SigV4; regression test added.
2. Non-transactional writes to the Job item (`dispatchComplete=true`, `→ REPORTING`) failed with
   `TransactionConflictException` while worker transactions touched the same item. Correctness held
   (SQS/stream retry + dedup) but completion was delayed ~6 min (visibility timeout). Fixed: retry
   with jitter on that error; experiments re-run to verify.

Blocked / waiting on the author:
- Bedrock Anthropic "use case details" form must be submitted in the console before the AI
  evaluation can run (Bedrock currently returns ResourceNotFoundException for Claude models).
- GitHub OIDC deploy role (`CatalogBridgeCicd` stack) creates IAM resources; needs the author's
  explicit go-ahead or to be run by the author.
- SNS email subscription confirmation (click the link in the email).

Not done yet:
- [ ] Failure experiments run 2 (after fix), incl. DLQ alarm observed before recovery.
- [ ] Benchmark batch 1 vs 10, max concurrency 2 vs 5.
- [ ] AI mapping evaluation on the fixed test set.
- [ ] Real user validation (none so far; synthetic only).
- [ ] Final docs with measured numbers; switch final demo deployment to faultInjection=false.

Known limits observed: account Lambda concurrency quota is 10 (no reserved concurrency possible);
the author is signed in as the AWS root user (recommend an Identity Center admin user).
