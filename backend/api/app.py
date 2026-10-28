"""HTTP API Lambda (API Gateway HTTP API, payload v2, JWT authorizer).

Authorization model: API Gateway validates the Cognito JWT (signature, issuer,
audience, expiry). This code takes ownerId ONLY from the verified `sub` claim
and checks ownership on every job/template access. A client-supplied owner is
never trusted. A job that belongs to someone else returns 404 (not 403) so
job IDs cannot be probed.
"""

import base64
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key

from api import ai
from shared import mapping as mapping_mod
from shared import obs, schema
from shared.aws import client, jobs_table, results_table, templates_table
from shared.ddbutil import as_int, is_conditional_fail, now_ms, plain

_TOKEN = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SUPPLIER = re.compile(r"^[\w .&()-]{0,60}$")
_JOB_NS = uuid.UUID("6f1c7a52-3b0e-4b8e-9d55-2d0f6f5f2a10")
AI_MAX_ATTEMPTS = 3
PAGE = 20


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def resp(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(plain(body), ensure_ascii=False)}


def _body(event) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if len(raw) > 64_000:
        raise ApiError(413, "BODY_TOO_LARGE", "Request body is too large.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(400, "BAD_JSON", "Request body must be JSON.")
    if not isinstance(data, dict):
        raise ApiError(400, "BAD_JSON", "Request body must be a JSON object.")
    return data


def _owner(event) -> str:
    try:
        sub = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except KeyError:
        raise ApiError(401, "UNAUTHENTICATED", "Missing identity.")
    if not _TOKEN.match(sub):
        raise ApiError(401, "UNAUTHENTICATED", "Bad identity.")
    return sub


def _cursor_encode(key):
    return base64.urlsafe_b64encode(json.dumps(plain(key)).encode()).decode() if key else None


def _cursor_decode(cursor, owner=None):
    if not cursor:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception:
        raise ApiError(400, "BAD_CURSOR", "Invalid cursor.")
    if owner is not None and key.get("ownerId") != owner:
        raise ApiError(400, "BAD_CURSOR", "Invalid cursor.")
    return key


def load_owned_job(owner, job_id) -> dict:
    if not _TOKEN.match(job_id or ""):
        raise ApiError(404, "NOT_FOUND", "Job not found.")
    job = jobs_table().get_item(Key={"jobId": job_id}, ConsistentRead=True).get("Item")
    if not job or job.get("ownerId") != owner:
        raise ApiError(404, "NOT_FOUND", "Job not found.")
    return job


def raw_key(owner, job_id):
    return f"raw/{owner}/{job_id}/input.csv"


def upload_form(owner, job_id):
    post = client("s3").generate_presigned_post(
        Bucket=os.environ["RAW_BUCKET"],
        Key=raw_key(owner, job_id),
        Conditions=[["content-length-range", 1, schema.MAX_FILE_BYTES]],
        ExpiresIn=300,
    )
    return {"url": post["url"], "fields": post["fields"], "maxBytes": schema.MAX_FILE_BYTES, "expiresInSeconds": 300}


def allowed_actions(job) -> list:
    s = job["status"]
    acts = []
    if s == schema.CREATED:
        acts.append("upload")
    if s == schema.AWAITING_MAPPING:
        acts += ["edit_mapping", "suggest_mapping", "run"]
    if s in (schema.COMPLETED, schema.COMPLETED_WITH_ERRORS):
        acts += ["download", "create_corrected_job"]
    if s == schema.REJECTED:
        acts.append("create_corrected_job")
    return acts


def job_view(job) -> dict:
    expected = as_int(job.get("expectedCount"))
    processed = as_int(job.get("processedCount"))
    stall_ms = int(os.environ.get("STALL_MINUTES", "10")) * 60_000
    active = job["status"] in schema.ACTIVE_RUN_STATUSES | {schema.VALIDATING_UPLOAD}
    view = {k: job.get(k) for k in (
        "jobId", "status", "supplierId", "currency", "fileName", "createdAt", "updatedAt", "parentJobId",
        "rowCount", "headers", "rejectReasons", "schemaVersion", "rulesVersion", "mappingVersion",
        "mappingSource", "runRequestId", "expectedCount", "processedCount", "validCount", "invalidCount",
        "dispatchComplete", "runRequestedAt", "completedAt", "templateId", "aiAdopted")}
    view["mapping"] = job.get("frozenMapping") or job.get("draftMapping") or {}
    view["mappingFrozen"] = "frozenMapping" in job
    sug = job.get("aiSuggestion")
    if sug:
        view["aiSuggestion"] = {k: sug.get(k) for k in ("mapping", "dropped", "notes", "modelId", "promptVersion", "latencyMs")}
    view["progress"] = round(processed / expected, 4) if expected else 0
    view["stalled"] = bool(active and now_ms() - as_int(job.get("updatedAt")) > stall_ms)
    view["allowedActions"] = allowed_actions(job)
    return view


# ---------------------------------------------------------------- jobs

def create_job(owner, event, _):
    body = _body(event)
    token = body.get("clientToken", "")
    if not _TOKEN.match(token):
        raise ApiError(400, "BAD_CLIENT_TOKEN", "clientToken (8-64 chars, letters/digits/-) is required for idempotent create.")
    currency = body.get("currency", "USD")
    if currency not in schema.CURRENCIES:
        raise ApiError(400, "BAD_CURRENCY", "Unsupported currency.", {"allowed": sorted(schema.CURRENCIES)})
    supplier = (body.get("supplierId") or "").strip()
    if not _SUPPLIER.match(supplier):
        raise ApiError(400, "BAD_SUPPLIER", "supplierId may use letters, digits, spaces and . & ( ) - (max 60).")
    file_name = str(body.get("fileName") or "input.csv")[:120]
    parent_id = body.get("parentJobId")
    parent = None
    if parent_id:
        parent = load_owned_job(owner, parent_id)
        if parent["status"] not in schema.TERMINAL_STATUSES:
            raise ApiError(409, "PARENT_NOT_FINISHED", "A corrected job can only be created from a finished job.")

    job_id = str(uuid.uuid5(_JOB_NS, f"{owner}:{token}"))
    existing = jobs_table().get_item(Key={"jobId": job_id}, ConsistentRead=True).get("Item")
    if existing:
        if existing["ownerId"] != owner:
            raise ApiError(409, "CONFLICT", "Token collision; retry with a new clientToken.")
        out = {"job": job_view(existing)}
        if existing["status"] == schema.CREATED:
            out["upload"] = upload_form(owner, job_id)
        return resp(200, out)

    day_start = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    count = jobs_table().query(IndexName="byOwner", Select="COUNT",
                               KeyConditionExpression=Key("ownerId").eq(owner) & Key("createdAt").gte(day_start))["Count"]
    limit = int(os.environ.get("DAILY_JOB_LIMIT", "30"))
    if count >= limit:
        raise ApiError(429, "DAILY_LIMIT", f"Daily limit of {limit} jobs reached.")

    t = now_ms()
    item = {
        "jobId": job_id, "ownerId": owner, "status": schema.CREATED, "createdAt": t, "updatedAt": t,
        "supplierId": supplier or (parent or {}).get("supplierId") or "", "currency": currency, "fileName": file_name,
        "fileKey": raw_key(owner, job_id), "schemaVersion": schema.SCHEMA_VERSION, "rulesVersion": schema.RULES_VERSION,
        "mappingVersion": 0, "aiAttempts": 0,
    }
    if parent:
        item["parentJobId"] = parent["jobId"]
        pm = parent.get("frozenMapping") or parent.get("draftMapping")
        if pm:
            item["parentMapping"] = pm
    try:
        jobs_table().put_item(Item=item, ConditionExpression="attribute_not_exists(jobId)")
    except Exception as e:
        if not is_conditional_fail(e):
            raise
        return create_job(owner, event, _)  # concurrent double-click: return the winner
    obs.info("job created", stage="api", jobId=job_id, parentJobId=item.get("parentJobId"))
    return resp(201, {"job": job_view(item), "upload": upload_form(owner, job_id)})


def list_jobs(owner, event, _):
    q = event.get("queryStringParameters") or {}
    kwargs = {"IndexName": "byOwner", "KeyConditionExpression": Key("ownerId").eq(owner),
              "ScanIndexForward": False, "Limit": PAGE}
    start = _cursor_decode(q.get("cursor"), owner)
    if start:
        kwargs["ExclusiveStartKey"] = start
    r = jobs_table().query(**kwargs)
    return resp(200, {"jobs": [job_view(j) for j in r.get("Items", [])],
                      "nextCursor": _cursor_encode(r.get("LastEvaluatedKey"))})


def get_job(owner, event, job_id):
    return resp(200, {"job": job_view(load_owned_job(owner, job_id))})


def _manifest(job):
    if not job.get("manifestKey"):
        raise ApiError(409, "NOT_READY", "File has not been validated yet.", {"status": job["status"]})
    body = client("s3").get_object(Bucket=os.environ["WORK_BUCKET"], Key=job["manifestKey"])["Body"].read()
    return json.loads(body)


def get_preview(owner, event, job_id):
    job = load_owned_job(owner, job_id)
    m = _manifest(job)
    q = event.get("queryStringParameters") or {}
    rows = m["rows"] if q.get("all") == "1" else m["rows"][:schema.PREVIEW_ROWS]
    return resp(200, {"headers": m["headers"], "rows": rows, "rowCount": m["rowCount"],
                      "sourceSignature": m["sourceSignature"], "parentMapping": job.get("parentMapping")})


def mapping_suggestion(owner, event, job_id):
    job = load_owned_job(owner, job_id)
    if job["status"] != schema.AWAITING_MAPPING:
        raise ApiError(409, "BAD_STATE", "Suggestions are only available before the run.", {"status": job["status"]})
    if job.get("aiSuggestion"):
        return resp(200, {"suggestion": job_view(job)["aiSuggestion"], "reused": True})
    try:  # bounded attempts per job, enforced atomically in the backend
        jobs_table().update_item(
            Key={"jobId": job_id}, UpdateExpression="ADD aiAttempts :one",
            ConditionExpression="attribute_not_exists(aiSuggestion) AND (attribute_not_exists(aiAttempts) OR aiAttempts < :max)",
            ExpressionAttributeValues={":one": 1, ":max": AI_MAX_ATTEMPTS},
        )
    except Exception as e:
        if not is_conditional_fail(e):
            raise
        raise ApiError(429, "AI_LIMIT", "AI suggestion limit reached for this job; please map manually.")
    m = _manifest(job)
    try:
        s = ai.suggest(m["headers"], m["rows"])
    except ai.AiUnavailable as e:
        obs.warn("ai suggestion unavailable", stage="api", jobId=job_id, error=str(e))
        obs.metric("AiSuggestionFailures")
        raise ApiError(503, "AI_UNAVAILABLE", "AI suggestion is unavailable right now. You can map the columns manually.")
    clean, dropped = ai.sanitize(s["rawMapping"], m["headers"])
    record = {"mapping": clean, "dropped": dropped, "notes": s["notes"], "modelId": s["modelId"],
              "promptVersion": s["promptVersion"], "schemaVersion": schema.SCHEMA_VERSION,
              "latencyMs": s["latencyMs"], "inputTokens": s["inputTokens"], "outputTokens": s["outputTokens"],
              "createdAt": now_ms()}
    try:  # at most one stored suggestion per job; a concurrent caller reuses it
        jobs_table().update_item(Key={"jobId": job_id}, UpdateExpression="SET aiSuggestion = :s",
                                 ConditionExpression="attribute_not_exists(aiSuggestion)",
                                 ExpressionAttributeValues={":s": record})
    except Exception as e:
        if not is_conditional_fail(e):
            raise
        job = load_owned_job(owner, job_id)
        return resp(200, {"suggestion": job_view(job)["aiSuggestion"], "reused": True})
    obs.info("ai suggestion stored", stage="api", jobId=job_id, latencyMs=s["latencyMs"], mapped=len(clean),
             dropped=len(dropped), inputTokens=s["inputTokens"], outputTokens=s["outputTokens"])
    obs.metric("AiSuggestionLatencyMs", s["latencyMs"], unit="Milliseconds")
    return resp(200, {"suggestion": {k: record[k] for k in ("mapping", "dropped", "notes", "modelId", "promptVersion", "latencyMs")},
                      "reused": False})


def put_mapping(owner, event, job_id):
    job = load_owned_job(owner, job_id)
    body = _body(event)
    if job["status"] != schema.AWAITING_MAPPING:
        raise ApiError(409, "BAD_STATE", "Mapping can only change before the run.", {"status": job["status"]})
    source = body.get("source", "manual")
    if source not in ("manual", "ai", "template", "parent"):
        raise ApiError(400, "BAD_SOURCE", "source must be manual, ai, template or parent.")
    try:
        clean = mapping_mod.check_mapping(body.get("mapping"), job["headers"], require_complete=False)
    except mapping_mod.MappingError as e:
        raise ApiError(400, "BAD_MAPPING", "Mapping is not valid for this file.", {"problems": e.problems})
    expected_version = body.get("mappingVersion")
    if not isinstance(expected_version, int):
        raise ApiError(400, "BAD_VERSION", "mappingVersion (the version you edited) is required.")
    template_id = body.get("templateId") if source == "template" else None
    try:
        r = jobs_table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET draftMapping = :m, mappingSource = :src, templateId = :tid, aiAdopted = :ai, "
                             "updatedAt = :t ADD mappingVersion :one",
            ConditionExpression="#s = :am AND mappingVersion = :v",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":m": clean, ":src": source, ":tid": template_id, ":ai": source == "ai",
                                       ":t": now_ms(), ":one": 1, ":am": schema.AWAITING_MAPPING,
                                       ":v": expected_version},
            ReturnValues="ALL_NEW",
        )
    except Exception as e:
        if not is_conditional_fail(e):
            raise
        raise ApiError(409, "STALE_MAPPING", "The job changed since you loaded it. Reload and try again.")
    return resp(200, {"job": job_view(r["Attributes"])})


