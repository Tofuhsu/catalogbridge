# Runbook

Operator procedures. All commands run from the repo root with AWS credentials for the account
(`aws login --region us-west-2`) and the dev virtualenv (`python3 -m venv .venv && .venv/bin/pip
install -r backend/requirements-dev.txt "botocore[crt]"`). Scripts read resource names from the
`CatalogBridge` stack outputs.

## Deploy / update

```bash
CB_ALERT_EMAIL=you@example.com scripts/deploy.sh
```

First time only: `cd infra && npx cdk bootstrap aws://<account>/us-west-2`. After deploy, confirm
the SNS subscription email ("AWS Notification - Subscription Confirmation"); alarms do not email
until it is confirmed.

Pushes to `main` also deploy through GitHub Actions (OIDC role, see "CI/CD setup").

## Invite a test user

Self sign-up is disabled. Only invite people who agreed to test:

```bash
aws cognito-idp admin-create-user --user-pool-id <UserPoolId> --username person@example.com \
  --user-attributes Name=email,Value=person@example.com Name=email_verified,Value=true
```

Cognito emails a temporary password; the user sets a new one on first sign-in at the site URL.
Remove access: `aws cognito-idp admin-delete-user --user-pool-id <UserPoolId> --username ...`.

## Alarm → action

| Alarm | Meaning | Do this |
| --- | --- | --- |
| `catalogbridge-WorkDLQNotEmpty` | Rows failed 3 times with a system error | `recover.py peek work` → read errorType in Worker logs → fix cause → `recover.py redrive work --max 10` → watch dashboard |
| `catalogbridge-IngestDLQNotEmpty` | Upload validation / dispatch / finalize message failed 5 times | `recover.py peek ingest`; fix; `redrive ingest` |
| `catalogbridge-EventDeliveryDLQNotEmpty` | EventBridge could not deliver an upload event to SQS (policy/permissions) | Check the queue policy; the job stays `CREATED`. Ask the user to re-create the job after the fix |
| `catalogbridge-LifecycleFailureDLQNotEmpty` | A Jobs-stream batch exhausted retries (contains stream metadata, not the record) | `recover.py status`; for the affected job use `redispatch` (if never dispatched) or `finalize` |
| `catalogbridge-StalledJobs` | A job had no progress for 10 min | `recover.py status`, then `recover.py inspect <jobId>` |
| `catalogbridge-WorkQueueOldestAge` | Backlog older than 5 min | Check Worker throttles/duration/conflicts on the dashboard; slow dependency? |
| `catalogbridge-RowSystemFailures` | ≥5 row-level system failures in a minute (these do NOT show as Lambda Errors) | Worker logs: `stage=worker level=ERROR` |
| `catalogbridge-<Fn>Errors` | Whole invocations failed | Function logs |

A message in a DLQ is never "done". Fix the cause first, then redrive a small number and watch.
Redrive keeps the message body, so the business key (`jobId#runRequestId#fileVersion#rowNumber`)
stays the same and already-written rows are skipped.

## Recovery commands

```bash
.venv/bin/python scripts/recover.py status            # queue/DLQ depth + active/stalled jobs
.venv/bin/python scripts/recover.py inspect <jobId>   # manifest rows vs Results vs counters, missing rows
.venv/bin/python scripts/recover.py redispatch <jobId> # dispatch never completed -> full re-dispatch; else only missing rows
.venv/bin/python scripts/recover.py finalize <jobId>  # resume report for PROCESSING(all rows done)/REPORTING
.venv/bin/python scripts/recover.py peek work|ingest
.venv/bin/python scripts/recover.py redrive work|ingest --max N
```

Decision guide for a stalled job (`inspect` output):

- `dispatchComplete=false` → `redispatch` (re-sends all rows; Worker dedups).
- `missingCount > 0` and nothing in WorkDLQ → `redispatch` (sends only missing rows).
- `missingCount > 0` and rows in WorkDLQ → fix cause, `redrive work`.
- `missingCount = 0`, status `PROCESSING` or `REPORTING` → `finalize`.
- `countersMatchResults=false` → stop and investigate; this should not happen (counters and
  results are written in one transaction).

Retention limits: queues keep messages 4 days, DLQs 14 days, the Jobs stream 24 h. Recovery uses
DynamoDB + the S3 manifest as the source of truth, so it works after stream records expire.

## Fault injection (test only)

Only when deployed with `-c faultInjection=true`:

```bash
.venv/bin/python scripts/faults.py set '{"worker": {"fail_rows": [7]}}'
.venv/bin/python scripts/faults.py clear
```

Available: `api.ai_unavailable`, `worker.fail_rows`, `worker.crash_after_commit_rows`, `worker.slow_ms`,
`importer.crash_after_sent`, `lifecycle.crash_after_report_write`. Always `clear` afterwards.

## CI/CD setup (once)

```bash
cd infra && npx cdk deploy CatalogBridgeCicd   # creates the GitHub OIDC provider + deploy role
gh variable set AWS_DEPLOY_ROLE_ARN --body <DeployRoleArn output>
gh variable set CB_ALERT_EMAIL --body you@example.com
```

## Cost check

```bash
aws ce get-cost-and-usage --time-period Start=$(date -v1d +%Y-%m-%d),End=$(date -v+1d +%Y-%m-%d) \
  --granularity MONTHLY --metrics UnblendedCost --filter '{"Tags":{"Key":"Project","Values":["catalogbridge"]}}'
```

(Cost allocation tags must be activated in Billing to filter by tag; otherwise view the whole
account.)

## Tear down

```bash
cd infra && npx cdk destroy CatalogBridge CatalogBridgeCicd
```

All buckets/tables use `RemovalPolicy.DESTROY` with auto-delete (this is a demo; nothing in it is
meant to be kept). This deletes uploaded files, reports, users and logs. The `CDKToolkit` bootstrap
stack and its asset bucket remain; delete them separately if you no longer use CDK in this
account/region.
