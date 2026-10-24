"""Operator recovery tool. See docs/runbook.md for when to use each command.

  python scripts/recover.py status                      # stalled jobs + DLQ depths
  python scripts/recover.py inspect <jobId>             # manifest vs Results vs counters
  python scripts/recover.py redispatch <jobId>          # re-send ONLY rows missing from Results (same run identity)
  python scripts/recover.py finalize <jobId>            # resume a job stuck in PROCESSING/REPORTING report step
  python scripts/recover.py peek <dlq>                  # show DLQ messages without removing them
  python scripts/recover.py redrive <dlq> --max N       # move up to N messages back to the source queue

<dlq> is one of: work, ingest. A message in a DLQ is a failure signal, not success:
fix the cause first, then redrive a limited number and watch the dashboard.
"""

import argparse
import json
import time

from boto3.dynamodb.conditions import Key

from cb import aws, outputs

ACTIVE = {"VALIDATING_UPLOAD", "RUN_REQUESTED", "PROCESSING", "REPORTING"}
DLQS = {"work": ("WorkDLQUrl", "WorkQueueUrl"), "ingest": ("IngestDLQUrl", "IngestQueueUrl")}


def ddb_table(name):
    import boto3
    from cb import REGION
    return boto3.resource("dynamodb", region_name=REGION).Table(outputs()[name])


def depth(url):
    a = aws("sqs").get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages",
                                                                       "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    return int(a["ApproximateNumberOfMessages"]), int(a["ApproximateNumberOfMessagesNotVisible"])


def status(_):
    o = outputs()
    for k in ("WorkQueueUrl", "IngestQueueUrl", "WorkDLQUrl", "IngestDLQUrl", "EventDeliveryDLQUrl", "LifecycleDLQUrl"):
        vis, inflight = depth(o[k])
        print(f"{k:22s} visible={vis:5d} inflight={inflight:5d}")
    now = int(time.time() * 1000)
    jobs = ddb_table("JobsTable")
    kw = {}
    print("\nActive jobs (status, minutes since last update, processed/expected, dispatchComplete):")
    while True:
        r = jobs.scan(**kw)
        for j in r.get("Items", []):
            if j["status"] in ACTIVE:
                age = (now - int(j.get("updatedAt", 0))) / 60000
                flag = "  <-- STALLED" if age > 10 else ""
                print(f"  {j['jobId']} {j['status']:18s} {age:6.1f} min  {j.get('processedCount', '-')}/{j.get('expectedCount', '-')}"
                      f"  dispatchComplete={j.get('dispatchComplete')}{flag}")
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def load(job_id):
    job = ddb_table("JobsTable").get_item(Key={"jobId": job_id}, ConsistentRead=True).get("Item")
    if not job:
        raise SystemExit("job not found")
    results = []
    kw = {"KeyConditionExpression": Key("jobId").eq(job_id), "ConsistentRead": True,
          "ProjectionExpression": "rowNumber, resultStatus, businessKey"}
    while True:
        r = ddb_table("ResultsTable").query(**kw)
        results += r.get("Items", [])
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    manifest = None
    if job.get("manifestKey"):
        body = aws("s3").get_object(Bucket=outputs()["WorkBucket"], Key=job["manifestKey"])["Body"].read()
        manifest = json.loads(body)
    return job, results, manifest


def inspect(a):
    job, results, manifest = load(a.job_id)
    have = {int(r["rowNumber"]) for r in results}
    expected_rows = {r["rowNumber"] for r in manifest["rows"]} if manifest else set()
    missing = sorted(expected_rows - have)
    valid = sum(1 for r in results if r["resultStatus"] == "VALID")
    report = {
        "status": job["status"], "runRequestId": job.get("runRequestId"), "dispatchComplete": job.get("dispatchComplete"),
        "manifestRows": len(expected_rows), "resultsRows": len(have),
        "counters": {k: int(job.get(k, 0)) for k in ("expectedCount", "processedCount", "validCount", "invalidCount")},
        "resultsValid": valid, "resultsInvalid": len(results) - valid,
        "missingRows": missing[:50], "missingCount": len(missing),
        "countersMatchResults": int(job.get("processedCount", 0)) == len(have)
        and int(job.get("validCount", 0)) == valid,
    }
    print(json.dumps(report, indent=2, default=str))
    return report


def redispatch(a):
    job, results, manifest = load(a.job_id)
    if job["status"] not in ("PROCESSING",):
        raise SystemExit(f"job is {job['status']}; redispatch only applies to PROCESSING jobs")
    have = {int(r["rowNumber"]) for r in results}
    missing = sorted({r["rowNumber"] for r in manifest["rows"]} - have)
    if not job.get("dispatchComplete"):
        msg = {"type": "DISPATCH_ROWS", "jobId": a.job_id, "runRequestId": job["runRequestId"]}
        print("dispatch never completed: re-sending the full dispatch (Worker dedups rows already done)")
    elif not missing:
        print("no missing rows; try `finalize` if the job is stuck")
        return
    else:
        msg = {"type": "DISPATCH_ROWS", "jobId": a.job_id, "runRequestId": job["runRequestId"], "rowNumbers": missing}
        print(f"re-sending {len(missing)} missing row(s) with the same runRequestId: {missing[:20]}")
    aws("sqs").send_message(QueueUrl=outputs()["IngestQueueUrl"], MessageBody=json.dumps(msg))


def finalize(a):
    aws("sqs").send_message(QueueUrl=outputs()["IngestQueueUrl"],
                            MessageBody=json.dumps({"type": "FINALIZE", "jobId": a.job_id}))
    print("FINALIZE sent; the importer resumes report generation idempotently")


def peek(a):
    url = outputs()[DLQS[a.dlq][0]]
    r = aws("sqs").receive_message(QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=5, AttributeNames=["All"])
    for m in r.get("Messages", []):
        print(json.dumps({"messageId": m["MessageId"], "receiveCount": m["Attributes"].get("ApproximateReceiveCount"),
                          "body": json.loads(m["Body"])}, ensure_ascii=False))


def redrive(a):
    src, dst = (outputs()[k] for k in DLQS[a.dlq])
    sqs = aws("sqs")
    moved = 0
    while moved < a.max:
        r = sqs.receive_message(QueueUrl=src, MaxNumberOfMessages=min(10, a.max - moved), VisibilityTimeout=30)
        msgs = r.get("Messages", [])
        if not msgs:
            break
        for m in msgs:
            # Same body => same business key (jobId, runRequestId, fileVersion, rowNumber).
            sqs.send_message(QueueUrl=dst, MessageBody=m["Body"])
            sqs.delete_message(QueueUrl=src, ReceiptHandle=m["ReceiptHandle"])
            moved += 1
    print(f"redrove {moved} message(s) from {a.dlq} DLQ")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=status)
    for name, fn in (("inspect", inspect), ("redispatch", redispatch), ("finalize", finalize)):
        s = sub.add_parser(name)
        s.add_argument("job_id")
        s.set_defaults(fn=fn)
    s = sub.add_parser("peek")
    s.add_argument("dlq", choices=DLQS)
    s.set_defaults(fn=peek)
    s = sub.add_parser("redrive")
    s.add_argument("dlq", choices=DLQS)
    s.add_argument("--max", type=int, default=10)
    s.set_defaults(fn=redrive)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