def run_job(owner, event, job_id):
    job = load_owned_job(owner, job_id)
    body = _body(event)
    run_id = body.get("runRequestId", "")
    if not _TOKEN.match(run_id):
        raise ApiError(400, "BAD_RUN_ID", "runRequestId (8-64 chars) is required.")
    if job.get("runRequestId"):
        if job["runRequestId"] == run_id:
            return resp(200, {"job": job_view(job), "duplicate": True})  # retried click
        raise ApiError(409, "ALREADY_RUN", "This job has already been run. Create a corrected job instead.")
    if job["status"] != schema.AWAITING_MAPPING:
        raise ApiError(409, "BAD_STATE", "Job is not ready to run.", {"status": job["status"]})
    try:
        clean = mapping_mod.check_mapping(job.get("draftMapping") or {}, job["headers"], require_complete=True)
    except mapping_mod.MappingError as e:
        raise ApiError(400, "BAD_MAPPING", "Mapping is incomplete or invalid.", {"problems": e.problems})
    if body.get("mappingVersion") != as_int(job.get("mappingVersion")):
        raise ApiError(409, "STALE_MAPPING", "You are running an older mapping version. Reload and confirm again.")
    t = now_ms()
    try:
        # The durable run request. Everything written here is immutable afterwards.
        r = jobs_table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #s = :rr, frozenMapping = :m, runRequestId = :r, expectedCount = :n, "
                             "schemaVersion = :sv, rulesVersion = :rv, "
                             "processedCount = :zero, validCount = :zero, invalidCount = :zero, "
                             "dispatchComplete = :false, runRequestedAt = :t, updatedAt = :t",
            ConditionExpression="#s = :am AND mappingVersion = :v AND attribute_not_exists(runRequestId)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":rr": schema.RUN_REQUESTED, ":m": clean, ":r": run_id,
                                       ":sv": schema.SCHEMA_VERSION, ":rv": schema.RULES_VERSION,
                                       ":n": as_int(job["rowCount"]), ":zero": 0, ":false": False, ":t": t,
                                       ":am": schema.AWAITING_MAPPING, ":v": as_int(job.get("mappingVersion"))},
            ReturnValues="ALL_NEW",
        )
    except Exception as e:
        if not is_conditional_fail(e):
            raise
        latest = load_owned_job(owner, job_id)
        if latest.get("runRequestId") == run_id:
            return resp(200, {"job": job_view(latest), "duplicate": True})
        raise ApiError(409, "CONFLICT", "The job changed while starting the run. Reload and try again.")
    obs.info("run requested", stage="api", jobId=job_id, runRequestId=run_id, rows=as_int(job["rowCount"]))
    return resp(202, {"job": job_view(r["Attributes"])})


