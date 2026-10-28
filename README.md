# CatalogBridge

Turn supplier product CSVs with different column names (English, Chinese, synonyms) into one
validated catalog file, with a row-level error report you can fix and re-run.

- **Live demo (invited accounts only):** https://d394jsxt8i7q8r.cloudfront.net
- **Status:** working MVP deployed on AWS (us-west-2). A demo/learning project, not a commercial
  service: no SLA, no real users yet, no market validation (see [Known limits](#known-limits)).
- **AI-assisted:** code and docs were written with an AI coding assistant (Claude Code) and
  reviewed, run and verified by the author.

## What it does

1. Sign in (Cognito, invited accounts only).
2. Upload a CSV (UTF-8, BOM ok, ≤1 MB, ≤500 rows). Structural problems (empty, duplicate/missing
   headers, ragged rows, wrong encoding, too many rows) are rejected with reasons before any work.
3. Preview, then map source columns to `sku, title, price, stock`: manually, from a saved
   supplier template, copied from a previous job, or suggested by Amazon Bedrock (optional; manual
   always works). Currency is a job setting (no exchange-rate conversion).
4. Run. Rows are validated (sku, title, price required; decimal price ≥ 0 with currency precision;
   stock optional, but if present an integer ≥ 0; duplicate SKUs in the file). Progress updates live.
5. Download the standard CSV (`catalogbridge.v2`) and an error report. Fix invalid rows in the
   browser and create a **corrected job** linked to the original.

## Architecture

S3 (versioned, private) → EventBridge → SQS → Lambda importer → SQS (one message per row) →
Lambda worker → DynamoDB; a DynamoDB Streams consumer drives dispatch and report generation.
HTTP API + Cognito JWT authorizer, Vue SPA on CloudFront, CloudWatch alarms/dashboard → SNS,
CDK (TypeScript) for everything, GitHub Actions with OIDC.

Details: [docs/architecture.md](docs/architecture.md) · decisions and trade-offs:
[docs/decisions.md](docs/decisions.md)

Reliability properties (each verified, see evidence):

- **Idempotent row processing:** result write + job counters in one DynamoDB transaction keyed by
  `jobId#runRequestId#fileVersion#rowNumber`; duplicates, re-dispatch, concurrent workers and
  crash-after-commit do not double count.
- **No lost runs:** the API only records a durable run request; the table stream turns it into
  work (outbox pattern). Dispatch marks itself complete only after every row was accepted by SQS.
- **Partial batch failure:** only failing rows are retried; business-invalid rows are results,
  not retries. Poison rows go to a DLQ, raise an alarm, and the job is never marked complete.
- **Recoverable:** stalled-job detection, `scripts/recover.py` (inspect / redispatch missing rows /
  finalize / DLQ redrive) keeps the original run identity.
- **Isolation:** owner from the verified token only; every job/template access checks ownership.

## Evidence

All numbers come from runs against the deployed stack; raw JSON is in [docs/evidence/](docs/evidence/).

| Area | Document |
| --- | --- |
| End-to-end product checks | [docs/evidence/](docs/evidence/) `e2e-*.json` |
| Failure experiments | [docs/failure-experiments.md](docs/failure-experiments.md) |
| Throughput / latency | [docs/benchmark-results.md](docs/benchmark-results.md) |
| AI mapping evaluation | [docs/ai-mapping-eval.md](docs/ai-mapping-eval.md) |
| Browser UI test | [docs/evidence/manual-ui-test-20261023.md](docs/evidence/manual-ui-test-20261023.md) |
| User validation | [docs/user-validation.md](docs/user-validation.md) |
| Operations | [docs/runbook.md](docs/runbook.md) |

## Repository layout

```
backend/    Python 3.13 Lambdas: api/ importer/ worker/ lifecycle/ shared/ + tests/ (pytest + moto)
frontend/   Vue 3 + TypeScript + Vite SPA
infra/      AWS CDK v2 (TypeScript) app + template assertion tests
scripts/    deploy, fixtures, e2e, experiments, benchmark, AI eval, fault switch, recovery
fixtures/   synthetic CSVs only (no real supplier data)
docs/       architecture, decisions, experiments, benchmarks, runbook, evidence/
```

## Run the tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
(cd backend && ../.venv/bin/python -m pytest -q)          # 54 tests, fake AWS (moto)
(cd frontend && npm ci && npm test && npm run build)
(cd infra && npm ci && npm run build && npm test)          # CloudFormation template assertions
```

## Deploy your own copy

Requires Node 22+, Python 3.11+, AWS CLI v2 signed in (`aws login`), and Bedrock access to an
Anthropic Claude Haiku model in the region (optional; without it AI suggestions return
"unavailable" and manual mapping works).

```bash
cd infra && npm ci && npx cdk bootstrap && cd ..
CB_ALERT_EMAIL=you@example.com scripts/deploy.sh
```

Then confirm the SNS email subscription and invite users (see [docs/runbook.md](docs/runbook.md)).
Tear down with `cd infra && npx cdk destroy CatalogBridge`.

## API

All routes require `Authorization: Bearer <Cognito ID token>`.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | /jobs | Create job (idempotent per `clientToken`), returns presigned upload form |
| GET | /jobs | List own jobs (cursor pagination) |
| GET | /jobs/{id} | Status, counters, progress, allowed actions |
| GET | /jobs/{id}/preview | Headers + first 20 rows (`?all=1` for all, used for corrections) |
| POST | /jobs/{id}/mapping-suggestion | Bedrock suggestion (stored once per job, max 3 attempts) |
| PUT | /jobs/{id}/mapping | Save draft mapping (optimistic concurrency on `mappingVersion`) |
| POST | /jobs/{id}/run | Freeze mapping and request the run (idempotent per `runRequestId`) |
| GET | /jobs/{id}/results | Row results, filter `status=VALID|INVALID` |
| GET | /jobs/{id}/downloads | Short-lived links: output, errors, original |
| GET/PUT | /mapping-templates[/{id}] | Own supplier mapping templates |

## Known limits

- Single region, invited accounts, one file per job, ≤500 rows / 1 MB, CSV only, no product
  variants (variation rows are exported as independent products), no direct store integration,
  no exchange-rate conversion.
- Counters live on one Job item; batching reduced writes to it (decisions D19, measured in
  benchmark-results.md), but very large files would still need sharded counters.
- The dedup guarantee covers DynamoDB results and counters only, not external side effects.
- The account's Lambda concurrency quota is 10, so reserved concurrency is not configured.
- No Shopify adapter (not validated against a real store, so not claimed).
- No external users yet. Checked instead on synthetic data, two public platform sample files
  (WooCommerce, Magento) and the author's own browser test (see [docs/user-validation.md](docs/user-validation.md)).
- AI mapping was evaluated only on 8 synthetic files (32/32 decisions matched the key); real
  supplier files are untested.
- Recovery from a crash mid-dispatch waits for the ingest queue visibility timeout (180 s; measured
  ~193 s end to end).
