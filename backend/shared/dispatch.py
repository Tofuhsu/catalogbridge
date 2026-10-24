"""DISPATCH_ROWS: fan out one WorkQueue message per manifest row.

Correctness rules (spec §6.2):
- Input is the frozen manifest (fixed S3 version) + frozen mapping of the run.
- Every SendMessageBatch entry is checked individually; a 200 response can
  still contain per-entry failures.
- dispatchComplete=true is written only after every row was accepted by SQS.
  If we crash halfway, the IngestQueue message is retried and we re-send all
  rows; the Worker's business-key dedup makes the re-sent rows no-ops.
"""

import json
import os
import time

from shared import faults, obs, rules, schema
from shared.aws import client, jobs_table
from shared.ddbutil import get_job, is_conditional_fail, now_ms

SEND_ATTEMPTS = 4


class DispatchError(Exception):
    """Some rows could not be enqueued; the whole dispatch will be retried."""


def load_manifest(job: dict) -> dict:
    body = client("s3").get_object(Bucket=os.environ["WORK_BUCKET"], Key=job["manifestKey"])["Body"].read()
    return json.loads(body)


def build_work_items(job: dict, manifest: dict) -> list:
    headers = manifest["headers"]
    mapping = job["frozenMapping"]
    projected = [(r["rowNumber"], rules.project_row(r["values"], headers, mapping)) for r in manifest["rows"]]
    dups = rules.find_duplicate_skus(projected)
    items = []
    for row_number, source in projected:
        items.append(
            {
                "v": 1,
                "jobId": job["jobId"],
                "runRequestId": job["runRequestId"],
                "fileVersion": job["fileVersion"],
                "rowNumber": row_number,
                "currency": job["currency"],
                "source": source,
                "duplicateOf": dups.get(row_number),
                "runRequestedAt": int(job["runRequestedAt"]),
            }
        )
    return items


def _send_chunk(sqs, queue_url, chunk):
    """Send up to 10 items; retry only the entries SQS reported as failed."""
    pending = {str(it["rowNumber"]): it for it in chunk}
    for attempt in range(SEND_ATTEMPTS):
        resp = sqs.send_message_batch(
            QueueUrl=queue_url,
            Entries=[{"Id": k, "MessageBody": json.dumps(v)} for k, v in pending.items()],
        )
        for ok in resp.get("Successful", []):
            pending.pop(ok["Id"], None)
        failed = resp.get("Failed", [])
        if not pending:
            return
        obs.warn("send_message_batch partial failure", attempt=attempt + 1,
                 failed=[{"id": f["Id"], "code": f.get("Code"), "senderFault": f.get("SenderFault")} for f in failed])
        obs.metric("DispatchEntryFailures", len(failed))
        if any(f.get("SenderFault") for f in failed):
            break  # our request is wrong; retrying the same entry will not help
        time.sleep(min(2.0, 0.1 * (2 ** attempt)))
    raise DispatchError(f"{len(pending)} row(s) not enqueued: {sorted(int(k) for k in pending)[:10]}")


def dispatch(job_id: str, run_request_id: str, row_numbers=None, receive_count: int = 1) -> str:
    """Returns a short outcome string (for logs/tests). Raises to request retry."""
    started = time.time()
    job = get_job(job_id)
    if not job or job.get("runRequestId") != run_request_id:
        obs.warn("dispatch for unknown job/run; dropping", jobId=job_id, runRequestId=run_request_id)
        return "dropped"

    status = job["status"]
    if status == schema.RUN_REQUESTED:
        try:
            jobs_table().update_item(
                Key={"jobId": job_id},
                UpdateExpression="SET #s = :p, processingStartedAt = :t, updatedAt = :t",
                ConditionExpression="#s = :rr AND runRequestId = :r",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":p": schema.PROCESSING, ":rr": schema.RUN_REQUESTED,
                                           ":r": run_request_id, ":t": now_ms()},
            )
        except Exception as e:
            if not is_conditional_fail(e):
                raise
        job = get_job(job_id)
        status = job["status"]

    if status != schema.PROCESSING:
        obs.info("dispatch skipped; job not processing", jobId=job_id, status=status)
        return "skipped"
    if job.get("dispatchComplete") and row_numbers is None:
        obs.info("dispatch already complete; duplicate notification", jobId=job_id, runRequestId=run_request_id)
        obs.metric("DuplicateDispatchSkipped")
        return "already_complete"

    items = build_work_items(job, load_manifest(job))
    if row_numbers is not None:
        wanted = set(int(n) for n in row_numbers)
        items = [it for it in items if it["rowNumber"] in wanted]

    sqs = client("sqs")
    queue_url = os.environ["WORK_QUEUE_URL"]
    crash_after = faults.get("importer").get("crash_after_sent")
    sent = 0
    for i in range(0, len(items), 10):
        _send_chunk(sqs, queue_url, items[i:i + 10])
        sent += len(items[i:i + 10])
        if crash_after is not None and receive_count == 1 and sent >= int(crash_after):
            obs.warn("FAULT: crash during dispatch", jobId=job_id, sent=sent)
            raise faults.InjectedFault(f"crash after dispatching {sent} rows")

    if row_numbers is None:
        jobs_table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET dispatchComplete = :true, dispatchedAt = :t, updatedAt = :t, dispatchAttempts = :n",
            ConditionExpression="runRequestId = :r",
            ExpressionAttributeValues={":true": True, ":t": now_ms(), ":r": run_request_id, ":n": receive_count},
        )
    obs.info("dispatch done", jobId=job_id, runRequestId=run_request_id, stage="dispatch", rows=sent,
             partial=row_numbers is not None, durationMs=int((time.time() - started) * 1000), attempt=receive_count)
    obs.metric("RowsDispatched", sent)
    return "dispatched"