def get_results(owner, event, job_id):
    load_owned_job(owner, job_id)
    q = event.get("queryStringParameters") or {}
    status_filter = q.get("status", "ALL")
    if status_filter not in ("ALL", "VALID", "INVALID"):
        raise ApiError(400, "BAD_FILTER", "status must be ALL, VALID or INVALID.")
    kwargs = {"KeyConditionExpression": Key("jobId").eq(job_id), "Limit": 50, "ConsistentRead": True}
    if status_filter != "ALL":
        kwargs["FilterExpression"] = "resultStatus = :rs"
        kwargs["ExpressionAttributeValues"] = {":rs": status_filter}
        kwargs["Limit"] = 200  # filter applies after Limit; read more per page
    start = _cursor_decode(q.get("cursor"))
    if start:
        if start.get("jobId") != job_id:
            raise ApiError(400, "BAD_CURSOR", "Invalid cursor.")
        kwargs["ExclusiveStartKey"] = start
    r = results_table().query(**kwargs)
    items = [{k: it.get(k) for k in ("rowNumber", "resultStatus", "normalizedData", "validationErrors",
                                     "sourceData", "processedAt")} for it in r.get("Items", [])]
    return resp(200, {"results": items, "nextCursor": _cursor_encode(r.get("LastEvaluatedKey"))})


