"""Importer Lambda: IngestQueue (SQS) consumer.

Message types:
- UPLOAD_READY  (from S3 -> EventBridge): bind the S3 object version to the job,
  validate structure, write the canonical manifest, -> AWAITING_MAPPING.
- DISPATCH_ROWS (from the Jobs stream lifecycle function, or the admin
  recovery script): fan rows out to the WorkQueue. See shared/dispatch.py.
- FINALIZE      (admin recovery only): resume report generation for a job.
"""

import json
import os
import re
import time

from shared import csvparse, dispatch, obs, report, schema
from shared.aws import client, jobs_table
from shared.ddbutil import get_job, is_conditional_fail, now_ms

_RAW_KEY = re.compile(r"^raw/(?P<owner>[A-Za-z0-9-]{1,64})/(?P<job>[A-Za-z0-9-]{1,64})/input\.csv$")


def manifest_key(job_id: str, version: str) -> str:
    return f"manifests/{job_id}/{version}.json"


def handle_upload(msg: dict) -> str:
    m = _RAW_KEY.match(msg.get("key", ""))
    if not m:
        obs.warn("upload with unexpected key; ignoring", key=msg.get("key"))
        return "ignored"
    owner, job_id, version = m["owner"], m["job"], msg.get("versionId")
    if not version or version == "null":
        raise ValueError("S3 event has no versionId; raw bucket must be versioned")

    job = get_job(job_id)
    if not job or job["ownerId"] != owner:
        obs.warn("upload for unknown job or wrong owner; ignoring", jobId=job_id)
        return "ignored"
    bound = job.get("fileVersion")
    if bound and bound != version:
        # The job is already bound to another object version. A new file needs a new job.
        obs.warn("upload of a different version for a bound job; ignoring", jobId=job_id,
                 boundVersion=bound, eventVersion=version)
        obs.metric("UploadVersionIgnored")
        return "version_ignored"

    if job["status"] == schema.CREATED:
        try:
            jobs_table().update_item(
                Key={"jobId": job_id},
                UpdateExpression="SET #s = :v, fileVersion = :ver, fileSize = :size, updatedAt = :t",
                ConditionExpression="#s = :c AND attribute_not_exists(fileVersion)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":v": schema.VALIDATING_UPLOAD, ":ver": version, ":c": schema.CREATED,
                                           ":size": int(msg.get("size") or 0), ":t": now_ms()},
            )
        except Exception as e:
            if not is_conditional_fail(e):
                raise
            return handle_upload(msg)  # someone else bound a version first; re-evaluate
    elif not (job["status"] == schema.VALIDATING_UPLOAD and bound == version):
        obs.info("upload event already handled", jobId=job_id, status=job["status"])
        return "already_handled"

    obj = client("s3").get_object(Bucket=msg["bucket"], Key=msg["key"], VersionId=version)
    data = obj["Body"].read(schema.MAX_FILE_BYTES + 1)
    t = now_ms()
    try:
        manifest = csvparse.parse_csv(data)
    except csvparse.StructuralError as e:
        _set_after_validation(job_id, version, "SET #s = :st, rejectReasons = :r, updatedAt = :t",
                              {":st": schema.REJECTED, ":r": e.reasons, ":t": t})
        obs.info("upload rejected", jobId=job_id, stage="import", reasons=e.reasons)
        obs.metric("UploadsRejected")
        return "rejected"

    key = manifest_key(job_id, version)
    client("s3").put_object(Bucket=os.environ["WORK_BUCKET"], Key=key,
                            Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                            ContentType="application/json")
    _set_after_validation(
        job_id, version,
        "SET #s = :st, manifestKey = :k, headers = :h, rowCount = :n, sourceSignature = :sig, "
        "hadBom = :bom, validatedAt = :t, updatedAt = :t",
        {":st": schema.AWAITING_MAPPING, ":k": key, ":h": manifest["headers"], ":n": manifest["rowCount"],
         ":sig": manifest["sourceSignature"], ":bom": manifest["hadBom"], ":t": t},
    )
    obs.info("upload validated", jobId=job_id, stage="import", rows=manifest["rowCount"])
    obs.metric("UploadsValidated")
    return "validated"


def _set_after_validation(job_id, version, expr, values):
    values = {**values, ":vu": schema.VALIDATING_UPLOAD, ":ver": version}
    try:
        jobs_table().update_item(
            Key={"jobId": job_id}, UpdateExpression=expr,
            ConditionExpression="#s = :vu AND fileVersion = :ver",
            ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues=values,
        )
    except Exception as e:
        if not is_conditional_fail(e):
            raise
        obs.info("validation result already recorded", jobId=job_id)


def process(msg: dict, receive_count: int) -> str:
    kind = msg.get("type")
    if kind == "UPLOAD_READY":
        return handle_upload(msg)
    if kind == "DISPATCH_ROWS":
        return dispatch.dispatch(msg["jobId"], msg["runRequestId"], msg.get("rowNumbers"), receive_count)
    if kind == "FINALIZE":
        return report.finalize(msg["jobId"])
    obs.warn("unknown message type; ignoring", type=kind)
    return "ignored"


def handler(event, context):
    failures = []
    for record in event.get("Records", []):
        started = time.time()
        receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", 1))
        try:
            outcome = process(json.loads(record["body"]), receive_count)
            obs.info("ingest message done", stage="ingest", messageId=record["messageId"], outcome=outcome,
                     attempt=receive_count, durationMs=int((time.time() - started) * 1000))
        except Exception as e:
            failures.append({"itemIdentifier": record["messageId"]})
            obs.error("ingest message failed; will be retried", stage="ingest", messageId=record["messageId"],
                      attempt=receive_count, errorType=type(e).__name__, error=str(e)[:300])
            obs.metric("IngestFailures")
    return {"batchItemFailures": failures}
