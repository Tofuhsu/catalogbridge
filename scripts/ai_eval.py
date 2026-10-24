"""Evaluate Bedrock mapping suggestions on the fixed synthetic test set (fixtures/ai_eval.json).

  python scripts/ai_eval.py        # writes docs/evidence/ai-eval-<timestamp>.json

For each fixture: one suggestion call via the real API. A target counts as correct when the
suggested column equals the answer key (or is one of the accepted answers, where the key lists
several, e.g. genuinely ambiguous columns). Wrong = a different column was suggested. Null where a
column existed = needs manual mapping. We report counts only; no "accuracy improvement" claims.
"""

import json
import os
import time

from cb import ROOT, TestUser, fixture, save_evidence

TARGETS = ["sku", "title", "price", "stock"]


def main():
    key = json.load(open(os.path.join(ROOT, "fixtures", "ai_eval.json"), encoding="utf-8"))
    rows = []
    with TestUser("ai") as U:
        api = U.api
        for name, answer in key.items():
            job_id = api.create_and_upload(fixture(name), name=name)
            job = api.wait(job_id, {"AWAITING_MAPPING", "REJECTED"})
            t0 = time.time()
            st, out = api.call("POST", f"/jobs/{job_id}/mapping-suggestion")
            wall_ms = int((time.time() - t0) * 1000)
            if st != 200:
                rows.append({"fixture": name, "status": st, "error": out.get("error")})
                print(name, "->", st, out.get("error", {}).get("code"))
                continue
            sug = out["suggestion"]
            per = {}
            for t in TARGETS:
                accepted = answer[t] if isinstance(answer[t], list) else [answer[t]]
                got = sug["mapping"].get(t)
                if got in accepted:
                    per[t] = "correct"
                elif got is None:
                    per[t] = "left_unmapped"
                else:
                    per[t] = "wrong"
            rows.append({"fixture": name, "status": st, "suggested": sug["mapping"], "expected": answer, "perTarget": per,
                         "dropped": sug["dropped"], "notes": sug["notes"], "modelLatencyMs": sug["latencyMs"],
                         "apiWallMs": wall_ms, "modelId": sug["modelId"]})
            print(f"{name:24s} {per}  {sug['latencyMs']} ms")
    graded = [r for r in rows if r.get("perTarget")]
    counts = {"correct": 0, "wrong": 0, "left_unmapped": 0}
    for r in graded:
        for v in r["perTarget"].values():
            counts[v] += 1
    # Fields the user would have to change by hand before running.
    needs_edit = counts["wrong"] + counts["left_unmapped"]
    lat = sorted(r["modelLatencyMs"] for r in graded)
    summary = {"fixtures": len(key), "graded": len(graded), "targetsGraded": 4 * len(graded), **counts,
               "targetsNeedingManualChange": needs_edit, "calls": len(rows),
               "modelLatencyMs": {"min": lat[0], "median": lat[len(lat) // 2], "max": lat[-1]} if lat else None}
    path = save_evidence(f"ai-eval-{time.strftime('%Y%m%dT%H%M%S')}.json", {"summary": summary, "rows": rows})
    print(json.dumps(summary, indent=2), "\n->", path)


if __name__ == "__main__":
    main()
