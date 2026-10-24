"""Lifecycle Lambda: Jobs table DynamoDB Streams consumer (+ scheduled stall check).

This is the "transactional outbox" half of the design: the API only writes a
durable run request (status RUN_REQUESTED) to DynamoDB; the stream turns that
committed change into a DISPATCH_ROWS message. So there is no window where the
API wrote the DB but crashed before enqueueing.

Stream semantics differ from SQS: records are ordered per item and the
checkpoint is a sequence number. On failure we report the FIRST failed
record's SequenceNumber and stop; Lambda retries from there, so records after
it are processed again (handlers must be idempotent, and are).

The ESM filter only delivers records whose NewImage.status is RUN_REQUESTED or
PROCESSING, so this function's own REPORTING/COMPLETED writes do not loop back.
"""

import json
import os
import time

from boto3.dynamodb.types import TypeDeserializer

from shared import obs, report, schema
from shared.aws import client, jobs_table
from shared.ddbutil import now_ms

_de = TypeDeserializer()


def _image(record, which):
    img = record.get("dynamodb", {}).get(which)
    return {k: _de.deserialize(v) for k, v in img.items()} if img else None


def handle_record(record, finalized: set) -> str:
    new = _image(record, "NewImage")
    old = _image(record, "OldImage") or {}
    if not new:
        return "no_image"
    job_id = new["jobId"]
    status = new.get("status")

    if status == schema.RUN_REQUESTED and old.get("status") != schema.RUN_REQUESTED:
        client("sqs").send_message(
            QueueUrl=os.environ["INGEST_QUEUE_URL"],
            MessageBody=json.dumps({"type": "DISPATCH_ROWS", "jobId": job_id, "runRequestId": new["runRequestId"]}),
        )
        obs.info("dispatch requested", stage="lifecycle", jobId=job_id, runRequestId=new["runRequestId"])
        return "dispatch_sent"

    if (status == schema.PROCESSING and new.get("dispatchComplete")
            and int(new.get("processedCount", 0)) == int(new.get("expectedCount", -1))):
        if job_id in finalized:
            return "finalize_dedup"
        outcome = report.finalize(job_id)  # re-checks on a consistent read
        finalized.add(job_id)
        return "finalize:" + outcome
    return "noop"


def handler(event, context):
    finalized = set()
    records = event.get("Records", [])
    for record in records:
        seq = record.get("dynamodb", {}).get("SequenceNumber")
        try:
            outcome = handle_record(record, finalized)
            if outcome != "noop":
                obs.info("stream record handled", stage="lifecycle", outcome=outcome, sequenceNumber=seq)
        except Exception as e:
            obs.error("stream record failed; checkpointing before it", stage="lifecycle", sequenceNumber=seq,
                      errorType=type(e).__name__, error=str(e)[:300])
            obs.metric("LifecycleFailures")
            return {"batchItemFailures": [{"itemIdentifier": seq}]}
    return {"batchItemFailures": []}


def stall_check(event, context):
    """Scheduled every few minutes: count jobs in an active state with no progress.

    Uses a Scan because it is an operator job over a small MVP table; the user-
    facing job list uses the ownerId GSI instead. Emits StalledJobs (alarmed).
    """
    cutoff = now_ms() - int(os.environ.get("STALL_MINUTES", "10")) * 60_000
    stalled = []
    kwargs = {"ProjectionExpression": "jobId, #s, updatedAt", "ExpressionAttributeNames": {"#s": "status"}}
    while True:
        resp = jobs_table().scan(**kwargs)
        for it in resp.get("Items", []):
            if it.get("status") in schema.ACTIVE_RUN_STATUSES | {schema.VALIDATING_UPLOAD} and int(it.get("updatedAt", 0)) < cutoff:
                stalled.append({"jobId": it["jobId"], "status": it["status"]})
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    obs.metric("StalledJobs", len(stalled))
    if stalled:
        obs.warn("stalled jobs found", stalled=stalled[:20], count=len(stalled))
    return {"stalled": len(stalled), "checkedAt": int(time.time())}
