"""Job finalization: PROCESSING -> REPORTING -> COMPLETED[_WITH_ERRORS].

Rules (spec §6.4):
- Only start when dispatchComplete=true AND processedCount == expectedCount,
  checked on a consistent read, not on the stream image.
- Enter REPORTING with a conditional update. A retry that finds REPORTING must
  continue (never skip forever), so finalize() is safe to call repeatedly.
- Report keys are deterministic per (jobId, runRequestId) and content is built
  from Results sorted by rowNumber, so a re-run writes identical bytes.
- The terminal status is written only after both S3 objects exist.
"""

import csv
import io
import json
import os

from boto3.dynamodb.conditions import Key

from shared import faults, obs, rules, schema
from shared.aws import client, jobs_table, results_table
from shared.ddbutil import as_int, get_job, is_conditional_fail, now_ms


class ReportError(Exception):
    """State is inconsistent (e.g. counters say done but results are missing)."""


def report_keys(job: dict) -> dict:
    base = f"reports/{job['jobId']}/{job['runRequestId']}"
    return {"outputKey": f"{base}/{schema.SCHEMA_VERSION}.csv", "errorsKey": f"{base}/errors.csv"}


def query_results(job_id: str) -> list:
    items = []
    kwargs = {"KeyConditionExpression": Key("jobId").eq(job_id), "ConsistentRead": True}
    while True:
        resp = results_table().query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    items.sort(key=lambda it: int(it["rowNumber"]))
    return items


def build_csvs(results: list) -> tuple[bytes, bytes]:
    out = io.StringIO(newline="")
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(schema.OUTPUT_COLUMNS)
    err = io.StringIO(newline="")
    ew = csv.writer(err, lineterminator="\r\n")
    ew.writerow(schema.ERROR_COLUMNS)
    for it in results:
        row = int(it["rowNumber"])
        if it["resultStatus"] == "VALID":
            d = it["normalizedData"]
            w.writerow([rules.csv_safe_text(d["sku"]), rules.csv_safe_text(d["title"]),
                        d["price"], d["currency"], d["stock"], row])
        else:
            sku = (it.get("sourceData") or {}).get("sku") or ""
            for e in it.get("validationErrors", []):
                ew.writerow([row, rules.csv_safe_text(sku), e["field"], e["code"], rules.csv_safe_text(e["message"])])
    # UTF-8 with BOM so spreadsheet apps detect the encoding of non-ASCII titles.
    return ("﻿" + out.getvalue()).encode("utf-8"), ("﻿" + err.getvalue()).encode("utf-8")


def finalize(job_id: str) -> str:
    job = get_job(job_id)
    if not job:
        return "missing"
    status = job["status"]
    if status in (schema.COMPLETED, schema.COMPLETED_WITH_ERRORS):
        return "done"
    if status == schema.PROCESSING:
        if not (job.get("dispatchComplete") and as_int(job.get("processedCount")) == as_int(job.get("expectedCount"))):
            return "not_ready"
        try:
            jobs_table().update_item(
                Key={"jobId": job_id},
                UpdateExpression="SET #s = :rep, reportingStartedAt = :t, updatedAt = :t",
                ConditionExpression="#s = :p AND dispatchComplete = :true AND processedCount = expectedCount",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":rep": schema.REPORTING, ":p": schema.PROCESSING,
                                           ":true": True, ":t": now_ms()},
            )
        except Exception as e:
            if not is_conditional_fail(e):
                raise
            job = get_job(job_id)
            if job["status"] != schema.REPORTING:
                return "lost_race:" + job["status"]
    elif status != schema.REPORTING:
        return "not_ready"

    results = query_results(job_id)
    expected = as_int(job.get("expectedCount"))
    if len(results) != expected:
        raise ReportError(f"job {job_id}: {len(results)} results but expectedCount={expected}")
    invalid = sum(1 for r in results if r["resultStatus"] != "VALID")
    if invalid != as_int(job.get("invalidCount")) or len(results) - invalid != as_int(job.get("validCount")):
        raise ReportError(f"job {job_id}: counters do not match results")

    output, errors = build_csvs(results)
    keys = report_keys(job)
    s3 = client("s3")
    bucket = os.environ["WORK_BUCKET"]
    s3.put_object(Bucket=bucket, Key=keys["outputKey"], Body=output, ContentType="text/csv; charset=utf-8")
    s3.put_object(Bucket=bucket, Key=keys["errorsKey"], Body=errors, ContentType="text/csv; charset=utf-8")

    lc_faults = faults.get("lifecycle")
    if lc_faults.get("crash_after_report_write"):
        if job_id not in lc_faults.get("_fired_for", []):
            _mark_fault_fired(job_id)
            obs.warn("FAULT: crash after report write", jobId=job_id)
            raise faults.InjectedFault("crash after report write")

    final = schema.COMPLETED if invalid == 0 else schema.COMPLETED_WITH_ERRORS
    t = now_ms()
    try:
        jobs_table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #s = :f, outputKey = :o, errorsKey = :e, completedAt = :t, updatedAt = :t",
            ConditionExpression="#s = :rep",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":f": final, ":o": keys["outputKey"], ":e": keys["errorsKey"],
                                       ":rep": schema.REPORTING, ":t": t},
        )
    except Exception as e:
        if not is_conditional_fail(e):
            raise
        return "already_final"
    obs.info("job finalized", jobId=job_id, runRequestId=job["runRequestId"], stage="report", status=final,
             rows=len(results), invalid=invalid, e2eMs=t - as_int(job.get("runRequestedAt"), t))
    obs.metric("JobEndToEndMs", t - as_int(job.get("runRequestedAt"), t), unit="Milliseconds")
    obs.metric("JobsCompleted")
    return final


def _mark_fault_fired(job_id: str):
    """Make crash_after_report_write fire once per job so recovery can be observed."""
    param = os.environ.get("FAULTS_PARAM")
    if not param:
        return
    ssm = client("ssm")
    cfg = json.loads(ssm.get_parameter(Name=param)["Parameter"]["Value"] or "{}")
    lc = cfg.setdefault("lifecycle", {})
    lc.setdefault("_fired_for", []).append(job_id)
    ssm.put_parameter(Name=param, Value=json.dumps(cfg), Type="String", Overwrite=True)
    faults.reset_cache()
