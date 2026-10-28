# 5-minute demo script

Prepare: signed-in browser at the site URL, `fixtures/synonyms.csv`, `fixtures/mixed_errors.csv`,
the CloudWatch dashboard `CatalogBridge`, and a terminal in the repo.

1. **Problem (30 s).** Two suppliers, two header styles (`synonyms.csv`, `zh_headers.csv`). Goal:
   one validated `catalogbridge.v2` file plus an error report.
2. **Upload + preview (45 s).** New job → upload `synonyms.csv` → status moves CREATED →
   VALIDATING_UPLOAD → AWAITING_MAPPING. Point out: presigned POST straight to private S3, the
   job is bound to the uploaded object version.
3. **Mapping (45 s).** "Suggest with AI" → columns filled, notes shown. Explain: schema-constrained
   output, server re-validation, nothing runs until confirmed, manual always works. Save as
   template.
4. **Run (45 s).** Confirm and run → progress bar. Explain the path: durable run request → table
   stream → dispatch → one SQS message per row → worker transaction (result + counter) → report.
5. **Errors + correction (60 s).** Run `mixed_errors.csv` → COMPLETED_WITH_ERRORS (7 valid, 5
   invalid). Show the error table, fix two cells, drop one row → "Create corrected job" → new job
   with parent link; the original stays unchanged. Download output and error report.
6. **Failure story (60 s).** Show `docs/failure-experiments.md`: crash after commit (no double
   count), crash halfway through dispatch (re-dispatch + dedup), poison rows → DLQ → alarm →
   fix → redrive with the same run identity. Show the dashboard widgets (queue age, DLQ,
   duplicates skipped).
7. **Limits (15 s).** 500 rows, single counter hot spot, dedup scope = DynamoDB writes, no real
   users yet.
