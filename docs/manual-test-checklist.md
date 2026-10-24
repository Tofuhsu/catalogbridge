# Manual test checklist (browser)

Site: https://d394jsxt8i7q8r.cloudfront.net · Fixtures: `~/projects/catalogbridge/fixtures/`
Tick each item and note anything unexpected (exact error text, step). Use only synthetic fixtures.
Automated equivalents already pass (`scripts/e2e.py`); this list is the human check.

## A. Sign-in

- [ ] A1. Signed out, open `/jobs` directly → redirected to the Sign in page.
- [ ] A2. Sign in → job list shows your email top right.
- [ ] A3. Sign out → back to the site signed out; `/jobs` redirects to Sign in again.

## B. Upload and structural checks (New job → choose file → Upload)

| # | File | Expected |
| --- | --- | --- |
| B1 | `en_standard.csv` | Status goes to AWAITING_MAPPING within seconds; preview shows 20 rows of 20 |
| B2 | `zh_headers.csv` | Chinese headers and titles display correctly (file has a BOM) |
| B3 | `quoted_newlines.csv` | Row 1 title shows "Mug, large (blue)" in one cell (comma + line break inside quotes) |
| B4 | `empty.csv` | Red message "File is empty." and no new job in the list |
| B5 | a file over 1 MB (make one: `python3 -c "print('a,b'); [print('x'*100+',1') for _ in range(11000)]" > ~/Downloads/too_big.csv`) | "File is larger than 1 MB." and no new job |
| B6 | `rows_501.csv` | REJECTED: "File has 501 data rows; the limit is 500." |
| B7 | `duplicate_header.csv` | REJECTED: "Duplicate header name(s): sku." |
| B8 | `not_utf8.csv` | REJECTED: "File is not valid UTF-8 ..." |

## C. Mapping (on an AWAITING_MAPPING job)

- [ ] C1. `synonyms.csv`: map Item Code→sku, Product Name→title, Unit Price→price, Qty Available→stock
      by hand → "Confirm mapping and run" becomes enabled.
- [ ] C2. Set two fields to the same column → "used twice" appears and the run button is disabled.
- [ ] C3. Leave one field "(not mapped)" → run button disabled.
- [ ] C4. "Save as template" (job needs a supplier name, e.g. `acme`) → "Saved as template ...".
- [ ] C5. New `synonyms.csv` job → "Apply a saved template" → all 4 fields filled.
- [ ] C6. New `en_standard.csv` job → apply the same template → red message listing the columns the
      template expects that this file does not have.
- [ ] C7. "Suggest with AI". **Right now expected:** "AI suggestion is unavailable right now. You can map
      the columns manually." (Bedrock use-case form not submitted). After the form is submitted:
      fields get filled, notes shown, nothing runs until you confirm.
- [ ] C8. Click "Suggest with AI" 4 times on one job → the 4th says the AI limit is reached.

## D. Run, results, downloads

- [ ] D1. `rows_500.csv` → run → progress bar moves → COMPLETED, 500 / 500, seconds shown.
- [ ] D2. `mixed_errors.csv` → COMPLETED_WITH_ERRORS, 7 valid / 5 invalid. The fix table lists:
      row 2 price negative · row 4 SKU required · row 6 stock not a whole number ·
      row 8 duplicate SKU (of row 7) · row 10 price format (`$12`).
- [ ] D3. Downloads on that job: output CSV has 7 rows (`sku,title,price,currency,stock,source_row`);
      error report has 5 rows; original is the file you uploaded.
- [ ] D4. `prompt_injection.csv` → run → open the output in Excel/Numbers: titles `=1+1` and
      `@SUM(A1)` show as text starting with `'`, not as formula results.
- [ ] D5. `en_standard.csv` with currency **JPY** → rows whose price has cents are INVALID
      ("JPY prices allow at most 0 decimal place(s)"); no conversion happens.
- [ ] D6. Refresh the page while a job is processing → progress continues; leaving the page causes no
      errors.

## E. Corrected job

- [ ] E1. On the `mixed_errors.csv` job, fix: row 2 price `5.00`, row 4 SKU `SKU-NEW-4`, row 6 stock `2`,
      row 8 SKU `SKU-NEW-8`, row 10 price `12.00` → "Create corrected job".
- [ ] E2. New job shows "corrected from ..." link and "Mapping copied from the original job" → run →
      COMPLETED, 12 / 0.
- [ ] E3. The original job still shows 7 valid / 5 invalid (unchanged).
- [ ] E4. Another corrected job, tick "Drop" on 2 rows → new job has 10 rows.

## F. List and robustness

- [ ] F1. Job list: newest first, "corrected" badge on corrected jobs, statuses match.
- [ ] F2. Double-click "Upload" quickly → only one new job appears.
- [ ] F3. Phone-width window (or phone): pages usable, tables scroll sideways.

## G. Isolation (needs a second invited account)

- [ ] G1. Copy a job URL from account 1, open it signed in as account 2 → "Job not found".
- [ ] G2. Account 2's job list and templates do not show account 1's items.

## H. Alerts (after confirming the SNS email)

- [ ] H1. Ask for a test alarm email (operator sets an alarm state briefly) → email arrives.
