"""End-to-end pipeline behaviour against moto (fake AWS), incl. failure scenarios of spec §11.1."""

import json

import boto3
from boto3.dynamodb.conditions import Key

from tests.conftest import call, set_faults
from tests.flow import (create_and_upload, csv_bytes, finalize_via_stream, get_job_item, ingest, pump_ingest,
                        run_job, run_worker, work_records)

A = "owner-a-0000-1111"
B = "owner-b-0000-2222"


def results_count(aws, job_id):
    t = boto3.resource("dynamodb", region_name="us-west-2").Table("Results")
    return t.query(KeyConditionExpression=Key("jobId").eq(job_id), Select="COUNT", ConsistentRead=True)["Count"]


def happy(aws, n=12, bad=()):
    job_id, up = create_and_upload(aws, A, csv_bytes(n, bad))
    ingest(up)
    assert get_job_item(aws, job_id)["status"] == "AWAITING_MAPPING"
    run_job(aws, A, job_id)
    pump_ingest(aws)
    return job_id


def test_happy_path_500_rows_with_stable_report(aws):
    job_id = happy(aws, n=500)
    recs = work_records(aws)
    assert len(recs) == 500
    assert run_worker(recs) == []
    finalize_via_stream(aws, job_id)
    job = get_job_item(aws, job_id)
    assert job["status"] == "COMPLETED"
    assert (job["processedCount"], job["validCount"], job["invalidCount"]) == (500, 500, 0)
    body = aws["s3"].get_object(Bucket=aws["WORK_BUCKET"], Key=job["outputKey"])["Body"].read().decode("utf-8-sig")
    lines = body.strip().split("\r\n")
    assert lines[0] == "sku,title,price,currency,stock,source_row"
    assert [int(l.split(",")[-1]) for l in lines[1:]] == list(range(1, 501))  # sorted by rowNumber


def test_business_errors_are_invalid_not_retried(aws):
    job_id = happy(aws, n=10, bad=(3, 7))
    assert run_worker(work_records(aws)) == []  # no batchItemFailures for bad data
    finalize_via_stream(aws, job_id)
    job = get_job_item(aws, job_id)
    assert job["status"] == "COMPLETED_WITH_ERRORS"
    assert (job["validCount"], job["invalidCount"]) == (8, 2)
    err = aws["s3"].get_object(Bucket=aws["WORK_BUCKET"], Key=job["errorsKey"])["Body"].read().decode("utf-8-sig")
    assert "3,SKU-0003,price,NEGATIVE" in err and "7,SKU-0007,price,NEGATIVE" in err


def test_duplicate_delivery_does_not_double_count(aws):
    job_id = happy(aws, n=10)
    recs = work_records(aws)
    run_worker(recs)
    # Same messages delivered again (SQS at-least-once), plus a re-send with new messageIds.
    resent = [dict(r, messageId=r["messageId"] + "-again") for r in recs]
    assert run_worker(recs) == [] and run_worker(resent) == []
    job = get_job_item(aws, job_id)
    assert job["processedCount"] == 10 and results_count(aws, job_id) == 10


def test_crash_after_commit_then_redelivery(aws, monkeypatch):
    job_id = happy(aws, n=10)
    set_faults(aws, monkeypatch, {"worker": {"crash_after_commit_rows": [4]}})
    recs = work_records(aws)
    failures = run_worker(recs)
    assert len(failures) == 1  # row 4 committed, but the invocation "died" before acking it
    failed = [dict(r, attributes={**r["attributes"], "ApproximateReceiveCount": "2"})
              for r in recs if r["messageId"] == failures[0]["itemIdentifier"]]
    assert run_worker(failed) == []  # redelivery is recognised as a duplicate
    job = get_job_item(aws, job_id)
    assert job["processedCount"] == 10 and results_count(aws, job_id) == 10


def test_mixed_batch_only_failed_item_retried(aws, monkeypatch):
    happy(aws, n=10)
    set_faults(aws, monkeypatch, {"worker": {"fail_rows": [5]}})
    recs = work_records(aws)
    failures = run_worker(recs, batch=10)
    row5 = [r["messageId"] for r in recs if json.loads(r["body"])["rowNumber"] == 5]
    assert [f["itemIdentifier"] for f in failures] == row5


