#!/usr/bin/env python3
"""Record a sign-off decision on one or more reconciliation breaks.

Enforces:
  * authority: the approver's role must be at or above the break's route_to
    tier (role order comes from meta.approval_roles in the results file);
  * segregation of duties: the approver cannot be the preparer;
  * a rejection must carry a note.

Every decision is appended to the break's signoffs and to the audit trail.

Usage:
    signoff.py --results results.json --break B-001 [--break B-002 | --all-for Reviewer]
               --approver "J. Doe" --role Reviewer
               [--decision approve|reject] [--note "..."] [--out-md report.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reconcile import render_markdown  # noqa: E402


class SignoffError(Exception):
    pass


def apply_signoff(res: dict, break_ids: list[str], approver: str, role: str,
                  decision: str, note: str = "") -> list[str]:
    roles = res["meta"]["approval_roles"]
    if role not in roles:
        raise SignoffError(f"Unknown role {role!r}; expected one of {roles}")
    preparer = (res["meta"].get("preparer") or "").strip().lower()
    if preparer and approver.strip().lower() == preparer:
        raise SignoffError("Segregation of duties: the preparer cannot sign off")
    if decision == "reject" and not note.strip():
        raise SignoffError("A rejection requires --note explaining why")

    index = {b["id"]: b for b in res["breaks"]}
    unknown = [i for i in break_ids if i not in index]
    if unknown:
        raise SignoffError(f"Unknown break id(s): {unknown}")
    for bid in break_ids:
        b = index[bid]
        if roles.index(role) < roles.index(b["route_to"]):
            raise SignoffError(
                f"{bid} requires {b['route_to']} or above; {role} lacks authority")
        if b["status"] == "approved":
            raise SignoffError(f"{bid} is already approved")

    now = datetime.now().isoformat(timespec="seconds")
    for bid in break_ids:
        b = index[bid]
        entry = {"approver": approver, "role": role, "decision": decision,
                 "note": note, "at": now}
        b["signoffs"].append(entry)
        b["status"] = "approved" if decision == "approve" else "rejected"
        res["audit_trail"].append({"at": now, "event": f"break_{decision}",
                                   "break": bid, "by": approver, "role": role,
                                   "note": note})
    statuses = {b["status"] for b in res["breaks"]}
    res["summary"]["signoff_complete"] = statuses <= {"approved"}
    return break_ids


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True)
    ap.add_argument("--break", dest="breaks", action="append", default=[])
    ap.add_argument("--all-for", help="Sign every pending break routed to this role")
    ap.add_argument("--approver", required=True)
    ap.add_argument("--role", required=True)
    ap.add_argument("--decision", choices=["approve", "reject"], default="approve")
    ap.add_argument("--note", default="")
    ap.add_argument("--out-md")
    args = ap.parse_args(argv)

    path = Path(args.results)
    res = json.loads(path.read_text(encoding="utf-8"))
    ids = list(args.breaks)
    if args.all_for:
        ids += [b["id"] for b in res["breaks"]
                if b["route_to"] == args.all_for and b["status"] == "pending_signoff"
                and b["id"] not in ids]
    if not ids:
        print("No breaks selected.", file=sys.stderr)
        return 1
    try:
        done = apply_signoff(res, ids, args.approver, args.role,
                             args.decision, args.note)
    except SignoffError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    path.write_text(json.dumps(res, indent=2), encoding="utf-8")
    if args.out_md:
        Path(args.out_md).write_text(render_markdown(res), encoding="utf-8")
    pending = [b["id"] for b in res["breaks"] if b["status"] != "approved"]
    print(f"{'approved' if args.decision == 'approve' else 'rejected'} {', '.join(done)} as {args.approver} ({args.role}).")
    print(f"Outstanding: {', '.join(pending) if pending else 'none, sign-off complete'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
