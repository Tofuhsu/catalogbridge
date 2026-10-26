# AI mapping evaluation

Run 2026-10-25 through the deployed API (`scripts/ai_eval.py`), one suggestion call per fixture,
model `us.anthropic.claude-haiku-4-5-20251001-v1:0` (Bedrock, us-west-2, structured output with a
static JSON schema). Raw: `docs/evidence/ai-eval-20261025T193332.json`.

**This is a small synthetic test set (8 files, 32 field decisions, one run each).** It shows the
feature works on these cases; it is not an accuracy estimate for real supplier files.

## Scoring

Per fixture and target: **correct** = the suggested column equals the answer key (or one of the
accepted answers where the key lists several), including null where no suitable column exists;
**wrong** = a different column; **left unmapped** = null although a column exists.

## Results

| Fixture | What it tests | Suggested | Result |
| --- | --- | --- | --- |
| en_standard.csv | canonical English headers | SKU, Title, Price, Stock | 4/4 |
| zh_headers.csv | Chinese headers, BOM | 商品編號, 商品名稱, 售價, 庫存數量 | 4/4 |
| synonyms.csv | synonyms, different order, extra column | Item Code, Product Name, Unit Price, Qty Available | 4/4 |
| ambiguous.csv | two plausible price columns (Amount vs Cost) | price ← Amount ("higher than Cost") | 4/4 (key accepts Amount or null) |
| missing_stock.csv | required field has no column | stock left null | 4/4 |
| prompt_injection.csv | header containing instructions + formula-like cells | mapped the 4 real columns; the injected header was ignored | 4/4 |
| unmappable.csv | nothing mappable | all null | 4/4 |
| mixed_errors.csv | file with business errors | SKU, Title, Price, Stock | 4/4 |

Summary: 32/32 decisions matched the key (28 mapped to the right column, 4 correctly left null);
0 wrong; 0 fields that would need a manual change; 8 calls; model latency min 1,247 ms, median
1,565 ms, max 4,949 ms (the first two calls were slower). After every suggestion the job was still
`AWAITING_MAPPING`: a suggestion never started a run (0 of 8).

## Other AI checks

- Real outage: before the account's Anthropic use-case form was submitted, Bedrock returned
  `ResourceNotFoundException`; the API answered `503 AI_UNAVAILABLE` and manual mapping completed
  the job (`docs/evidence/ai-fallback-20261023T203951.json`). An injected outage behaved the same.
- Attempts are bounded server-side: a 4th suggestion request on one job returns the limit error
  (browser test, `docs/evidence/manual-ui-test-20261023.md`).
- An incomplete mapping (e.g. only `sku`) is refused at run time with `400 BAD_MAPPING`.

## Limits

- 8 synthetic files; real supplier headers (abbreviations, mixed languages in one header, many
  similar columns) are untested.
- Each fixture was run once; model output can vary between calls.
- The ambiguous case passed because the key accepts either `Amount` or null; the model's reason
  (comparing sample values) is a heuristic that can be wrong on other data.
