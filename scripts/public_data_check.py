"""Run real public e-commerce sample files through the DEPLOYED stack (no recruited users needed).

  python scripts/public_data_check.py <dir-with-downloaded-files>

Files are NOT stored in this repository (third-party licenses); download them yourself from the
URLs below, then point this script at the folder. It records each file's SHA-256 so the run is
reproducible, and writes docs/evidence/public-data-<timestamp>.json.

This validates behaviour on real-world formats. It is not user validation.
"""

import collections
import hashlib
import os
import sys
import time

from cb import TestUser, save_evidence

SOURCES = {
    "woocommerce_sample_products.csv": {
        "url": "https://raw.githubusercontent.com/woocommerce/woocommerce/trunk/plugins/woocommerce/sample-data/sample_products.csv",
        "license": "GPL-2.0 (woocommerce/woocommerce)",
        # Answer key written by hand from the column meanings. Sale price is a discount, not the price.
        "key": {"sku": "SKU", "title": "Name", "price": "Regular price", "stock": "Stock"},
    },
    "magento_products_gear_bags.csv": {
        "url": "https://raw.githubusercontent.com/magento/magento2-sample-data/2.4-develop/app/code/Magento/CatalogSampleData/fixtures/SimpleProduct/products_gear_bags.csv",
        "license": "OSL-3.0 (magento/magento2-sample-data)",
        "key": {"sku": "sku", "title": "name", "price": "price", "stock": "qty"},
    },
}
NOT_OBTAINED = {
    "Shopify product_template.csv": "https://help.shopify.com/csv/product_template.csv returned HTTP 403 (bot protection); not bypassed",
    "Open Food Facts search export": "world.openfoodfacts.org search download returned HTTP 503 twice",
}


def main():
    folder = sys.argv[1]
    rows = []
    with TestUser("public") as U:
        api = U.api
        for name, src in SOURCES.items():
            data = open(os.path.join(folder, name), "rb").read()
            rec = {"file": name, "url": src["url"], "license": src["license"], "bytes": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}
            job_id = api.create_and_upload(data, name=name)
            job = api.wait(job_id, {"AWAITING_MAPPING", "REJECTED"})
            rec.update(status=job["status"], rows=job.get("rowCount"), columns=len(job.get("headers") or []),
                       rejectReasons=job.get("rejectReasons"))
            if job["status"] == "REJECTED":
                rows.append(rec)
                continue
            st, out = api.call("POST", f"/jobs/{job_id}/mapping-suggestion")
            if st == 200:
                sug = out["suggestion"]["mapping"]
                rec["ai"] = {"suggested": sug, "notes": out["suggestion"]["notes"],
                             "latencyMs": out["suggestion"]["latencyMs"],
                             "perTarget": {t: ("correct" if sug.get(t) == v else "left_unmapped" if sug.get(t) is None else "wrong")
                                           for t, v in src["key"].items()}}
            else:
                rec["ai"] = {"status": st, "error": out.get("error")}
            api.map_and_run(job_id, src["key"])
            job = api.wait(job_id, {"COMPLETED", "COMPLETED_WITH_ERRORS"})
            codes = collections.Counter()
            cursor = None
            while True:
                q = f"/jobs/{job_id}/results?status=INVALID" + (f"&cursor={cursor}" if cursor else "")
                r = api.call("GET", q)[1]
                for it in r["results"]:
                    for e in it["validationErrors"]:
                        codes[f"{e['field']}:{e['code']}"] += 1
                cursor = r.get("nextCursor")
                if not cursor:
                    break
            rec["run"] = {"status": job["status"], "valid": job["validCount"], "invalid": job["invalidCount"],
                          "errorCodes": dict(codes), "endToEndSeconds": (job["completedAt"] - job["runRequestedAt"]) / 1000}
            rows.append(rec)
            print(name, rec.get("ai", {}).get("perTarget"), rec["run"])
    path = save_evidence(f"public-data-{time.strftime('%Y%m%dT%H%M%S')}.json", {"files": rows, "notObtained": NOT_OBTAINED})
    print("->", path)


if __name__ == "__main__":
    main()
