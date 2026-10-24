# Browser UI test, 2026-10-23 (~23:00 America/Los_Angeles)

Who: the author signed in themselves (Cognito Hosted UI, built-in browser of the Claude desktop
app); the UI was then driven by the AI assistant in that signed-in session. The assistant never
entered credentials. Data: synthetic fixture `manual_test_all_in_one.csv` and the author's own jobs.

| Item | How | Result |
| --- | --- | --- |
| A1 signed-out deep link → sign-in page | browser | PASS |
| A2 sign in | author, browser | PASS (after invite-email fix) |
| A3 sign out | not done by the assistant (it does not sign users out) | NOT RUN |
| B1–B3 upload, BOM, Chinese headers, quoted newline | author's upload in browser; preview read | PASS |
| B4 empty file, B5 >1 MB (client message) | browser file chooser not available to the assistant; server-side refusal verified by e2e; client check confirmed present in the deployed bundle | PARTIAL |
| B6–B8 501 rows / duplicate header / not UTF-8 | live API e2e (`e2e-20261023T231307.json`) | PASS |
| C1 manual mapping | browser | PASS |
| C2 same column twice → "used twice", run disabled | browser | PASS |
| C3 unmapped field → run disabled | browser | PASS |
| New: sample values + repeated-SKU warning (added after the author's one-column-off mapping) | browser | PASS (warned "9 repeat(s)" for Brand, "1 repeat(s)" for the real duplicate) |
| C4 save template | browser | PASS ("Saved as template "acme"") |
| C5 apply template | browser | PASS (4 fields restored) |
| C6 template with missing columns | browser | PASS (lists 4 missing columns, fills nothing) |
| C7 AI unavailable message | browser | PASS (Bedrock use-case form not submitted: real outage) |
| C8 AI limit | browser | PASS (4th click: limit reached) |
| D1/D6 run + live progress | browser | PASS (RUN_REQUESTED → COMPLETED_WITH_ERRORS 6/5 in 5.1 s) |
| D2 error table | browser | PASS |
| D3/D4 output + error report content, formula prefix, decimals | read from S3 by operator (no browser download) | PASS |
| D5 JPY precision | live API | PASS (0/20, no conversion) |
| E1–E3 corrected job from in-table edits | browser | PASS (11/0 in 1.2 s; parent unchanged 6/5) |
| E4 drop rows | browser | PASS (9 rows; ran 6/3) |
| F1 job list | browser | PASS |
| F2 double-click create | live API (same clientToken) | PASS |
| F3 phone width 375 px | browser | PASS (no page side-scroll; tables scroll in their box) |
| G isolation | live API with two users (8 routes → 404) | PASS |
| H alert email | SNS subscription still PendingConfirmation | NOT RUN |
