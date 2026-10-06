# gl-reconciler

A Claude Code plugin that **finds breaks, traces their root cause, and routes them for sign-off** when you reconcile a general-ledger account against a subledger, bank statement or counterparty extract.

## What's in the plugin

| Component | Path | Purpose |
|---|---|---|
| Manifest | `.claude-plugin/plugin.json` | Plugin metadata |
| Skill | `skills/gl-reconciliation/SKILL.md` | End-to-end method: inputs, matching, root cause, routing, guardrails |
| Command | `/gl-reconciler:reconcile <gl.csv> <sub.csv> [period-end] [preparer]` | Runs a full reconciliation |
| Command | `/gl-reconciler:signoff ...` | Records an approval or rejection on breaks |
| Agent | `agents/break-investigator.md` | Confirms or revises the root cause and proposes a correcting JE |
| Engine | `scripts/reconcile.py` | Deterministic matching, break classification, tie-out and routing (stdlib only, Decimal math) |
| Sign-off | `scripts/signoff.py` | Enforces approval authority and segregation of duties, and writes the audit trail |
| Config | `config/default.json` | Column map, tolerances, approval tiers |

## How it works

1. **Match** in three passes: `exact` (reference + amount + date within tolerance), `amount_date`, and `aggregate` (many-to-one by reference).
2. **Classify** every unmatched item as `timing` (with cut-off detection), `sign_flip`, `transposition`, `rounding`, `amount_mismatch`, `duplicate_posting`, `missing_in_gl` or `missing_in_subledger`. Each break gets a root-cause hypothesis, the supporting evidence and a recommended action.
3. **Tie out**: the sum of the break impacts must equal the GL − sub variance, so the unexplained amount is always 0.
4. **Route** each break to a tier based on its gross amount (Preparer ≤ 100, Reviewer ≤ 10,000, Controller above). Type floors apply (for example, duplicates and sign flips need at least a Reviewer), and items older than 30 days escalate one tier.
5. **Sign off**: `signoff.py` refuses an approver below the required tier, the preparer approving their own work, and a rejection without a note.

## Quick start

```bash
python3 scripts/reconcile.py --gl examples/gl_cash.csv --sub examples/bank_statement.csv \
  --config config/default.json --period-end 2026-09-30 --preparer "A. Preparer" \
  --out-json recon_results.json --out-md recon_report.md

python3 scripts/signoff.py --results recon_results.json --all-for Preparer \
  --approver "R. Reviewer" --role Reviewer --out-md recon_report.md
```

The example data produces one break of every type and ties out to zero unexplained variance.

To install the plugin in Claude Code, add this repository as a plugin marketplace source, or run `claude --plugin-dir .` from the repo root.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