def get_downloads(owner, event, job_id):
    job = load_owned_job(owner, job_id)
    s3 = client("s3")
    out = {"expiresInSeconds": 300}

    def sign(bucket, key, name, version=None):
        params = {"Bucket": bucket, "Key": key, "ResponseContentDisposition": f'attachment; filename="{name}"'}
        if version:
            params["VersionId"] = version
        return s3.generate_presigned_url("get_object", Params=params, ExpiresIn=300)

    short = job_id[:8]
    if job.get("fileVersion"):
        out["original"] = sign(os.environ["RAW_BUCKET"], job["fileKey"], f"original-{short}.csv", job["fileVersion"])
    if job["status"] in (schema.COMPLETED, schema.COMPLETED_WITH_ERRORS):
        out["output"] = sign(os.environ["WORK_BUCKET"], job["outputKey"], f"{schema.SCHEMA_VERSION}-{short}.csv")
        out["errors"] = sign(os.environ["WORK_BUCKET"], job["errorsKey"], f"errors-{short}.csv")
    return resp(200, out)


# ---------------------------------------------------------------- templates

def list_templates(owner, event, _):
    r = templates_table().query(KeyConditionExpression=Key("ownerId").eq(owner), Limit=100)
    return resp(200, {"templates": [{k: t.get(k) for k in ("templateId", "name", "supplierId", "mapping",
                                                           "sourceHeaders", "sourceSignature", "version", "updatedAt")}
                                    for t in r.get("Items", [])]})


