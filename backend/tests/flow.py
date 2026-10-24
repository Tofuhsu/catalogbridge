"""Helpers that drive the pipeline locally, playing the role of the AWS event sources."""

import json

from boto3.dynamodb.types import TypeSerializer

from tests.conftest import call, drain, new_token

_ser = TypeSerializer()

GOOD_HEADER = "Item Code,Product Name,Unit Price,On Hand"
MAPPING = {"sku": "Item Code", "title": "Product Name", "price": "Unit Price", "stock": "On Hand"}


def csv_bytes(n_rows=5, bad_rows=(), header=GOOD_HEADER):
    lines = [header]
    for i in range(1, n_rows + 1):
        price = "-1" if i in bad_rows else f"{i}.99"
        lines.append(f"SKU-{i:04d},Product {i},{price},{i}")
    return ("\r\n".join(lines) + "\r\n").encode()


def create_and_upload(aws, owner, data, currency="USD", parent=None):
    body = {"clientToken": new_token(), "currency": currency, "supplierId": "acme", "fileName": "f.csv"}
    if parent:
        body["parentJobId"] = parent
    status, out = call("POST /jobs", owner, body)
    assert status == 201, out
    job_id = out["job"]["jobId"]
    key = out["upload"]["fields"]["key"]
    put = aws["s3"].put_object(Bucket=aws["RAW_BUCKET"], Key=key, Body=data)
    return job_id, {"type": "UPLOAD_READY", "bucket": aws["RAW_BUCKET"], "key": key,
                    "versionId": put["VersionId"], "size": len(data), "eventId": new_token()}


def ingest(msg, receive_count=1):
    from importer import handler

    rec = {"messageId": new_token(), "body": json.dumps(msg), "attributes": {"ApproximateReceiveCount": str(receive_count)}}
    return handler.handler({"Records": [rec]}, None)


def stream_record(old, new, seq="1"):
    def img(d):
        return {k: _ser.serialize(v) for k, v in d.items()} if d else None
    rec = {"eventName": "MODIFY", "dynamodb": {"SequenceNumber": seq, "NewImage": img(new)}}
    if old:
        rec["dynamodb"]["OldImage"] = img(old)
    return rec


def get_job_item(aws, job_id):
    from shared.ddbutil import get_job
    return get_job(job_id)


def lifecycle_on(old, new):
    from lifecycle import handler
    return handler.handler({"Records": [stream_record(old, new)]}, None)


def run_job(aws, owner, job_id, mapping=MAPPING):
    st, out = call("GET /jobs/{jobId}", owner, path={"jobId": job_id})
    v = out["job"]["mappingVersion"]
    st, out = call("PUT /jobs/{jobId}/mapping", owner, {"mapping": mapping, "mappingVersion": v, "source": "manual"},
                   path={"jobId": job_id})
    assert st == 200, out
    before = get_job_item(aws, job_id)
    run_id = new_token()
    st, out = call("POST /jobs/{jobId}/run", owner, {"runRequestId": run_id, "mappingVersion": v + 1},
                   path={"jobId": job_id})
    assert st == 202, out
    after = get_job_item(aws, job_id)
    lifecycle_on(before, after)  # stream: RUN_REQUESTED -> DISPATCH_ROWS on ingest queue
    return run_id


def pump_ingest(aws, receive_count=1):
    msgs = drain(aws, "INGEST_QUEUE_URL")
    results = []
    for m in msgs:
        results.append(ingest(json.loads(m["body"]), receive_count))
    return msgs, results


def work_records(aws):
    return drain(aws, "WORK_QUEUE_URL")


def run_worker(records, batch=10):
    from worker import handler
    failures = []
    for i in range(0, len(records), batch):
        failures += handler.handler({"Records": records[i:i + batch]}, None)["batchItemFailures"]
    return failures


def finalize_via_stream(aws, job_id):
    job = get_job_item(aws, job_id)
    return lifecycle_on(None, job)
