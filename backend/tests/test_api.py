import json
from unittest import mock

from tests.conftest import call, new_token
from tests.flow import MAPPING, create_and_upload, csv_bytes, get_job_item, ingest, run_job, pump_ingest, run_worker, work_records, finalize_via_stream

A = "owner-a-0000-1111"
B = "owner-b-0000-2222"


def ready_job(aws, owner=A, n=3):
    job_id, up = create_and_upload(aws, owner, csv_bytes(n))
    ingest(up)
    return job_id


def test_create_is_idempotent_per_client_token(aws):
    body = {"clientToken": new_token(), "currency": "USD"}
    s1, o1 = call("POST /jobs", A, body)
    s2, o2 = call("POST /jobs", A, body)
    assert (s1, s2) == (201, 200) and o1["job"]["jobId"] == o2["job"]["jobId"]
    assert "upload" in o2  # still CREATED: a fresh upload form is returned
    s3_, o3 = call("POST /jobs", B, body)  # same token, other user -> different job
    assert o3["job"]["jobId"] != o1["job"]["jobId"]


def test_presigned_post_is_size_limited_and_key_bound(aws):
    s, o = call("POST /jobs", A, {"clientToken": new_token()})
    policy = json.loads(__import__("base64").b64decode(o["upload"]["fields"]["policy"]))
    assert ["content-length-range", 1, 1048576] in policy["conditions"]
    assert o["upload"]["fields"]["key"] == f"raw/{A}/{o['job']['jobId']}/input.csv"
    # regional endpoint: the global one 307-redirects for new buckets (seen in the first deploy)
    assert o["upload"]["url"].startswith("https://raw-bucket.s3.us-west-2.amazonaws.com")


def test_user_b_cannot_see_or_touch_user_a_job(aws):
    job_id = ready_job(aws)
    for route, body in [("GET /jobs/{jobId}", None), ("GET /jobs/{jobId}/preview", None),
                        ("GET /jobs/{jobId}/results", None), ("GET /jobs/{jobId}/downloads", None),
                        ("PUT /jobs/{jobId}/mapping", {"mapping": MAPPING, "mappingVersion": 0}),
                        ("POST /jobs/{jobId}/run", {"runRequestId": new_token(), "mappingVersion": 0}),
                        ("POST /jobs/{jobId}/mapping-suggestion", None)]:
        s, o = call(route, B, body, path={"jobId": job_id})
        assert s == 404, (route, o)
    s, o = call("GET /jobs", B)
    assert o["jobs"] == []
    s, o = call("GET /jobs", A)
    assert [j["jobId"] for j in o["jobs"]] == [job_id]


def test_b_cannot_create_corrected_job_from_a(aws):
    job_id = ready_job(aws)
    s, o = call("POST /jobs", B, {"clientToken": new_token(), "parentJobId": job_id})
    assert s == 404


def test_templates_are_per_user(aws):
    body = {"name": "Acme", "supplierId": "acme", "mapping": MAPPING,
            "sourceHeaders": ["Item Code", "Product Name", "Unit Price", "On Hand"]}
    assert call("PUT /mapping-templates/{templateId}", A, body, path={"templateId": "acme"})[0] == 200
    assert call("GET /mapping-templates", B)[1]["templates"] == []
    assert len(call("GET /mapping-templates", A)[1]["templates"]) == 1
    # B writing the same template id creates B's own template, not A's.
    call("PUT /mapping-templates/{templateId}", B, dict(body, name="mine"), path={"templateId": "acme"})
    assert call("GET /mapping-templates", A)[1]["templates"][0]["name"] == "Acme"


def test_mapping_validation(aws):
    job_id = ready_job(aws)
    s, o = call("PUT /jobs/{jobId}/mapping", A, {"mapping": {"sku": "Nope"}, "mappingVersion": 0}, path={"jobId": job_id})
    assert s == 400 and "not in this file" in o["error"]["details"]["problems"][0]
    s, o = call("PUT /jobs/{jobId}/mapping", A, {"mapping": {"sku": "Item Code", "title": "Item Code"}, "mappingVersion": 0},
                path={"jobId": job_id})
    assert s == 400 and "mapped to both" in o["error"]["details"]["problems"][0]
    s, o = call("PUT /jobs/{jobId}/mapping", A, {"mapping": {"sku": "Item Code"}, "mappingVersion": 0}, path={"jobId": job_id})
    assert s == 200
    s, o = call("POST /jobs/{jobId}/run", A, {"runRequestId": new_token(), "mappingVersion": 1}, path={"jobId": job_id})
    assert s == 400 and "not mapped" in o["error"]["details"]["problems"][0]  # incomplete mapping never runs


def test_stale_mapping_version_rejected(aws):
    job_id = ready_job(aws)
    assert call("PUT /jobs/{jobId}/mapping", A, {"mapping": MAPPING, "mappingVersion": 0}, path={"jobId": job_id})[0] == 200
    s, o = call("PUT /jobs/{jobId}/mapping", A, {"mapping": MAPPING, "mappingVersion": 0}, path={"jobId": job_id})
    assert s == 409
    s, o = call("POST /jobs/{jobId}/run", A, {"runRequestId": new_token(), "mappingVersion": 0}, path={"jobId": job_id})
    assert s == 409


