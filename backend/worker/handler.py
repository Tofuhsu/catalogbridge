"""Worker Lambda: WorkQueue (SQS) ESM, one row per message.

Per row, the result write and the Job counter update are in ONE DynamoDB transaction:
  1. Put Results[jobId, rowNumber] only if it does not exist yet, and
  2. ADD processedCount/validCount|invalidCount on the Job.
Rows of the same job in one SQS batch share a transaction (N puts + one counter ADD of N) to
reduce writes to the Job item; any doubt (an existing row, a conflict) falls back to per-row.
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


def commit_group(items: list) -> bool:
    """One transaction for several rows of the same job run: N conditional Puts + ONE counter update.

    This cuts writes to the shared Job item (the measured hot spot) from one per row to one per
    group. It returns False whenever the caller should fall back to per-row commit(), which keeps
    the exact dedup semantics: any row already present (possible duplicate), the same row twice
    in the batch, a job-state mismatch, or conflicts that persist after retries.
    """
    if len(items) < 2 or len({it["rowNumber"] for it in items}) != len(items):
        return False  # DynamoDB rejects two operations on one item in a transaction
    ddb = client("dynamodb")
    valid = sum(1 for it in items if it["resultStatus"] == "VALID")
    first = items[0]
    tx = [{"Put": {
        "TableName": os.environ["RESULTS_TABLE"],
        "Item": _av(it),
        "ConditionExpression": "attribute_not_exists(rowNumber)",
    }} for it in items]
    tx.append({"Update": {
        "TableName": os.environ["JOBS_TABLE"],
        "Key": _av({"jobId": first["jobId"]}),
        "UpdateExpression": "ADD processedCount :n, validCount :v, invalidCount :i SET updatedAt = :t",
        "ConditionExpression": "runRequestId = :r AND #s = :p",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": _av({":n": len(items), ":v": valid, ":i": len(items) - valid,
                                          ":t": max(it["processedAt"] for it in items),
                                          ":r": first["runRequestId"], ":p": schema.PROCESSING}),
    }})
    for attempt in range(TX_CONFLICT_RETRIES + 1):
        try:
            ddb.transact_write_items(TransactItems=tx)
            obs.metric("TxGroupCommits")
            obs.metric("TxGroupRows", len(items))
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            codes = [r.get("Code", "None") for r in e.response.get("CancellationReasons", [])]
            if "ConditionalCheckFailed" in codes:
                break  # some row exists or the job changed: let per-row commit decide row by row
            if "TransactionConflict" in codes and attempt < TX_CONFLICT_RETRIES:
                obs.metric("TxConflictRetries")
                time.sleep(random.uniform(0, 0.05 * (2 ** attempt)))
                continue
            break
    obs.metric("TxGroupFallbacks")
    return False


def finish_record(record: dict, msg: dict, item: dict, outcome: str, fault_cfg: dict) -> str:
    receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", 1))
    row = int(msg["rowNumber"])
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


def process_record(record: dict, fault_cfg: dict) -> str:
    """Per-row path (also the fallback inside handler)."""
    msg = json.loads(record["body"])
    if int(msg["rowNumber"]) in set(fault_cfg.get("fail_rows", [])):
        raise faults.InjectedFault(f"permanent system failure for row {msg['rowNumber']}")
    item = build_result(msg, record)
    return finish_record(record, msg, item, commit(item), fault_cfg)


def handler(event, context):
    started = time.time()
    fault_cfg = faults.get("worker")
    if fault_cfg.get("slow_ms"):
        time.sleep(int(fault_cfg["slow_ms"]) / 1000.0)  # simulates a slow downstream
    failures = []

    def fail(record, e):  # per-record: only this message is retried
        failures.append({"itemIdentifier": record["messageId"]})
        obs.error("row failed; will be retried by SQS", stage="worker", messageId=record["messageId"],
                  attempt=record.get("attributes", {}).get("ApproximateReceiveCount"),
                  errorType=type(e).__name__, error=str(e)[:300])
        obs.metric("RowSystemFailures")

    # Validate every row first, then group rows of the same job run into one transaction.
    groups = {}
    fail_rows = set(fault_cfg.get("fail_rows", []))
    for record in event.get("Records", []):
        try:
            msg = json.loads(record["body"])
            if int(msg["rowNumber"]) in fail_rows:
                raise faults.InjectedFault(f"permanent system failure for row {msg['rowNumber']}")
            item = build_result(msg, record)
            groups.setdefault((msg["jobId"], msg["runRequestId"]), []).append((record, msg, item))
        except Exception as e:
            fail(record, e)

    for members in groups.values():
        try:
            grouped = commit_group([item for _, _, item in members])
        except Exception as e:  # unexpected error on the group: fall back to per-row
            obs.warn("group commit error; falling back to per-row", errorType=type(e).__name__, error=str(e)[:200])
            grouped = False
        for record, msg, item in members:
            try:
                finish_record(record, msg, item, "created" if grouped else commit(item), fault_cfg)
            except Exception as e:
                fail(record, e)
    obs.info("batch done", stage="worker", size=len(event.get("Records", [])), failed=len(failures),
             durationMs=int((time.time() - started) * 1000))
    obs.metric("WorkerBatchSize", len(event.get("Records", [])))
    return {"batchItemFailures": failures}
