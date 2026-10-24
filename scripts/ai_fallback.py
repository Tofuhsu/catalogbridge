"""Live check: AI unavailable -> clear error, manual mapping still completes; a suggestion never runs by itself.

  python scripts/ai_fallback.py     # needs -c faultInjection=true; writes docs/evidence/ai-fallback-*.json
"""

import time

from cb import TestUser, fixture, save_evidence
from experiments import set_faults

MANUAL = {"sku": "Item Code", "title": "Product Name", "price": "Unit Price", "stock": "Qty Available"}


def main():
    ev = {}
    with TestUser("aifb") as U:
        a = U.api
        # 1. AI unavailable (injected), manual mapping completes the job
        set_faults({"api": {"ai_unavailable": True}})
        try:
            j = a.create_and_upload(fixture("synonyms.csv"), name="synonyms.csv")
            a.wait(j, {"AWAITING_MAPPING"})
            st, out = a.call("POST", f"/jobs/{j}/mapping-suggestion")
            ev["unavailable"] = {"status": st, "error": out.get("error")}
        finally:
            set_faults({})
        a.map_and_run(j, MANUAL)
        job = a.wait(j, {"COMPLETED", "COMPLETED_WITH_ERRORS"})
        ev["manualAfterUnavailable"] = {"status": job["status"], "valid": job["validCount"], "invalid": job["invalidCount"],
                                        "mappingSource": job["mappingSource"]}
        # 2. a (real) suggestion is stored but does not start anything
        j2 = a.create_and_upload(fixture("ambiguous.csv"), name="ambiguous.csv")
        a.wait(j2, {"AWAITING_MAPPING"})
        st, out = a.call("POST", f"/jobs/{j2}/mapping-suggestion")
        time.sleep(5)
        after = a.call("GET", f"/jobs/{j2}")[1]["job"]
        ev["suggestionDoesNotRun"] = {"suggestStatus": st, "suggestion": out.get("suggestion", {}).get("mapping"),
                                      "statusAfter": after["status"], "runRequestId": after.get("runRequestId")}
        # 3. incomplete mapping is refused at run time
        v = after["mappingVersion"]
        a.call("PUT", f"/jobs/{j2}/mapping", {"mapping": {"sku": "Code"}, "mappingVersion": v, "source": "ai"})
        st, out = a.call("POST", f"/jobs/{j2}/run", {"runRequestId": "run-incomplete-0001", "mappingVersion": v + 1})
        ev["incompleteRunRefused"] = {"status": st, "error": out.get("error")}
    ok = (ev["unavailable"]["status"] == 503 and ev["manualAfterUnavailable"]["status"] == "COMPLETED"
          and ev["suggestionDoesNotRun"]["suggestStatus"] == 200
          and ev["suggestionDoesNotRun"]["statusAfter"] == "AWAITING_MAPPING" and ev["incompleteRunRefused"]["status"] == 400)
    ev["pass"] = ok
    path = save_evidence(f"ai-fallback-{time.strftime('%Y%m%dT%H%M%S')}.json", ev)
    print("PASS" if ok else "FAIL", ev, "\n->", path)


if __name__ == "__main__":
    main()
