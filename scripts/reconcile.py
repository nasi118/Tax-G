#!/usr/bin/env python3
"""GL reconciliation engine.

Matches general-ledger entries against a subledger / bank / counterparty
extract, classifies every unmatched item as a typed break with a root-cause
hypothesis, and routes each break to an approver tier for sign-off.

Stdlib only. Amounts are handled as Decimal end to end.

Usage:
    reconcile.py --gl gl.csv --sub bank.csv [--config cfg.json]
                 [--period-end 2026-09-30] [--preparer "A. Smith"]
                 [--out-json results.json] [--out-md report.md]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_CONFIG = {
    "columns": {
        "id": "id",
        "date": "date",
        "amount": "amount",
        "reference": "reference",
        "description": "description",
    },
    "matching": {
        "date_tolerance_days": 3,
        "timing_window_days": 10,
        "rounding_tolerance": "1.00",
    },
    "routing": {
        "tiers": [
            {"role": "Preparer", "max_abs_impact": "100", "sla_days": 2},
            {"role": "Reviewer", "max_abs_impact": "10000", "sla_days": 3},
            {"role": "Controller", "max_abs_impact": None, "sla_days": 5},
        ],
        "min_role_by_type": {
            "duplicate_posting": "Reviewer",
            "sign_flip": "Reviewer",
            "missing_in_subledger": "Reviewer",
        },
        "aging_escalation_days": 30,
    },
}

ROOT_CAUSE = {
    "timing": (
        "Same item recorded on both sides in different periods/dates "
        "(cut-off or in-transit item).",
        "No correcting entry. Confirm the item clears in the next period; "
        "escalate if it does not.",
    ),
    "sign_flip": (
        "Entry posted with the wrong debit/credit direction.",
        "Reverse the GL entry and repost with the correct sign "
        "(net correction = 2x amount).",
    ),
    "transposition": (
        "Digits transposed when keying the amount (difference divisible by 9, "
        "same digits).",
        "Post a correcting entry for the difference.",
    ),
    "rounding": (
        "Small difference consistent with FX or rounding.",
        "Post to rounding/FX difference account if within policy.",
    ),
    "amount_mismatch": (
        "Same reference but amounts disagree (partial payment, fee, "
        "short-pay, or keying error).",
        "Obtain source document; post correcting entry or record "
        "fee/short-pay.",
    ),
    "duplicate_posting": (
        "Same reference, amount and date posted more than once.",
        "Reverse the duplicate entry.",
    ),
    "missing_in_gl": (
        "Activity in the subledger/bank with no GL entry (unrecorded "
        "transaction, e.g. bank fee, interest, direct debit).",
        "Record the missing journal entry after verifying the source.",
    ),
    "missing_in_subledger": (
        "GL entry with no supporting subledger/bank activity (unsupported "
        "manual JE, or item not yet cleared).",
        "Obtain support for the GL entry; reverse if unsupported.",
    ),
}


@dataclass
class Entry:
    side: str
    row: int
    id: str
    date: date
    amount: Decimal
    reference: str
    description: str
    matched: bool = False
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "row": self.row,
            "id": self.id,
            "date": self.date.isoformat(),
            "amount": str(self.amount),
            "reference": self.reference,
            "description": self.description,
        }


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None) -> dict:
    if not path:
        return DEFAULT_CONFIG
    with open(path, encoding="utf-8") as fh:
        return deep_merge(DEFAULT_CONFIG, json.load(fh))


def parse_date(value: str) -> date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {value!r}")


def parse_amount(value: str) -> Decimal:
    v = value.strip().replace(",", "").replace("$", "")
    negative = v.startswith("(") and v.endswith(")")
    if negative:
        v = v[1:-1]
    try:
        amt = Decimal(v)
    except InvalidOperation as exc:
        raise ValueError(f"Unrecognised amount: {value!r}") from exc
    return -amt if negative else amt


def load_entries(path: str, side: str, columns: dict) -> list[Entry]:
    entries = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for k, c in columns.items()
                   if k in ("date", "amount") and c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{path}: missing required column(s) {missing}; "
                             f"found {reader.fieldnames}")
        for i, row in enumerate(reader, start=2):
            try:
                entries.append(Entry(
                    side=side,
                    row=i,
                    id=(row.get(columns["id"]) or f"{side}-{i}").strip(),
                    date=parse_date(row[columns["date"]]),
                    amount=parse_amount(row[columns["amount"]]),
                    reference=(row.get(columns["reference"]) or "").strip().upper(),
                    description=(row.get(columns["description"]) or "").strip(),
                    raw=row,
                ))
            except ValueError as exc:
                raise SystemExit(f"{path} line {i}: {exc}") from exc
    return entries


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def days_apart(a: Entry, b: Entry) -> int:
    return abs((a.date - b.date).days)


def match(gl: list[Entry], sub: list[Entry], cfg: dict) -> list[dict]:
    tol = cfg["matching"]["date_tolerance_days"]
    matches = []

    def pair(g: Entry, s: Entry, how: str):
        g.matched = s.matched = True
        matches.append({"method": how, "gl": [g.id], "sub": [s.id],
                        "amount": str(g.amount)})

    # Pass 1: reference + amount + date within tolerance.
    for g in gl:
        if not g.reference:
            continue
        cands = [s for s in sub if not s.matched and s.reference == g.reference
                 and s.amount == g.amount and days_apart(g, s) <= tol]
        if cands:
            pair(g, min(cands, key=lambda s: days_apart(g, s)), "exact")

    # Pass 2: amount + date within tolerance, references absent or differing.
    for g in gl:
        if g.matched:
            continue
        cands = [s for s in sub if not s.matched and s.amount == g.amount
                 and days_apart(g, s) <= tol
                 and not (g.reference and s.reference and g.reference == s.reference)]
        if cands:
            pair(g, min(cands, key=lambda s: days_apart(g, s)), "amount_date")

    # Pass 3: many-to-one / many-to-many by reference where totals agree.
    refs = {e.reference for e in gl + sub if e.reference and not e.matched}
    for ref in sorted(refs):
        gs = [g for g in gl if not g.matched and g.reference == ref]
        ss = [s for s in sub if not s.matched and s.reference == ref]
        if gs and ss and (len(gs) > 1 or len(ss) > 1) \
                and sum(g.amount for g in gs) == sum(s.amount for s in ss):
            for e in gs + ss:
                e.matched = True
            matches.append({"method": "aggregate", "gl": [g.id for g in gs],
                            "sub": [s.id for s in ss],
                            "amount": str(sum(g.amount for g in gs))})
    return matches


# --------------------------------------------------------------------------
# Break classification
# --------------------------------------------------------------------------

def is_transposition(a: Decimal, b: Decimal) -> bool:
    ca, cb = abs(a * 100), abs(b * 100)
    if ca == cb or (ca - cb) % 9 != 0:
        return False
    da, db = str(int(ca)), str(int(cb))
    return len(da) == len(db) and sorted(da) == sorted(db)


def classify(gl: list[Entry], sub: list[Entry], cfg: dict,
             period_end: date | None) -> list[dict]:
    m = cfg["matching"]
    tol, window = m["date_tolerance_days"], m["timing_window_days"]
    rounding = Decimal(str(m["rounding_tolerance"]))
    breaks: list[dict] = []

    def add(kind: str, g: Entry | None, s: Entry | None, extra: str = ""):
        impact = (g.amount if g else Decimal(0)) - (s.amount if s else Decimal(0))
        cause, action = ROOT_CAUSE[kind]
        evidence = []
        if g and s:
            evidence.append(f"GL {g.id} {g.amount} on {g.date} vs "
                            f"sub {s.id} {s.amount} on {s.date}")
        elif g:
            evidence.append(f"GL {g.id} {g.amount} on {g.date} ref "
                            f"{g.reference or '-'}: no counterpart")
        elif s:
            evidence.append(f"Sub {s.id} {s.amount} on {s.date} ref "
                            f"{s.reference or '-'}: no counterpart")
        if extra:
            evidence.append(extra)
        for e in (g, s):
            if e:
                e.matched = True
        breaks.append({
            "type": kind,
            "impact": impact,
            "gl": g.to_dict() if g else None,
            "sub": s.to_dict() if s else None,
            "root_cause": cause,
            "recommended_action": action,
            "evidence": evidence,
            "item_date": min(x.date for x in (g, s) if x),
        })

    open_gl = lambda: [g for g in gl if not g.matched]
    open_sub = lambda: [s for s in sub if not s.matched]

    # Duplicates: an unmatched item whose twin (same ref/amount/date) is on
    # the same side. Checked first so the twin isn't misread as a break.
    for side, items in (("gl", gl), ("sub", sub)):
        seen: dict = {}
        for e in items:
            key = (e.reference, e.amount, e.date)
            if not e.reference:
                continue
            if key in seen and not e.matched:
                twin = seen[key]
                note = f"Twin of {twin.id} (same ref, amount and date)"
                if side == "gl":
                    add("duplicate_posting", e, None, note)
                else:
                    add("duplicate_posting", None, e, note)
            else:
                seen.setdefault(key, e)

    # Paired breaks: an open GL item and an open sub item that clearly
    # describe the same transaction.
    for g in open_gl():
        best = None
        for s in open_sub():
            same_ref = bool(g.reference) and g.reference == s.reference
            near = days_apart(g, s) <= tol
            if (same_ref or s.reference == "" or g.reference == "") \
                    and g.amount == s.amount and days_apart(g, s) <= window:
                best = ("timing", s, f"{days_apart(g, s)} days apart")
            elif g.amount == -s.amount and (same_ref or near):
                best = ("sign_flip", s, "Amounts equal and opposite")
            elif same_ref:
                diff = g.amount - s.amount
                if is_transposition(g.amount, s.amount):
                    best = ("transposition", s,
                            f"Difference {diff} is divisible by 9")
                elif abs(diff) <= rounding:
                    best = ("rounding", s, f"Difference {diff}")
                else:
                    best = ("amount_mismatch", s, f"Difference {diff}")
            if best:
                break
        if best:
            kind, s, note = best
            if kind == "timing" and period_end and \
                    (min(g.date, s.date) <= period_end < max(g.date, s.date)):
                note += f"; straddles period end {period_end} (cut-off)"
            add(kind, g, s, note)

    for g in open_gl():
        add("missing_in_subledger", g, None)
    for s in open_sub():
        add("missing_in_gl", None, s)
    return breaks


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

def route(breaks: list[dict], cfg: dict, as_of: date) -> None:
    r = cfg["routing"]
    tiers = r["tiers"]
    roles = [t["role"] for t in tiers]
    for b in breaks:
        magnitude = abs(b["impact"])
        if b["type"] == "timing":
            magnitude = abs(Decimal(b["gl"]["amount"]))
        idx = len(tiers) - 1
        for i, t in enumerate(tiers):
            cap = t.get("max_abs_impact")
            if cap is None or magnitude <= Decimal(str(cap)):
                idx = i
                break
        reasons = [f"|amount| {magnitude} within {tiers[idx]['role']} limit"]
        floor = r.get("min_role_by_type", {}).get(b["type"])
        if floor in roles and roles.index(floor) > idx:
            idx = roles.index(floor)
            reasons.append(f"{b['type']} requires at least {floor}")
        age = max(0, (as_of - b["item_date"]).days)
        if age > r.get("aging_escalation_days", 10**9) and idx < len(tiers) - 1:
            idx += 1
            reasons.append(f"aged {age} days: escalated one tier")
        b["magnitude"] = magnitude
        b["age_days"] = age
        b["route_to"] = tiers[idx]["role"]
        b["sla_days"] = tiers[idx].get("sla_days")
        b["routing_reason"] = "; ".join(reasons)
        b["status"] = "pending_signoff"
        b["signoffs"] = []


# --------------------------------------------------------------------------
# Orchestration and output
# --------------------------------------------------------------------------

def reconcile(gl_path: str, sub_path: str, cfg: dict,
              period_end: date | None = None, preparer: str = "") -> dict:
    cols = cfg["columns"]
    gl = load_entries(gl_path, "gl", cols)
    sub = load_entries(sub_path, "sub", cols)
    matches = match(gl, sub, cfg)
    breaks = classify(gl, sub, cfg, period_end)
    as_of = period_end or max((e.date for e in gl + sub), default=date.today())
    route(breaks, cfg, as_of)
    breaks.sort(key=lambda b: (-b["magnitude"], b["type"]))
    for i, b in enumerate(breaks, start=1):
        b["id"] = f"B-{i:03d}"

    gl_total = sum((e.amount for e in gl), Decimal(0))
    sub_total = sum((e.amount for e in sub), Decimal(0))
    variance = gl_total - sub_total
    explained = sum((b["impact"] for b in breaks), Decimal(0))
    by_type: dict[str, dict] = {}
    for b in breaks:
        t = by_type.setdefault(b["type"], {"count": 0, "impact": Decimal(0)})
        t["count"] += 1
        t["impact"] += b["impact"]

    for b in breaks:
        b["impact"] = str(b["impact"])
        b["magnitude"] = str(b["magnitude"])
        b["item_date"] = b["item_date"].isoformat()
    return {
        "meta": {
            "gl_file": str(gl_path),
            "sub_file": str(sub_path),
            "period_end": period_end.isoformat() if period_end else None,
            "as_of": as_of.isoformat(),
            "preparer": preparer,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "approval_roles": [t["role"] for t in cfg["routing"]["tiers"]],
        },
        "summary": {
            "gl_count": len(gl),
            "sub_count": len(sub),
            "gl_total": str(gl_total),
            "sub_total": str(sub_total),
            "variance": str(variance),
            "explained_by_breaks": str(explained),
            "unexplained": str(variance - explained),
            "tie_out": variance == explained,
            "matched_groups": len(matches),
            "break_count": len(breaks),
            "by_type": {k: {"count": v["count"], "impact": str(v["impact"])}
                        for k, v in sorted(by_type.items())},
            "by_route": {role: sum(1 for b in breaks if b["route_to"] == role)
                         for role in [t["role"] for t in cfg["routing"]["tiers"]]},
        },
        "matches": matches,
        "breaks": breaks,
        "audit_trail": [{
            "at": datetime.now().isoformat(timespec="seconds"),
            "event": "reconciliation_run",
            "by": preparer or "unknown",
        }],
    }


def render_markdown(res: dict) -> str:
    s, meta = res["summary"], res["meta"]
    out = [
        "# GL Reconciliation Report",
        "",
        f"- **GL file:** `{meta['gl_file']}`  ",
        f"- **Subledger file:** `{meta['sub_file']}`  ",
        f"- **Period end:** {meta['period_end'] or 'n/a'} · "
        f"**As of:** {meta['as_of']} · **Preparer:** {meta['preparer'] or 'n/a'}",
        "",
        "## Summary",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| GL total ({s['gl_count']} items) | {s['gl_total']} |",
        f"| Subledger total ({s['sub_count']} items) | {s['sub_total']} |",
        f"| Variance (GL − Sub) | {s['variance']} |",
        f"| Explained by breaks | {s['explained_by_breaks']} |",
        f"| Unexplained | {s['unexplained']} |",
        f"| Matched groups | {s['matched_groups']} |",
        f"| Breaks | {s['break_count']} |",
        "",
        f"**Tie-out:** {'PASS: variance fully explained' if s['tie_out'] else 'FAIL: unexplained variance remains'}",
        "",
    ]
    if s["by_type"]:
        out += ["## Breaks by type", "", "| Type | Count | Net impact |",
                "|---|---:|---:|"]
        out += [f"| {k} | {v['count']} | {v['impact']} |"
                for k, v in s["by_type"].items()]
        out.append("")
    if res["breaks"]:
        out += ["## Break register", "",
                "| ID | Type | Impact | Ref | Age | Route to | Status |",
                "|---|---|---:|---|---:|---|---|"]
        for b in res["breaks"]:
            ref = (b["gl"] or b["sub"])["reference"] or "-"
            out.append(f"| {b['id']} | {b['type']} | {b['impact']} | {ref} | "
                       f"{b['age_days']} | {b['route_to']} | {b['status']} |")
        out += ["", "## Root cause and actions", ""]
        for b in res["breaks"]:
            out += [f"### {b['id']} · {b['type']} · {b['impact']}", "",
                    f"- **Root cause:** {b['root_cause']}",
                    f"- **Evidence:** {' | '.join(b['evidence'])}",
                    f"- **Recommended action:** {b['recommended_action']}",
                    f"- **Routed to:** {b['route_to']} (SLA {b['sla_days']}d): "
                    f"{b['routing_reason']}"]
            for so in b["signoffs"]:
                out.append(f"- **Sign-off:** {so['decision']} by {so['approver']} "
                           f"({so['role']}) at {so['at']}"
                           + (f": {so['note']}" if so.get("note") else ""))
            out.append("")
    out += ["## Sign-off", "", "| Role | Breaks | Name | Date |",
            "|---|---:|---|---|"]
    for role, n in s["by_route"].items():
        out.append(f"| {role} | {n} | | |")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gl", required=True, help="GL extract CSV")
    ap.add_argument("--sub", required=True, help="Subledger / bank / counterparty CSV")
    ap.add_argument("--config", help="JSON config (column map, tolerances, routing)")
    ap.add_argument("--period-end", help="Period end date YYYY-MM-DD")
    ap.add_argument("--preparer", default="", help="Name of the preparer")
    ap.add_argument("--out-json", help="Write full results JSON here")
    ap.add_argument("--out-md", help="Write markdown report here")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    pe = parse_date(args.period_end) if args.period_end else None
    res = reconcile(args.gl, args.sub, cfg, pe, args.preparer)
    md = render_markdown(res)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(res, indent=2), encoding="utf-8")
    if args.out_md:
        Path(args.out_md).write_text(md, encoding="utf-8")
    print(md)
    return 0 if res["summary"]["tie_out"] else 2


if __name__ == "__main__":
    sys.exit(main())