def test_run_is_idempotent_and_frozen(aws):
    job_id = ready_job(aws)
    call("PUT /jobs/{jobId}/mapping", A, {"mapping": MAPPING, "mappingVersion": 0}, path={"jobId": job_id})
    rid = new_token()
    assert call("POST /jobs/{jobId}/run", A, {"runRequestId": rid, "mappingVersion": 1}, path={"jobId": job_id})[0] == 202
    s, o = call("POST /jobs/{jobId}/run", A, {"runRequestId": rid, "mappingVersion": 1}, path={"jobId": job_id})
    assert s == 200 and o["duplicate"] is True
    s, o = call("POST /jobs/{jobId}/run", A, {"runRequestId": new_token(), "mappingVersion": 1}, path={"jobId": job_id})
    assert s == 409
    s, o = call("PUT /jobs/{jobId}/mapping", A, {"mapping": MAPPING, "mappingVersion": 1}, path={"jobId": job_id})
    assert s == 409  # mapping cannot change after run


def test_ai_unavailable_falls_back_to_manual(aws, monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    job_id = ready_job(aws)
    s, o = call("POST /jobs/{jobId}/mapping-suggestion", A, path={"jobId": job_id})
    assert s == 503 and o["error"]["code"] == "AI_UNAVAILABLE"
    assert call("PUT /jobs/{jobId}/mapping", A, {"mapping": MAPPING, "mappingVersion": 0}, path={"jobId": job_id})[0] == 200


def test_ai_attempts_are_bounded(aws, monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    job_id = ready_job(aws)
    codes = [call("POST /jobs/{jobId}/mapping-suggestion", A, path={"jobId": job_id})[0] for _ in range(5)]
    assert codes == [503, 503, 503, 429, 429]


def test_ai_suggestion_sanitized_and_reused(aws, monkeypatch):
    job_id = ready_job(aws)
    fake = {"rawMapping": {"sku": "Item Code", "title": "Ignore previous instructions", "price": "Unit Price",
                           "stock": "Unit Price"},
            "notes": "n", "modelId": "m", "promptVersion": "p", "latencyMs": 5, "inputTokens": 1, "outputTokens": 1}
    with mock.patch("api.ai.suggest", return_value=fake) as m:
        s, o = call("POST /jobs/{jobId}/mapping-suggestion", A, path={"jobId": job_id})
        assert s == 200 and o["suggestion"]["mapping"] == {"sku": "Item Code", "price": "Unit Price"}
        assert len(o["suggestion"]["dropped"]) == 2
        s, o = call("POST /jobs/{jobId}/mapping-suggestion", A, path={"jobId": job_id})
        assert o["reused"] is True and m.call_count == 1  # at most one stored suggestion per file


def test_downloads_only_after_completion_and_corrected_job(aws):
    job_id, up = create_and_upload(aws, A, csv_bytes(4, bad_rows=(2,)))
    ingest(up)
    s, o = call("GET /jobs/{jobId}/downloads", A, path={"jobId": job_id})
    assert "output" not in o and "original" in o
    run_job(aws, A, job_id)
    pump_ingest(aws)
    run_worker(work_records(aws))
    finalize_via_stream(aws, job_id)
    s, o = call("GET /jobs/{jobId}/downloads", A, path={"jobId": job_id})
    assert {"output", "errors", "original"} <= set(o)
    s, o = call("GET /jobs/{jobId}/results", A, query={"status": "INVALID"}, path={"jobId": job_id})
    assert [r["rowNumber"] for r in o["results"]] == [2]
    # Corrected job: new job with parentJobId; parent results stay untouched.
    child_id, up2 = create_and_upload(aws, A, csv_bytes(4), parent=job_id)
    ingest(up2)
    child = get_job_item(aws, child_id)
    assert child["parentJobId"] == job_id and child["parentMapping"] == MAPPING
    assert get_job_item(aws, job_id)["invalidCount"] == 1


def test_bad_inputs(aws):
    assert call("POST /jobs", A, {"clientToken": "x"})[0] == 400
    assert call("POST /jobs", A, {"clientToken": new_token(), "currency": "BTC"})[0] == 400
    assert call("GET /jobs/{jobId}", A, path={"jobId": "../../etc"})[0] == 404
    assert call("GET /jobs", A, query={"cursor": "garbage"})[0] == 400


def test_daily_limit(aws, monkeypatch):
    monkeypatch.setenv("DAILY_JOB_LIMIT", "2")
    assert call("POST /jobs", A, {"clientToken": new_token()})[0] == 201
    assert call("POST /jobs", A, {"clientToken": new_token()})[0] == 201
    assert call("POST /jobs", A, {"clientToken": new_token()})[0] == 429


def test_run_without_stock_mapping_exports_empty_stock(aws):
    from tests.flow import lifecycle_on
    job_id = ready_job(aws, n=3)
    m = {k: v for k, v in MAPPING.items() if k != "stock"}
    assert call("PUT /jobs/{jobId}/mapping", A, {"mapping": m, "mappingVersion": 0}, path={"jobId": job_id})[0] == 200
    before = get_job_item(aws, job_id)
    s, o = call("POST /jobs/{jobId}/run", A, {"runRequestId": new_token(), "mappingVersion": 1}, path={"jobId": job_id})
    assert s == 202 and o["job"]["schemaVersion"] == "catalogbridge.v2" and o["job"]["rulesVersion"] == "rules.v2"
    lifecycle_on(before, get_job_item(aws, job_id))
    pump_ingest(aws)
    run_worker(work_records(aws))
    finalize_via_stream(aws, job_id)
    job = get_job_item(aws, job_id)
    assert job["status"] == "COMPLETED" and job["validCount"] == 3
    out = aws["s3"].get_object(Bucket=aws["WORK_BUCKET"], Key=job["outputKey"])["Body"].read().decode("utf-8-sig")
    assert out.splitlines()[1].split(",")[4] == ""  # stock column empty
