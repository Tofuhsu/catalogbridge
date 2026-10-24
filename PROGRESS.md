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

Later the same day:
- [x] Failure experiments run 2 (after the fix): 6/6 consistent; crash-after-commit 364.7 s -> 12.9 s;
      DLQ alarm reached ALARM 183 s after messages hit the DLQ, then fix + redrive completed the job.
      docs/failure-experiments.md
- [x] GitHub OIDC deploy role deployed (author approved). First CI deploy failed: the repo uses
      GitHub's immutable OIDC subject (`repo:owner@id/repo@id`); trust policy fixed; CI deploy
      succeeded (run 40064143235, docs/evidence/ci-oidc-deploy-run-40064143235.log).
- [x] Benchmarks: batch 1 vs 10 x max concurrency 2 vs 5, 3 runs x 2,000 rows each.
      docs/benchmark-results.md. Concurrency 5 exposed the Job-item hot spot as ~200 s tail jobs.

Human check (2026-10-23, by the author, not a target user):
- [x] Invited account signed in through the Cognito Hosted UI in a browser (user status CONFIRMED).
      The first invite email was confusing (one-line body; {username} shows the internal ID because
      email is the sign-in attribute) and the author got "incorrect password"; template fixed and
      re-sent, then sign-in worked.
- [x] One browser upload (117 rows) went through S3 -> EventBridge -> validation -> AWAITING_MAPPING.
- [ ] The rest of the flow (mapping, run, download, corrected job) has no run recorded under the
      author's account; it is verified only by the automated e2e script.

Blocked / waiting on the author:
- Bedrock Anthropic "use case details" form (AI evaluation cannot run until then).
- SNS email subscription still PendingConfirmation (alarms fire but cannot email).
- Real user validation (none so far; synthetic only).

Not done yet:
- [ ] AI mapping evaluation on the fixed test set (docs/ai-mapping-eval.md says "not run").
- [ ] Switch the final demo deployment to faultInjection=false (after the AI checks).

Known limits observed: account Lambda concurrency quota is 10 (no reserved concurrency possible);
the author is signed in as the AWS root user (recommend an Identity Center admin user).
