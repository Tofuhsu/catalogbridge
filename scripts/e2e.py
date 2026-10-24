"""End-to-end checks against the DEPLOYED stack, through the public API and S3 (no shortcuts).

  python scripts/e2e.py            # writes docs/evidence/e2e-<timestamp>.json

Creates two throwaway Cognito users (A, B), deletes them at the end.
"""

import csv
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

from cb import STD, TestUser, fixture, outputs, save_evidence, urlopen

checks = []


def check(name, ok, **detail):
    checks.append({"check": name, "pass": bool(ok), **detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))


def download(url) -> bytes:
    with urlopen(url, timeout=30) as r:
        return r.read()


def run_file(api, name, mapping=STD, **kw):
    job_id = api.create_and_upload(fixture(name), name=name, **kw)
    job = api.wait(job_id, {"AWAITING_MAPPING", "REJECTED"})
    if job["status"] == "REJECTED":
        return job
    t0 = time.time()
    api.map_and_run(job_id, mapping)
    job = api.wait(job_id, {"COMPLETED", "COMPLETED_WITH_ERRORS"}, timeout=300)
    job["_clientSeconds"] = round(time.time() - t0, 2)
    return job


def main():
    started = time.strftime("%Y%m%dT%H%M%S")
    with TestUser("a") as A, TestUser("b") as B:
        a, b = A.api, B.api

        # 1. 500-row happy path, stable report order
        job = run_file(a, "rows_500.csv")
        st, d = a.call("GET", f"/jobs/{job['jobId']}/downloads")
        out = download(d["output"]).decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(out)))
        check("500 rows complete with consistent counters",
              job["status"] == "COMPLETED" and job["processedCount"] == job["validCount"] == 500 and len(rows) == 501,
              status=job["status"], processed=job["processedCount"], outputRows=len(rows) - 1,
              endToEndSeconds=(job["completedAt"] - job["runRequestedAt"]) / 1000)
        check("report sorted by source row", [int(r[-1]) for r in rows[1:]] == list(range(1, 501)))
        again = download(a.call("GET", f"/jobs/{job['jobId']}/downloads")[1]["output"]).decode("utf-8-sig")
        check("report download is byte-identical on repeat", again == out)
        happy_id = job["jobId"]

        # 2. business errors -> INVALID, not retried
        job = run_file(a, "mixed_errors.csv")
        st, r = a.call("GET", f"/jobs/{job['jobId']}/results?status=INVALID")
        codes = sorted({e["code"] for it in r["results"] for e in it["validationErrors"]})
        check("business errors become INVALID results", job["status"] == "COMPLETED_WITH_ERRORS"
              and job["invalidCount"] == 5 and job["validCount"] == 7,
              valid=job["validCount"], invalid=job["invalidCount"], codes=codes)
        mixed_id = job["jobId"]

        # 3. corrected job flow
        src = fixture("mixed_errors.csv").decode()
        fixed = (src.replace("-5.00", "5.00").replace(",2.5\r\n", ",2\r\n").replace("$12", "12.00"))
        lines = fixed.split("\r\n")
        lines[4] = "SKU-FIXED-4" + lines[4]  # row 4 had an empty SKU
        lines[8] = lines[8].replace(lines[8].split(",")[0], "SKU-FIXED-8", 1)  # duplicate SKU
        child = a.create_and_upload("\r\n".join(lines).encode(), parent=mixed_id, name="mixed_errors-corrected.csv")
        a.wait(child, {"AWAITING_MAPPING"})
        st, pv = a.call("GET", f"/jobs/{child}/preview")
        a.map_and_run(child, pv["parentMapping"], source="parent")
        cj = a.wait(child, {"COMPLETED", "COMPLETED_WITH_ERRORS"})
        parent_after = a.call("GET", f"/jobs/{mixed_id}")[1]["job"]
        check("corrected job completes and keeps parent unchanged",
              cj["status"] == "COMPLETED" and cj["parentJobId"] == mixed_id and parent_after["invalidCount"] == 5,
              child=cj["status"], childValid=cj["validCount"], childInvalid=cj["invalidCount"])

        # 4. structural rejections: nothing dispatched
        for name, needle in [("empty.csv", None), ("rows_501.csv", "limit is 500"),
                             ("duplicate_header.csv", "Duplicate header"), ("not_utf8.csv", "UTF-8")]:
            if name == "empty.csv":
                st, outj = a.call("POST", "/jobs", {"clientToken": str(uuid.uuid4())})
                code = a.upload(outj["upload"], b"")
                check("empty file refused by upload policy (content-length-range)", code == 400, s3Status=code)
                continue
            job = run_file(a, name)
            st, r = a.call("GET", f"/jobs/{job['jobId']}/results")
            check(f"structural reject: {name}", job["status"] == "REJECTED" and any(needle in x for x in job["rejectReasons"])
                  and r["results"] == [], reasons=job.get("rejectReasons"))
        job = run_file(a, "quoted_newlines.csv")
        check("BOM + quoted newlines parsed", job["status"] == "COMPLETED" and job["validCount"] == 2)

        # 5. upload size limit enforced by S3, not only the browser
        st, outj = a.call("POST", "/jobs", {"clientToken": str(uuid.uuid4())})
        code = a.upload(outj["upload"], b"a,b\r\n" + b"x" * 1_100_000)
        check("upload > 1 MB rejected by S3 policy", code == 400, s3Status=code)

        # 6. isolation: B cannot read/modify/download A's job or see A's templates
        a.call("PUT", "/mapping-templates/acme", {"name": "Acme", "supplierId": "acme", "mapping": STD,
                                                  "sourceHeaders": ["SKU", "Title", "Price", "Stock"]})
        denied = {}
        for m, p, body in [("GET", f"/jobs/{happy_id}", None), ("GET", f"/jobs/{happy_id}/preview", None),
                           ("GET", f"/jobs/{happy_id}/results", None), ("GET", f"/jobs/{happy_id}/downloads", None),
                           ("PUT", f"/jobs/{happy_id}/mapping", {"mapping": STD, "mappingVersion": 1}),
                           ("POST", f"/jobs/{happy_id}/run", {"runRequestId": str(uuid.uuid4()), "mappingVersion": 1}),
                           ("POST", f"/jobs/{happy_id}/mapping-suggestion", None),
                           ("POST", "/jobs", {"clientToken": str(uuid.uuid4()), "parentJobId": happy_id})]:
            denied[f"{m} {p.replace(happy_id, '{A-job}')}"] = b.call(m, p, body)[0]
        b_jobs = b.call("GET", "/jobs")[1]["jobs"]
        b_tpl = b.call("GET", "/mapping-templates")[1]["templates"]
        check("user B gets 404 on every A-job route", all(v == 404 for v in denied.values()), statuses=denied)
        check("user B lists none of A's jobs/templates", b_jobs == [] and b_tpl == [])
        raw_url = f"https://{outputs()['RawBucket']}.s3.amazonaws.com/raw/"
        try:
            urlopen(raw_url, timeout=10)
            public = True
        except urllib.error.HTTPError as e:
            public = e.code not in (403,)
        check("buckets are not publicly readable", not public)
        st, _ = b.call("GET", "/jobs")
        import urllib.request as ur
        req = ur.Request(outputs()["ApiUrl"] + "/jobs")
        try:
            urlopen(req, timeout=10)
            unauth = 200
        except urllib.error.HTTPError as e:
            unauth = e.code
        check("API rejects requests without a token", unauth == 401, status=unauth)

        # 7. idempotency of create and run
        token = str(uuid.uuid4())
        s1, o1 = a.call("POST", "/jobs", {"clientToken": token})
        s2, o2 = a.call("POST", "/jobs", {"clientToken": token})
        check("double-click create returns the same job", o1["job"]["jobId"] == o2["job"]["jobId"] and (s1, s2) == (201, 200))

        result = {"startedAt": started, "region": outputs().get("ApiUrl", "").split(".")[2] if "." in outputs().get("ApiUrl", "") else None,
                  "checks": checks, "passed": sum(c["pass"] for c in checks), "total": len(checks)}
        path = save_evidence(f"e2e-{started}.json", result)
        print(f"\n{result['passed']}/{result['total']} checks passed -> {path}")
        return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