def put_template(owner, event, template_id):
    if not _TEMPLATE_ID.match(template_id or ""):
        raise ApiError(400, "BAD_TEMPLATE_ID", "Template id: lowercase letters, digits and -, max 40.")
    body = _body(event)
    headers = body.get("sourceHeaders")
    if not isinstance(headers, list) or not headers or len(headers) > schema.MAX_COLUMNS \
            or not all(isinstance(h, str) and 0 < len(h) <= schema.MAX_HEADER_LEN for h in headers):
        raise ApiError(400, "BAD_HEADERS", "sourceHeaders must be the list of the file's column names.")
    try:
        clean = mapping_mod.check_mapping(body.get("mapping"), headers, require_complete=False)
    except mapping_mod.MappingError as e:
        raise ApiError(400, "BAD_MAPPING", "Template mapping is not valid.", {"problems": e.problems})
    name = str(body.get("name") or template_id)[:80]
    supplier = (body.get("supplierId") or "").strip()
    if not _SUPPLIER.match(supplier):
        raise ApiError(400, "BAD_SUPPLIER", "Invalid supplierId.")
    count = templates_table().query(KeyConditionExpression=Key("ownerId").eq(owner), Select="COUNT")["Count"]
    from shared.csvparse import header_signature
    t = now_ms()
    try:
        r = templates_table().update_item(
            Key={"ownerId": owner, "templateId": template_id},
            UpdateExpression="SET #n = :n, supplierId = :sup, mapping = :m, sourceHeaders = :h, "
                             "sourceSignature = :sig, updatedAt = :t ADD version :one",
            ConditionExpression="attribute_exists(templateId) OR :count < :max",
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={":n": name, ":sup": supplier, ":m": clean, ":h": headers,
                                       ":sig": header_signature(headers), ":t": t, ":one": 1,
                                       ":count": count, ":max": 50},
            ReturnValues="ALL_NEW",
        )
    except Exception as e:
        if not is_conditional_fail(e):
            raise
        raise ApiError(429, "TEMPLATE_LIMIT", "Template limit (50) reached.")
    a = r["Attributes"]
    return resp(200, {"template": {k: a.get(k) for k in ("templateId", "name", "supplierId", "mapping",
                                                         "sourceHeaders", "sourceSignature", "version", "updatedAt")}})


