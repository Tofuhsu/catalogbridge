# AI mapping evaluation

**Status: not run yet.** The fixed evaluation (`scripts/ai_eval.py` over the 8 fixtures in
`fixtures/ai_eval.json`) needs Bedrock access to an Anthropic Claude model. As of 2026-10-23 the
account returns `ResourceNotFoundException: Model use case details have not been submitted for
this account` until the account owner submits Anthropic's one-time use-case form in the Bedrock
console.

## What has been observed so far (not an evaluation)

- One live suggestion through the deployed API (before the restriction applied), on
  `synonyms.csv`: all 4 targets mapped correctly (`Item Code→sku`, `Product Name→title`,
  `Unit Price→price`, `Qty Available→stock`), model latency 4,686 ms, API wall time 5,005 ms,
  model `us.anthropic.claude-haiku-4-5-20251001-v1:0`. One call is an example, not an accuracy
  number.
- A real outage (the use-case restriction) and an injected outage both returned
  `503 AI_UNAVAILABLE`, and manual mapping completed the job
  (`docs/evidence/ai-fallback-20261023T203951.json`).
- An incomplete mapping (only `sku`) is refused at run time with `400 BAD_MAPPING`.

## How the evaluation will be scored

Per fixture and target: **correct** (suggested column equals the answer key, or one of the accepted
answers for genuinely ambiguous columns, or null where no column exists), **wrong** (a different
column), **left unmapped** (null although a column exists). Reported: counts, fields needing manual
change, model latency min/median/max, number of calls, token counts. No accuracy-improvement claims.

Test set: English canonical, Chinese headers, synonyms in a different order, ambiguous price
columns, missing stock column, prompt-injection header + formula-like cells, unmappable, and a
file with business errors.
