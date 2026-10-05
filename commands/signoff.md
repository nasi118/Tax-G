---
description: Record an approver's sign-off or rejection on reconciliation breaks
argument-hint: <results.json> <break-ids|all-for:Role> <approver> <role> [approve|reject] [note]
---

Record a sign-off on GL reconciliation breaks: $ARGUMENTS

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/signoff.py"` with `--results`, one `--break` per id (or `--all-for <Role>`), `--approver`, `--role` and `--decision`. Add `--note` for rejections. Regenerate the markdown report with `--out-md`.

Only record a decision the user stated explicitly. If the script refuses (insufficient authority, segregation of duties, or a missing note), report the reason and ask who should sign. Don't retry with a different role or name.
Finish by listing the breaks still outstanding and which role each one needs.