ROUTES = {
    "POST /jobs": create_job,
    "GET /jobs": list_jobs,
    "GET /jobs/{jobId}": get_job,
    "GET /jobs/{jobId}/preview": get_preview,
    "POST /jobs/{jobId}/mapping-suggestion": mapping_suggestion,
    "PUT /jobs/{jobId}/mapping": put_mapping,
    "POST /jobs/{jobId}/run": run_job,
    "GET /jobs/{jobId}/results": get_results,
    "GET /jobs/{jobId}/downloads": get_downloads,
    "GET /mapping-templates": list_templates,
    "PUT /mapping-templates/{templateId}": put_template,
}


def handler(event, context):
    started = time.time()
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    try:
        fn = ROUTES.get(route)
        if not fn:
            raise ApiError(404, "NO_ROUTE", "Not found.")
        owner = _owner(event)
        result = fn(owner, event, params.get("jobId") or params.get("templateId"))
    except ApiError as e:
        result = resp(e.status, {"error": {"code": e.code, "message": e.message, "details": e.details}})
    except Exception as e:
        obs.error("unhandled api error", route=route, errorType=type(e).__name__, error=str(e)[:300])
        result = resp(500, {"error": {"code": "INTERNAL", "message": "Internal error."}})
    obs.info("api request", stage="api", route=route, status=result["statusCode"],
             durationMs=int((time.time() - started) * 1000))
    return result
