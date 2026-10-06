---
name: gl-reconciliation
description: Reconcile a general-ledger account against a subledger, bank statement or counterparty extract. Finds breaks, traces each to a root cause, and routes it to the right approver for sign-off. Use when the user asks to reconcile GL/bank/subledger balances, explain a variance, investigate unreconciled items, prepare a month-end account rec, or collect reviewer/controller sign-off on reconciling items.
---

# GL Reconciliation

Run the reconciliation in three stages: **find breaks**, **trace root cause**, **route for sign-off**. The matching, tie-out and routing are deterministic and live in the bundled scripts. Your job is to drive them, investigate what they can't explain, and present the result so an approver can sign.

## 1. Gather inputs

You need:

- **GL extract** (CSV) for the account and period.
- **Comparison extract** (CSV): subledger, bank statement, custodian or counterparty.
- **Period end date** (YYYY-MM-DD). This drives the cut-off and aging logic.
- **Preparer name**. This drives the segregation-of-duties check.

Both files need at least a date column and an amount column, plus a reference column if one exists. If the headers differ from `id,date,amount,reference,description`, copy `${CLAUDE_PLUGIN_ROOT}/config/default.json` and edit its `columns` map. Don't rewrite the user's files. If the file stores debits and credits in separate columns, derive a signed `amount` into a working copy and say that you did.

Confirm the sign convention: both sides must express the same economic direction as a positive number. A bank statement credit is a GL debit to cash, so cash recs usually need no flip. A liability subledger often does.

## 2. Find breaks

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/reconcile.py" \
  --gl <gl.csv> --sub <sub.csv> \
  --config <config.json> \
  --period-end <YYYY-MM-DD> --preparer "<name>" \
  --out-json recon_results.json --out-md recon_report.md
```

Exit code 0 means the variance is fully explained by breaks. Exit code 2 means the tie-out failed, which should never happen with an unmodified engine. Treat it as a defect and stop.

Matching passes, in order: `exact` (reference, amount and date within tolerance), then `amount_date` (amount and date, references absent or different), then `aggregate` (many-to-one by reference where the totals agree).

Break taxonomy:

| Type | Signal |
|---|---|
| `timing` | Same amount, dates outside tolerance but within the timing window. Flagged `cut-off` if it straddles period end. |
| `sign_flip` | Equal and opposite amounts. |
| `transposition` | Same reference, difference divisible by 9, same digits. |
| `rounding` | Same reference, difference within `rounding_tolerance`. |
| `amount_mismatch` | Same reference, any other difference. |
| `duplicate_posting` | A twin with the same reference, amount and date on the same side. |
| `missing_in_gl` | Present only in the subledger or bank. |
| `missing_in_subledger` | Present only in the GL. |

## 3. Trace root cause

The engine attaches a *hypothesis* to each break. For anything routed to Reviewer or above, and for any break you can't confirm from the two extracts alone, delegate to the `break-investigator` agent (one break or a related cluster per call). Pass it the break JSON and the file paths. It returns a confirmed or revised root cause, the supporting evidence, and a proposed correcting entry.

Look across breaks too: several breaks on one counterparty, preparer or day usually share one root cause. Call that out.

Never invent evidence. If the data can't confirm a cause, say "unconfirmed" and list the document that would confirm it.

## 4. Route for sign-off

Routing is already in the results: tier by gross amount, with floors by break type and escalation for aged items. Present a short summary to the user:

1. Tie-out line: GL total, sub total, variance, unexplained (must be 0).
2. Break register grouped by `route_to`, largest first.
3. For each break: root cause, evidence, proposed JE (debit/credit/amount), approver.
4. Items needing a human decision (unconfirmed causes, write-offs, anything above Controller).

Record decisions only when the user gives them explicitly:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/signoff.py" --results recon_results.json \
  --break B-003 --approver "<name>" --role Reviewer \
  [--decision reject --note "<why>"] --out-md recon_report.md
```

The script refuses when the approver's role is below the break's tier, when the approver is the preparer, and when a rejection has no note. Don't work around a refusal. Report it and ask who should sign.

## Guardrails

- Don't post journal entries or change source systems. Propose entries only.
- Don't sign off on the user's behalf or invent approver names.
- Keep amounts exact (the engine uses Decimal). Don't round in the narrative.
- Treat the files as confidential financial data. Don't send them anywhere outside the session.
