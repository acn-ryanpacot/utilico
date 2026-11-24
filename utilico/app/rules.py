"""Versioned rule engine (prompt §8). Deterministic code parameterised by config/rules/v1.0.json.

A case context ("ctx") describes what the rules look at:
  wo_id, subtype, recorded_premise, premise (resolved; the suggested one once a PB3 match is confirmed),
  match_status (NONE | PENDING | CONFIRMED | REJECTED), suggested_premise, account_id, sa_id, meter_id,
  sp_sa_ids, window {disconnect_date, start, end}, bill_ids (affected draft or issued bills), case_type.
"""
import re

from .pit import STOP_SUBTYPES, resolve_premise, sa_covering
from .util import D, RULE_SET_VERSION, day_range, fold_lookalikes, levenshtein

FAILED_STATUSES = ("ERROR", "SKIPPED", "UNPROCESSED")
CROSSREF = re.compile(r"cross[\s-]?ref", re.I)


def near_matches(view, premise, max_distance=1):
    """Crosswalk premises at the minimum folded Levenshtein distance (<= 1, O/0 and I/1 equal)."""
    folded = fold_lookalikes(premise)
    cands = [(levenshtein(fold_lookalikes(p), folded), levenshtein(p, premise), p) for p in view.xw]
    cands = [c for c in cands if c[0] <= max_distance]
    if not cands:
        return []
    best = min(c[0] for c in cands)
    return [{"premise": c[2], "distance": c[0], "raw_distance": c[1]} for c in sorted(cands) if c[0] == best]


def post_days(bill, win):
    s = max(win["start"], bill["period_start"])
    e = min(win["end"] or bill["period_end"], bill["period_end"])
    return day_range(s, e) if s <= e else []


