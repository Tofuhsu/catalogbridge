"""Worker Lambda: WorkQueue (SQS) ESM, one row per message.

Per row, ONE DynamoDB transaction does both:
  1. Put Results[jobId, rowNumber] only if it does not exist yet, and
  2. ADD processedCount/validCount|invalidCount on the Job.
So a row is counted exactly once even if SQS delivers the message twice, two
workers race on it, or we crash after the commit (spec §6.3). The guarantee
covers these DynamoDB writes only, not arbitrary external side effects.

Error classes:
- Business validation failure -> INVALID result, committed, never retried.
- System failure (throttling, conflicts that exhaust retries, injected
  faults, bad state) -> this message ID goes into batchItemFailures, SQS
  redelivers it after the visibility timeout, and after maxReceiveCount it
  lands in the DLQ.
"""

import json
import os
import random
import time

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from shared import faults, obs, rules, schema
from shared.aws import client, results_table
from shared.ddbutil import now_ms

_ser = TypeSerializer()
TX_CONFLICT_RETRIES = 5


def _av(d: dict) -> dict:
    return {k: _ser.serialize(v) for k, v in d.items() if v is not None}


class StateMismatch(Exception):
    """The job is not in a state that accepts this row (system problem, not user data)."""


def business_key(msg: dict) -> str:
    # Stable across redelivery and re-dispatch. Deliberately NOT the SQS messageId:
    # a re-sent row gets a new messageId but must dedup against the first one.
    return f"{msg['jobId']}#{msg['runRequestId']}#{msg['fileVersion']}#{msg['rowNumber']}"


def build_result(msg: dict, record: dict) -> dict:
    normalized, errors = rules.validate_row(msg["source"], msg["currency"], msg.get("duplicateOf"))
    t = now_ms()
    sent_ts = int(record.get("attributes", {}).get("SentTimestamp", t))
    item = {
        "jobId": msg["jobId"],
        "rowNumber": int(msg["rowNumber"]),
        "businessKey": business_key(msg),
        "runRequestId": msg["runRequestId"],
        "fileVersion": msg["fileVersion"],
        "resultStatus": "VALID" if normalized else "INVALID",
        "sourceData": {k: v for k, v in msg["source"].items() if v is not None},
        "processedAt": t,
        "messageId": record["messageId"],
        "receiveCount": int(record.get("attributes", {}).get("ApproximateReceiveCount", 1)),
        "queueWaitMs": t - sent_ts,
        "sinceRunMs": t - int(msg.get("runRequestedAt", t)),
    }
    if normalized:
        item["normalizedData"] = normalized
    else:
        item["validationErrors"] = errors
    return item


def commit(item: dict) -> str:
    """Returns 'created' or 'duplicate'. Raises on anything that is not a proven duplicate."""
    ddb = client("dynamodb")
    counter = "validCount" if item["resultStatus"] == "VALID" else "invalidCount"
    tx = [
        {"Put": {
            "TableName": os.environ["RESULTS_TABLE"],
            "Item": _av(item),
            "ConditionExpression": "attribute_not_exists(rowNumber)",
        }},
        {"Update": {
            "TableName": os.environ["JOBS_TABLE"],
            "Key": _av({"jobId": item["jobId"]}),
            "UpdateExpression": f"ADD processedCount :one, {counter} :one SET updatedAt = :t",
            "ConditionExpression": "runRequestId = :r AND #s = :p",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": _av({":one": 1, ":t": item["processedAt"],
                                              ":r": item["runRequestId"], ":p": schema.PROCESSING}),
        }},
    ]
    for attempt in range(TX_CONFLICT_RETRIES + 1):
        try:
            ddb.transact_write_items(TransactItems=tx)
            return "created"
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            codes = [r.get("Code", "None") for r in e.response.get("CancellationReasons", [])]
            if codes and codes[0] == "ConditionalCheckFailed":
                # Never assume "cancelled" means "duplicate": prove the result exists
                # and belongs to the same business key.
                existing = results_table().get_item(
                    Key={"jobId": item["jobId"], "rowNumber": item["rowNumber"]}, ConsistentRead=True
                ).get("Item")
                if existing and existing.get("businessKey") == item["businessKey"]:
                    return "duplicate"
                raise StateMismatch(f"result exists with different businessKey: {existing and existing.get('businessKey')}")
            if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
                raise StateMismatch("job is not PROCESSING for this run")
            if "TransactionConflict" in codes and attempt < TX_CONFLICT_RETRIES:
                # Many rows update the same Job item: the known MVP hot spot.
                obs.metric("TxConflictRetries")
                time.sleep(random.uniform(0, 0.05 * (2 ** attempt)))
                continue
            raise


def process_record(record: dict, fault_cfg: dict) -> str:
    msg = json.loads(record["body"])
    receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", 1))
    row = int(msg["rowNumber"])

    if row in set(fault_cfg.get("fail_rows", [])):
        raise faults.InjectedFault(f"permanent system failure for row {row}")

    item = build_result(msg, record)
    outcome = commit(item)

    if outcome == "created" and row in set(fault_cfg.get("crash_after_commit_rows", [])) and receive_count == 1:
        # Simulates: commit succeeded, but the function died before SQS learned it.
        raise faults.InjectedFault(f"crash after commit for row {row}")

    if outcome == "duplicate":
        obs.metric("DuplicatesSkipped")
    else:
        obs.metric("RowsValid" if item["resultStatus"] == "VALID" else "RowsInvalid")
        obs.metric("RowQueueWaitMs", item["queueWaitMs"], unit="Milliseconds")
    obs.info("row processed", stage="worker", jobId=msg["jobId"], runRequestId=msg["runRequestId"],
             rowNumber=row, messageId=record["messageId"], attempt=receive_count,
             outcome=outcome, resultStatus=item["resultStatus"])
    return outcome


def handler(event, context):
    started = time.time()
    fault_cfg = faults.get("worker")
    if fault_cfg.get("slow_ms"):
        time.sleep(int(fault_cfg["slow_ms"]) / 1000.0)  # simulates a slow downstream
    failures = []
    for record in event.get("Records", []):
        try:
            process_record(record, fault_cfg)
        except Exception as e:  # per-record: only this message is retried
            failures.append({"itemIdentifier": record["messageId"]})
            obs.error("row failed; will be retried by SQS", stage="worker", messageId=record["messageId"],
                      attempt=record.get("attributes", {}).get("ApproximateReceiveCount"),
                      errorType=type(e).__name__, error=str(e)[:300])
            obs.metric("RowSystemFailures")
    obs.info("batch done", stage="worker", size=len(event.get("Records", [])), failed=len(failures),
             durationMs=int((time.time() - started) * 1000))
    obs.metric("WorkerBatchSize", len(event.get("Records", [])))
    return {"batchItemFailures": failures}