def test_permanent_failure_never_marks_job_complete(aws, monkeypatch):
    job_id = happy(aws, n=5)
    set_faults(aws, monkeypatch, {"worker": {"fail_rows": [2]}})
    recs = work_records(aws)
    for attempt in range(1, 4):  # maxReceiveCount=3 then the message goes to the DLQ
        run_worker([dict(r, attributes={**r["attributes"], "ApproximateReceiveCount": str(attempt)}) for r in recs])
    finalize_via_stream(aws, job_id)
    job = get_job_item(aws, job_id)
    assert job["status"] == "PROCESSING" and job["processedCount"] == 4
    # Recovery: fix the cause, re-send ONLY the missing row with the same run identity.
    set_faults(aws, monkeypatch, {})
    ingest({"type": "DISPATCH_ROWS", "jobId": job_id, "runRequestId": job["runRequestId"], "rowNumbers": [2]})
    assert run_worker(work_records(aws)) == []
    finalize_via_stream(aws, job_id)
    assert get_job_item(aws, job_id)["status"] == "COMPLETED"


def test_crash_halfway_through_dispatch(aws, monkeypatch):
    job_id, up = create_and_upload(aws, A, csv_bytes(50))
    ingest(up)
    run_job(aws, A, job_id)
    set_faults(aws, monkeypatch, {"importer": {"crash_after_sent": 20}})
    from tests.conftest import drain
    msgs = drain(aws, "INGEST_QUEUE_URL")
    out = ingest(json.loads(msgs[0]["body"]), receive_count=1)
    assert out["batchItemFailures"]  # dispatch failed after 20 rows were sent
    assert get_job_item(aws, job_id)["dispatchComplete"] is False
    ingest(json.loads(msgs[0]["body"]), receive_count=2)  # SQS redelivers; dispatch restarts from row 1
    assert get_job_item(aws, job_id)["dispatchComplete"] is True
    recs = work_records(aws)
    assert len(recs) == 70  # 20 duplicates + 50
    assert run_worker(recs) == []
    finalize_via_stream(aws, job_id)
    job = get_job_item(aws, job_id)
    assert job["status"] == "COMPLETED" and job["processedCount"] == 50 and results_count(aws, job_id) == 50


def test_crash_after_report_write_recovers_same_report(aws, monkeypatch):
    job_id = happy(aws, n=8, bad=(2,))
    run_worker(work_records(aws))
    set_faults(aws, monkeypatch, {"lifecycle": {"crash_after_report_write": True}})
    out = finalize_via_stream(aws, job_id)
    assert out["batchItemFailures"]  # stream batch will be retried
    job = get_job_item(aws, job_id)
    assert job["status"] == "REPORTING"
    first = aws["s3"].get_object(Bucket=aws["WORK_BUCKET"], Key=f"reports/{job_id}/{job['runRequestId']}/errors.csv")["Body"].read()
    # Retry sees REPORTING and continues instead of skipping.
    from shared import report
    assert report.finalize(job_id) == "COMPLETED_WITH_ERRORS"
    job = get_job_item(aws, job_id)
    second = aws["s3"].get_object(Bucket=aws["WORK_BUCKET"], Key=job["errorsKey"])["Body"].read()
    assert first == second


def test_duplicate_dispatch_notification_is_skipped(aws):
    job_id = happy(aws, n=5)
    job = get_job_item(aws, job_id)
    ingest({"type": "DISPATCH_ROWS", "jobId": job_id, "runRequestId": job["runRequestId"]})
    assert len(work_records(aws)) == 5  # not 10


def test_structural_rejection_creates_no_work(aws):
    job_id, up = create_and_upload(aws, A, b"a,a\n1,2\n")
    ingest(up)
    job = get_job_item(aws, job_id)
    assert job["status"] == "REJECTED" and "Duplicate header" in job["rejectReasons"][0]
    assert work_records(aws) == []


def test_other_version_cannot_overwrite_bound_job(aws):
    job_id, up = create_and_upload(aws, A, csv_bytes(3))
    ingest(up)
    put = aws["s3"].put_object(Bucket=aws["RAW_BUCKET"], Key=up["key"], Body=csv_bytes(9))
    ingest(dict(up, versionId=put["VersionId"]))
    job = get_job_item(aws, job_id)
    assert job["rowCount"] == 3 and job["fileVersion"] == up["versionId"]
    ingest(up)  # same event redelivered: no change
    assert get_job_item(aws, job_id)["manifestKey"] == job["manifestKey"]


def test_concurrent_same_row_only_one_creates(aws):
    happy(aws, n=1)
    rec = work_records(aws)[0]
    import json as _j
    from worker.handler import build_result, commit
    msg = _j.loads(rec["body"])
    item = build_result(msg, rec)
    assert commit(item) == "created"
    assert commit(dict(item, messageId="other")) == "duplicate"
