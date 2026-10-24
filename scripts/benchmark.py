"""Throughput / latency measurement for the current Worker mapping config (spec §11.2).

  python scripts/benchmark.py --jobs 4 --runs 3 --label b10-c2

Fixed inputs: fixtures/rows_500.csv, same region, memory, code and retry settings; only the
worker batch size / max concurrency differ between configs (changed with
`cdk deploy -c workerBatchSize=.. -c workerMaxConcurrency=..`). Each run submits N independent
500-row jobs at once (the product limit is 500 rows per job).

Stop conditions: abort a run if any job is not terminal after 15 minutes; never more than
--jobs x 500 rows per run. Writes docs/evidence/bench-<label>-<timestamp>.json with raw numbers.
"""

import argparse
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

from cb import STD, TestUser, aws, fixture, outputs, save_evidence
from experiments import aws_metric_max, consistency, metric_sum, results_of


def pct(vals, p):
    vals = sorted(vals)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def esm_config():
    lam = aws("lambda")
    fn = outputs()["WorkerFunctionName"]
    m = lam.list_event_source_mappings(FunctionName=fn)["EventSourceMappings"][0]
    f = lam.get_function_configuration(FunctionName=fn)
    return {"batchSize": m["BatchSize"], "maxConcurrency": m.get("ScalingConfig", {}).get("MaximumConcurrency"),
            "batchingWindowS": m.get("MaximumBatchingWindowInSeconds"), "memoryMB": f["MemorySize"],
            "timeoutS": f["Timeout"], "codeSha256": f["CodeSha256"], "runtime": f["Runtime"], "arch": f["Architectures"]}


def one_run(api, n_jobs):
    data = fixture("rows_500.csv")
    ids = [api.create_and_upload(data, name="rows_500.csv") for _ in range(n_jobs)]
    for j in ids:
        api.wait(j, {"AWAITING_MAPPING"})
    threads = [threading.Thread(target=api.map_and_run, args=(j, STD)) for j in ids]
    t0 = time.time()
    [t.start() for t in threads]
    [t.join() for t in threads]
    for j in ids:
        api.wait(j, {"COMPLETED", "COMPLETED_WITH_ERRORS"}, timeout=900, poll=2)
    wall = time.time() - t0
    jobs = [consistency(j) for j in ids]
    row_lat, queue_wait, first_start, last_done = [], [], None, None
    from experiments import job_item
    for j in ids:
        job = job_item(j)
        rr = int(job["runRequestedAt"])
        first_start = rr if first_start is None else min(first_start, rr)
        for r in results_of(j):
            row_lat.append(int(r["processedAt"]) - rr)
            queue_wait.append(int(r["queueWaitMs"]))
            last_done = int(r["processedAt"]) if last_done is None else max(last_done, int(r["processedAt"]))
    rows = n_jobs * 500
    span_s = (last_done - first_start) / 1000
    return {
        "jobs": n_jobs, "rows": rows, "clientWallSeconds": round(wall, 2),
        "rowsPerSecond_firstRunRequestToLastRow": round(rows / span_s, 2),
        "jobEndToEndSeconds": [j["endToEndSeconds"] for j in jobs],
        "rowLatencyMs": {"p50": pct(row_lat, 50), "p95": pct(row_lat, 95), "max": max(row_lat)},
        "queueWaitMs": {"p50": pct(queue_wait, 50), "p95": pct(queue_wait, 95)},
        "allConsistent": all(j["countersMatchResults"] and j["processedCount"] == 500 for j in jobs),
        "receiveCountHistogram": [j["resultReceiveCountHistogram"] for j in jobs],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--label", required=True)
    a = p.parse_args()
    cfg = esm_config()
    print("config:", cfg)
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    runs = []
    with TestUser("bench") as U:
        for i in range(a.runs):
            r = one_run(U.api, a.jobs)
            print(f"run {i + 1}: {r['rowsPerSecond_firstRunRequestToLastRow']} rows/s, job e2e {r['jobEndToEndSeconds']}")
            runs.append(r)
            time.sleep(20)
    time.sleep(70)
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    o = outputs()
    ev = {
        "label": a.label, "config": cfg, "region": o["ApiUrl"].split(".")[2], "startedAt": start.isoformat(),
        "note": "Run 1 may include cold starts; runs are sequential in one window.",
        "runs": runs,
        "summary": {
            "rowsPerSecond": [r["rowsPerSecond_firstRunRequestToLastRow"] for r in runs],
            "rowsPerSecondMedian": statistics.median(r["rowsPerSecond_firstRunRequestToLastRow"] for r in runs),
            "jobE2ESecondsMedian": statistics.median(s for r in runs for s in r["jobEndToEndSeconds"]),
            "rowLatencyP95MsMedian": statistics.median(r["rowLatencyMs"]["p95"] for r in runs),
        },
        "metrics": {
            "DuplicatesSkipped": metric_sum("DuplicatesSkipped", "WorkerFn", start, end),
            "TxConflictRetries": metric_sum("TxConflictRetries", "WorkerFn", start, end),
            "RowSystemFailures": metric_sum("RowSystemFailures", "WorkerFn", start, end),
            "workerInvocations": sum(d["Sum"] for d in aws_metric_max("AWS/Lambda", "Invocations",
                                                                      {"FunctionName": o["WorkerFunctionName"]}, start, end, "Sum")),
            "workerConcurrentMax": aws_metric_max("AWS/Lambda", "ConcurrentExecutions",
                                                  {"FunctionName": o["WorkerFunctionName"]}, start, end),
            "workQueueOldestAge": aws_metric_max("AWS/SQS", "ApproximateAgeOfOldestMessage",
                                                 {"QueueName": o["WorkQueueUrl"].rsplit("/", 1)[1]}, start, end),
        },
    }
    path = save_evidence(f"bench-{a.label}-{start.strftime('%Y%m%dT%H%M%S')}.json", ev)
    print(ev["summary"], "\n->", path)


if __name__ == "__main__":
    main()
