---
description: Reconcile a GL extract against a subledger/bank file, explain every break and route it for sign-off
argument-hint: <gl.csv> <sub.csv> [period-end YYYY-MM-DD] [preparer]
---

Use the `gl-reconciliation` skill to reconcile these inputs: $ARGUMENTS

If either file, the period end or the preparer is missing, ask for it before running.
Run the engine, then investigate breaks routed to Reviewer or above with the `break-investigator` agent.
Finish with the tie-out line, the break register grouped by approver, and the proposed correcting entries.
Write `recon_results.json` and `recon_report.md` next to the GL file.
