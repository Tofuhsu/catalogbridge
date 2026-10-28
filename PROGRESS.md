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
- [x] Full browser UI pass in the author's signed-in session (docs/evidence/manual-ui-test-20261023.md):
      mapping checks, templates, AI unavailable + limit, run, error table, corrected job (11/0), drop
      rows, phone width. Not run: sign-out by the assistant, alert email (SNS unconfirmed).
- [x] Usability fix from the author's test: they mapped every field one column off (sku <- Brand) and
      only saw it after running (1 valid / 14 invalid). The mapping table now shows sample values and
      warns when the sku column repeats.

Blocked / waiting on the author:
- Bedrock Anthropic "use case details" form (AI evaluation cannot run until then).
- SNS email subscription still PendingConfirmation (alarms fire but cannot email).
- Real user validation (none so far; synthetic only).

Not done yet:
- [ ] AI mapping evaluation on the fixed test set (docs/ai-mapping-eval.md says "not run").
- [ ] Switch the final demo deployment to faultInjection=false (after the AI checks).

Known limits observed: account Lambda concurrency quota is 10 (no reserved concurrency possible);
the author is signed in as the AWS root user (recommend an Identity Center admin user).

## 2026-10-24

- [x] Fault injection turned off for the demo deployment (`infra/cdk.json`), deployed by CI via OIDC.
- [x] Pre-publication scan: no keys, tokens, passwords or personal email in the repo; the AWS account
      ID in the CI log evidence was replaced with `<account-id>`. The repo is still private.
- [x] Docs final pass: decisions D7 (measured hot spot), D12, D17 (mapping sample values), D18
      (invite email); README known limits.
- Local `aws login` session expired, so the AI evaluation and the cost check wait for the next
  sign-in (plus the Bedrock use-case form).
- Deferred on purpose (would need experiments re-run): shorter importer timeout; per-batch or
  sharded counters for the Job hot spot.

## 2026-10-25

- [x] Anthropic use-case form submitted in the Bedrock console (the author approved every field; the
      assistant filled it in the author's signed-in browser session). Bedrock access worked ~20 s later.
- [x] AI mapping evaluation: 8 synthetic fixtures, 32/32 decisions matched the key, 0 suggestions
      started a run, model latency median 1,565 ms (docs/ai-mapping-eval.md).
- [x] Cost check: Budgets / Cost Explorer show US$0.00 month-to-date (estimated); see
      benchmark-results.md for the caveat.
- [x] Repository made public (author's decision): https://github.com/Tofuhsu/catalogbridge. Full git
      history scanned first: no keys, tokens, passwords or personal email; the AWS account ID remains
      in one old commit of the CI log evidence (account IDs are not secret; history not rewritten).
      CI deploy stays limited to pushes on main, and the OIDC role trusts only this repo's main branch.
- [x] SNS subscription confirmed by the author; test alarm delivered 2 emails (ALARM + OK), 0 failed.
- [x] Sign-out (A3) done by the author in the browser.
- Remaining items that only the author can do (real users, Identity Center admin, deleting test
  data) are written up outside the repo with step-by-step instructions.

## 2026-10-28

- [x] Worker writes one transaction per job group in a batch (N conditional puts + one counter ADD),
      per-row fallback keeps the dedup proof (decisions D19). 3 new tests; a mutation that treats a
      cancelled group as committed fails 2 tests.
- [x] Importer timeout 60 s -> 30 s (IngestQueue visibility 360 s -> 180 s, D21).
- [x] Failure experiments run 3 on the new code: 6/6 consistent, 0 duplicate results (9 runs across
      the three duplicate-producing scenarios so far). Crash mid-dispatch 378 s -> 193 s; slow
      downstream 98.6 s -> 70.9 s.
- [x] Benchmarks v2 (all four configs from a clean worktree at b806f68): batch 10 / conc 2
      53.9 -> 181.5 rows/s, p95 38.4 s -> 13.5 s; batch 10 / conc 5 91.4 -> 393.1 rows/s,
      p95 102.3 s -> 6.9 s; batch 1 unchanged as expected. A benchmark loop that would have deployed
      uncommitted code mid-series was stopped before it executed; the series was re-run cleanly.
- [x] Without recruiting: public WooCommerce and Magento sample files through the live stack. The
      WooCommerce file exposed that "stock required" rejects shops that do not track stock; the author
      decided stock is optional -> catalogbridge.v2 / rules.v2 (D20). Result 22/25 and 14/14; AI 8/8.
- [x] Author self-test protocol (B) and an unseen fixture prepared; not run yet (author's task).
- [x] Final deployment: v2 code, batch 10 / conc 2, fault injection off; live e2e 16/16.
