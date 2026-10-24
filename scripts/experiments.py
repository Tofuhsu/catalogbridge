"""Failure experiments on the DEPLOYED test stack (spec §11.1). Requires -c faultInjection=true.

  python scripts/experiments.py all
  python scripts/experiments.py crash_after_commit      # or any single experiment name

Every experiment: sets a fault, runs a real job through the public API, clears the fault,
and records what actually happened (job state, Results vs counters, receive counts,
CloudWatch metrics) in docs/evidence/exp-<name>-<timestamp>.json. Faults are always cleared,
even when an experiment fails.
"""

import collections
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key

from cb import REGION, STD, TestUser, aws, fixture, outputs, save_evidence


def set_faults(cfg):
    aws("ssm").put_parameter(Name=outputs()["FaultsParam"], Value=json.dumps(cfg), Type="String", Overwrite=True)
    time.sleep(7)  # functions cache the parameter for 5 s


def results_of(job_id):
    import boto3
    t = boto3.resource("dynamodb", region_name=REGION).Table(outputs()["ResultsTable"])
    items, kw = [], {"KeyConditionExpression": Key("jobId").eq(job_id), "ConsistentRead": True}
    while True:
        r = t.query(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def job_item(job_id):
    import boto3
    return boto3.resource("dynamodb", region_name=REGION).Table(outputs()["JobsTable"]).get_item(
        Key={"jobId": job_id}, ConsistentRead=True)["Item"]


def consistency(job_id):
    job = job_item(job_id)
    res = results_of(job_id)
    rows = [int(r["rowNumber"]) for r in res]
    valid = sum(1 for r in res if r["resultStatus"] == "VALID")
    rc = collections.Counter(int(r.get("receiveCount", 1)) for r in res)
    return {
        "status": job["status"], "expectedCount": int(job.get("expectedCount", 0)),
        "processedCount": int(job.get("processedCount", 0)), "validCount": int(job.get("validCount", 0)),
        "invalidCount": int(job.get("invalidCount", 0)), "resultsRows": len(rows), "distinctRows": len(set(rows)),
        "resultsValid": valid, "countersMatchResults": int(job.get("processedCount", 0)) == len(rows)
        and int(job.get("validCount", 0)) == valid,
        "resultReceiveCountHistogram": dict(sorted(rc.items())),
        "endToEndSeconds": (int(job["completedAt"]) - int(job["runRequestedAt"])) / 1000 if job.get("completedAt") else None,
        "dispatchAttempts": int(job.get("dispatchAttempts", 0)) if job.get("dispatchAttempts") else None,
        "reportKey": job.get("outputKey"),
    }


def metric_sum(name, service, start, end, stat="Sum"):
    cw = aws("cloudwatch")
    r = cw.get_metric_statistics(Namespace="CatalogBridge", MetricName=name, Dimensions=[{"Name": "Service", "Value": service}],
                                 StartTime=start, EndTime=end, Period=60, Statistics=[stat])
    pts = r["Datapoints"]
    if stat == "Sum":
        return sum(p["Sum"] for p in pts)
    return max((p[stat] for p in pts), default=None)


def aws_metric_max(namespace, name, dims, start, end, stat="Maximum"):
    r = aws("cloudwatch").get_metric_statistics(Namespace=namespace, MetricName=name,
                                                Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
                                                StartTime=start, EndTime=end, Period=60, Statistics=[stat])
    return [{"t": p["Timestamp"].isoformat(), stat: p[stat]} for p in sorted(r["Datapoints"], key=lambda p: p["Timestamp"])]


def alarm_history(alarm_prefix, start):
    cw = aws("cloudwatch")
    out = []
    for a in cw.describe_alarms(AlarmNamePrefix=alarm_prefix)["MetricAlarms"]:
        h = cw.describe_alarm_history(AlarmName=a["AlarmName"], HistoryItemType="StateUpdate", StartDate=start)
        out += [{"alarm": a["AlarmName"], "t": i["Timestamp"].isoformat(), "summary": i["HistorySummary"]}
                for i in h["AlarmHistoryItems"]]
    return out


def start_job(api, name="rows_500.csv", data=None):
    job_id = api.create_and_upload(data or fixture(name), name=name)
    api.wait(job_id, {"AWAITING_MAPPING"})
    api.map_and_run(job_id, STD)
    return job_id


def wait_terminal(api, job_id, timeout=900):
    return api.wait(job_id, {"COMPLETED", "COMPLETED_WITH_ERRORS"}, timeout=timeout, poll=3)


# ---------------------------------------------------------------- experiments

def exp_duplicate_delivery(api):
    """Re-send every row of a running job with NEW messageIds (what a re-dispatch looks like)."""
    set_faults({"worker": {"slow_ms": 300}})  # keep the job PROCESSING long enough to inject duplicates
    job_id = start_job(api)
    while job_item(job_id).get("dispatchComplete") is not True:
        time.sleep(1)
    job = job_item(job_id)
    aws("sqs").send_message(QueueUrl=outputs()["IngestQueueUrl"], MessageBody=json.dumps(
        {"type": "DISPATCH_ROWS", "jobId": job_id, "runRequestId": job["runRequestId"], "rowNumbers": list(range(1, 501))}))
    set_faults({})
    wait_terminal(api, job_id)
    time.sleep(90)  # let the duplicate messages drain after completion
    return job_id, {"injected": "500 duplicate work messages (new messageIds, same business keys)"}


def exp_crash_after_commit(api):
    rows = [3, 50, 120, 333, 499]
    set_faults({"worker": {"crash_after_commit_rows": rows}})
    job_id = start_job(api)
    wait_terminal(api, job_id)
    set_faults({})
    return job_id, {"injected": f"commit succeeds, then the invocation reports failure for rows {rows} on first receive"}


def exp_crash_mid_dispatch(api):
    set_faults({"importer": {"crash_after_sent": 200}})
    job_id = start_job(api)
    wait_terminal(api, job_id, timeout=1200)  # ingest visibility timeout is 360 s
    set_faults({})
    return job_id, {"injected": "dispatcher raises after 200 of 500 rows on the first attempt"}


def exp_permanent_failure_and_redrive(api):
    set_faults({"worker": {"fail_rows": [5, 250]}})
    job_id = start_job(api)
    t0 = time.time()
    dlq = outputs()["WorkDLQUrl"]
    while time.time() - t0 < 1500:
        a = aws("sqs").get_queue_attributes(QueueUrl=dlq, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
        if int(a["ApproximateNumberOfMessages"]) >= 2:
            break
        time.sleep(10)
    seconds_to_dlq = round(time.time() - t0)
    before = consistency(job_id)
    # Wait for the alarm to evaluate so we can record it, then fix + redrive.
    time.sleep(120)
    set_faults({})
    out = subprocess.run([sys.executable, "scripts/recover.py", "redrive", "work", "--max", "10"],
                         capture_output=True, text=True)
    wait_terminal(api, job_id, timeout=600)
    return job_id, {"injected": "rows 5 and 250 always fail with a system error (maxReceiveCount=3)",
                    "secondsUntilBothInDLQ": seconds_to_dlq, "stateWhileInDLQ": before,
                    "redriveOutput": out.stdout.strip()}


def exp_crash_after_report(api):
    set_faults({"lifecycle": {"crash_after_report_write": True}})
    job_id = start_job(api, "mixed_errors.csv")
    wait_terminal(api, job_id, timeout=600)
    set_faults({})
    return job_id, {"injected": "lifecycle writes both report files, then crashes before the final status update (once)"}


def exp_slow_downstream(api):
    set_faults({"worker": {"slow_ms": 2000}})
    job_id = start_job(api)
    wait_terminal(api, job_id, timeout=1200)
    set_faults({})
    return job_id, {"injected": "every worker batch sleeps 2 s (simulated slow dependency)"}


EXPERIMENTS = {
    "duplicate_delivery": exp_duplicate_delivery,
    "crash_after_commit": exp_crash_after_commit,
    "crash_mid_dispatch": exp_crash_mid_dispatch,
    "permanent_failure_and_redrive": exp_permanent_failure_and_redrive,
    "crash_after_report": exp_crash_after_report,
    "slow_downstream": exp_slow_downstream,
}


def run_one(name, api):
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    try:
        job_id, info = EXPERIMENTS[name](api)
    finally:
        set_faults({})
    time.sleep(70)  # let EMF metrics land
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    o = outputs()
    ev = {
        "experiment": name, "jobId": job_id, "startedAt": start.isoformat(), **info,
        "result": consistency(job_id),
        "metrics": {
            "DuplicatesSkipped": metric_sum("DuplicatesSkipped", "WorkerFn", start, end),
            "RowSystemFailures": metric_sum("RowSystemFailures", "WorkerFn", start, end),
            "TxConflictRetries": metric_sum("TxConflictRetries", "WorkerFn", start, end),
            "IngestFailures": metric_sum("IngestFailures", "ImporterFn", start, end),
            "LifecycleFailures": metric_sum("LifecycleFailures", "LifecycleFn", start, end),
            "workerConcurrentExecutionsMax": aws_metric_max("AWS/Lambda", "ConcurrentExecutions",
                                                            {"FunctionName": o["WorkerFunctionName"]}, start, end),
            "workQueueOldestAgeMax": aws_metric_max("AWS/SQS", "ApproximateAgeOfOldestMessage",
                                                    {"QueueName": o["WorkQueueUrl"].rsplit("/", 1)[1]}, start, end),
        },
        "alarmHistory": alarm_history("catalogbridge-", start),
    }
    path = save_evidence(f"exp-{name}-{start.strftime('%Y%m%dT%H%M%S')}.json", ev)
    print(f"{name}: {json.dumps(ev['result'])}\n  -> {path}")
    return ev


def main():
    names = list(EXPERIMENTS) if sys.argv[1:] in ([], ["all"]) else sys.argv[1:]
    with TestUser("exp") as U:
        for n in names:
            run_one(n, U.api)


if __name__ == "__main__":
    main()