def run_rules(view, ctx, rules_cfg):
    """Evaluate R1-R4 and PB1-PB3 for one case context. Returns {rule_id: result}."""
    out = {}
    win = ctx["window"]
    dd = win["disconnect_date"]
    stop = ctx["subtype"] in STOP_SUBTYPES
    linked = ctx.get("premise") is not None and ctx.get("match_status") in ("NONE", "CONFIRMED")
    reads = view.reads_by_meter.get(ctx.get("meter_id"), {}) if ctx.get("meter_id") else {}
    bills = [view.bills[b] for b in ctx.get("bill_ids") or [] if b in view.bills]

    # R1 / R2: only once the disconnect is linked to the account (recorded match or confirmed PB3 match)
    r1, r2 = [], []
    if linked:
        for b in bills:
            pd = post_days(b, win)
            kwh_days = [d for d in pd if d in reads and D(reads[d]["kwh"]) > 0]
            if pd and (stop or kwh_days):
                r1.append({"bill_id": b["bill_id"], "post_days": len(pd), "from": pd[0], "to": pd[-1], "state": b["_state"]})
            est = [reads[d]["read_id"] for d in pd if d in reads and reads[d]["read_quality"] == "ESTIMATED" and D(reads[d]["kwh"]) > 0]
            if est:
                r2.append({"bill_id": b["bill_id"], "estimated_days": len(est), "read_ids": est})
    rec_wo = [win["reconnect_wo_id"]] if win.get("reconnect_wo_id") else []      # the reconnect that closes the window
    out["R1"] = {"fired": bool(r1), "details": r1, "records": [ctx["wo_id"]] + rec_wo + [x["bill_id"] for x in r1]}
    out["R2"] = {"fired": bool(r2), "details": r2,
                 "records": [ctx["wo_id"]] + rec_wo + [x["bill_id"] for x in r2] + [x["read_ids"][0] for x in r2]}

    # R3: complaint on or after the disconnect date without a billing hold
    cats = rules_cfg["reconciliation"]["R3"]["categories"]
    r3 = [c for c in view.crm_by_account.get(ctx["account_id"], [])
          if c["category"] in cats and c["opened_at"][:10] >= dd and c.get("billing_hold_set", "N") == "N"]
    out["R3"] = {"fired": bool(r3), "details": [{"case_id": c["case_id"], "category": c["category"], "opened_at": c["opened_at"]} for c in r3],
                 "records": [c["case_id"] for c in r3]}

    # R4: disputed revenue posted (issued bills only; drafts have no journal yet)
    open_disputes = [c for c in view.crm_by_account.get(ctx["account_id"], []) if c["category"] == "BILLING_DISPUTE" and c["status"] != "CLOSED"]
    r4, sev = [], None
    for b in bills:
        if b["_state"] != "ISSUED":
            continue
        if not (b["bill_status"] == "DISPUTED" or open_disputes):
            continue
        gl = view.gl_by_ref.get(b["bill_id"], [])
        if not gl:
            continue
        per = gl[0]["accounting_period"]
        closed = (view.periods.get(per) or {}).get("status") == "CLOSED"
        r4.append({"bill_id": b["bill_id"], "je_id": gl[0]["je_id"], "period": per, "period_status": "CLOSED" if closed else "OPEN",
                   "dispute_case_ids": [c["case_id"] for c in open_disputes]})
        sev = "High" if closed or sev == "High" else "Medium"
    out["R4"] = {"fired": bool(r4), "severity": sev, "details": r4,
                 "records": [x["bill_id"] for x in r4] + [x["je_id"] for x in r4] + sorted({c for x in r4 for c in x["dispute_case_ids"]})}

    # PB1: SA active despite a stop-type disconnect (only when the OMS premise resolves as recorded)
    pb1 = []
    if stop and ctx.get("match_status") == "NONE":
        res = resolve_premise(view, ctx["recorded_premise"], dd)
        sa = sa_covering(res["sp_sas"], dd) if res else None
        sa = sa or (res["sa"] if res else None)
        if sa and sa["sa_status"] == "ACTIVE":
            pb1.append({"sa_id": sa["sa_id"], "sa_status": sa["sa_status"]})
    out["PB1"] = {"fired": bool(pb1), "details": pb1, "records": [ctx["wo_id"]] + [x["sa_id"] for x in pb1]}

    # PB2: the disconnect's integration message failed / not processed at scenario time
    msg = view.latest_msg(ctx["wo_id"])
    st = msg["status"] if msg else None
    pb2 = msg is not None and st in rules_cfg["prebill"]["PB2"]["statuses"]
    out["PB2"] = {"fired": pb2, "details": [{"msg_id": msg["msg_id"], "status": st, "error_message": msg.get("error_message", "")}] if msg else [],
                  "records": [ctx["wo_id"], msg["msg_id"]] if msg else []}

    # PB3: unmatched OMS premise with a near match in the crosswalk
    pb3 = []
    if ctx["recorded_premise"] not in view.xw:
        for c in near_matches(view, ctx["recorded_premise"], rules_cfg["prebill"]["PB3"]["max_distance"]):
            pb3.append({"recorded": ctx["recorded_premise"], "suggested": c["premise"], "distance": c["distance"]})
    out["PB3"] = {"fired": bool(pb3), "details": pb3, "records": [ctx["wo_id"]], "near_matches": [x["suggested"] for x in pb3]}
    for v in out.values():
        v["rule_set_version"] = RULE_SET_VERSION
    return out


def fired(results):
    return [r for r in ("R1", "R2", "R3", "R4", "PB1", "PB2", "PB3") if results.get(r, {}).get("fired")]


def probable_cause(view, ctx, results, rules_cfg, affected_run=None):
    """First match wins (prompt §8). Returns (code, label, quote)."""
    msg = view.latest_msg(ctx["wo_id"])
    st = msg["status"] if msg else None
    err = (msg or {}).get("error_message", "")
    if st == "SKIPPED":
        return "C1", "No integration mapping", err
    if st == "ERROR" and CROSSREF.search(err):
        return "C3", "ID mismatch", err
    todos = view.open_todos(ctx["wo_id"], ctx.get("sa_id"))
    if st == "ERROR" and todos:
        t = todos[0]
        return "C2", "Integration failure; To Do %s open and %s" % (t["todo_id"], "unassigned" if not t.get("assigned_to") else "assigned to " + t["assigned_to"]), err
    if st == "UNPROCESSED" or (msg and affected_run and msg.get("processed_at") and msg["processed_at"] > affected_run):
        return "C6", "Bill ran before stop processed", err
    if ctx["subtype"] == "NONPAY" and results.get("R2", {}).get("fired"):
        return "C4+C5", "Non-pay window estimated", err
    return "OTHER", "Cause not determined", err


def s_codes(cause, results, rules_cfg):
    base = next((c["s_codes"] for c in rules_cfg["causes"] if c["code"] == cause), [])
    out = list(base)
    for r, s in rules_cfg["s_code_additions"].items():
        if results.get(r, {}).get("fired") and s not in out:
            out.append(s)
    return out
