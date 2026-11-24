"""POC 1 back-scan and daily check, ported (prompt §18). Same rules, Decimal money.

Back-scan: every completed disconnect -> issued bills (FINAL / DISPUTED) covering post-disconnect days -> credit =
phantom ESTIMATED kWh x rate + fixed charges for those days (fixed retained on NONPAY when the tariff says so).
Severity HIGH (credit >= $250, >= 2 bills affected or a DISPUTED bill), MEDIUM ($25-$249.99), LOW (< $25),
REVIEW ($0 credit but ACTUAL usage after the disconnect - revenue protection, out of scope for the workflow).
Daily check: completed non-NONPAY disconnects whose SA is still ACTIVE at 23:59 on the as-of date.
"""
import re
from collections import OrderedDict
from decimal import Decimal

from .calculator import fixed_creditable
from .datasource import F_KEY
from .pit import View, disconnect_window, resolve_premise
from .rules import near_matches
from .util import D, day_range, f2, kwh1, money


def analyze(view, wo, res, win, scan_start, scan_end):
    first = max(win["start"], scan_start)
    last = min(win["end"], scan_end) if win["end"] else scan_end
    reads = view.reads_by_meter.get(res["xw"]["mdm_meter_id"], {})
    cands = [(b, sa) for sa in res["sp_sas"] for b in view.bills_by_sa.get(sa["sa_id"], []) if b["bill_status"] in ("FINAL", "DISPUTED")]
    per = OrderedDict()
    unbilled = Decimal(0)
    if first <= last:
        for d in day_range(first, last):
            rd = reads.get(d)
            hits = [x for x in cands if x[0]["period_start"] <= d <= x[0]["period_end"]]
            hit = next((x for x in hits if x[1]["start_date"] <= d and (not x[1].get("stop_date") or x[1]["stop_date"] >= d)), hits[0] if hits else None)
            if hit:
                p = per.setdefault(hit[0]["bill_id"], {"bill": hit[0], "sa": hit[1], "days": 0, "phantom": Decimal(0), "actual": Decimal(0)})
                p["days"] += 1
                if rd and D(rd["kwh"]) > 0:
                    if rd["read_quality"] == "ESTIMATED":
                        p["phantom"] += D(rd["kwh"])
                    else:
                        p["actual"] += D(rd["kwh"])
            elif rd and rd["read_quality"] == "ACTUAL" and D(rd["kwh"]) > 0:
                unbilled += D(rd["kwh"])
    lines = []
    for p in per.values():
        t = view.tariffs.get(p["sa"]["tariff_code"])
        rate = D(t["rate_per_kwh"]) if t else D(p["bill"]["rate_per_kwh"])
        fd = D(t["fixed_daily_charge"]) if t else D(p["bill"]["fixed_daily_charge"])
        ph = kwh1(p["phantom"])
        cons = money(ph * rate)
        famt = money(p["days"] * fd)
        fc = famt if fixed_creditable(wo["wo_subtype"], t) else Decimal("0.00")
        lines.append({"bill": p["bill"], "bill_id": p["bill"]["bill_id"], "days": p["days"], "phantom": ph, "actual": kwh1(p["actual"]),
                      "cons": cons, "fixed_credit": fc, "fixed_retained": famt - fc, "credit": money(cons + fc)})
    credit = money(sum((l["credit"] for l in lines), Decimal(0)))
    affected = [l for l in lines if l["credit"] > 0 or l["actual"] > 0]
    actual_after = sum((l["actual"] for l in lines), Decimal(0)) + unbilled
    if credit > 0:
        if credit >= 250 or len(affected) >= 2 or any(l["bill"]["bill_status"] == "DISPUTED" for l in affected):
            sev = "HIGH"
        elif credit >= 25:
            sev = "MEDIUM"
        else:
            sev = "LOW"
    else:
        sev = "REVIEW" if actual_after > 0 else None
    return {"credit": credit, "severity": sev, "flagged": credit > 0 or actual_after > 0, "lines": lines,
            "unbilled_kwh": kwh1(actual_after), "phantom_kwh": kwh1(sum((l["phantom"] for l in lines), Decimal(0))),
            "bill_ids": [l["bill_id"] for l in affected]}


def detect(view, as_of, scan_start=None, scan_end=None):
    bills = list(view.bills.values())
    scan_start = scan_start or min(b["period_start"] for b in bills)
    scan_end = scan_end or max(b["period_end"] for b in bills)
    as_of_end = as_of + " 23:59"
    back = {"findings": [], "unmatched": [], "scanned": 0, "matched": 0}
    daily = {"holds": [], "unmatched": [], "excluded_nonpay": []}
    for wo in view.completed_disconnects():
        back["scanned"] += 1
        in_daily = wo["completed_at"] <= as_of_end
        dd = wo["completed_at"][:10]
        res = resolve_premise(view, wo["oms_premise_id"], dd)
        if not res:
            cands = near_matches(view, wo["oms_premise_id"])
            um = {"wo": wo, "suggestion": cands[0]["premise"] if cands else None, "exposure": None, "account_id": None, "res": None}
            if cands:
                r2 = resolve_premise(view, cands[0]["premise"], dd)
                if r2:
                    a = analyze(view, wo, r2, disconnect_window(view, wo, wo["oms_premise_id"]), scan_start, scan_end)
                    um.update(exposure=a["credit"], account_id=r2["sa"]["account_id"], res=r2, analysis=a)
            back["unmatched"].append(um)
            if in_daily:
                daily["unmatched"].append(um)
            continue
        back["matched"] += 1
        win = disconnect_window(view, wo)
        a = analyze(view, wo, res, win, scan_start, scan_end)
        if a["flagged"]:
            back["findings"].append({"wo": wo, "res": res, "win": win, "account_id": res["sa"]["account_id"], **a})
        if in_daily:
            sa = res["sa"]
            if wo["wo_subtype"] == "NONPAY":
                daily["excluded_nonpay"].append({"wo": wo, "account_id": sa["account_id"]})
            elif sa["sa_status"] == "ACTIVE" or (sa.get("stop_processed_at") and sa["stop_processed_at"] > as_of_end):
                msg = view.latest_msg(wo["wo_id"])
                daily["holds"].append({"wo": wo, "account_id": sa["account_id"], "sa_id": sa["sa_id"], "res": res,
                                       "msg": msg, "todos": view.open_todos(wo["wo_id"], sa["sa_id"])})
    back["total_credit"] = money(sum((f["credit"] for f in back["findings"]), Decimal(0)))
    return {"back": back, "daily": daily, "scan_start": scan_start, "scan_end": scan_end, "as_of": as_of}


def backscan_rows(view):
    """Back-scan as of the scenario date, classified for 'Send to workflow'. FINDING rows with a credit and the
    unmatched row with a near match are eligible; REVIEW rows are revenue protection - out of scope."""
    det = detect(view, view.Tdate)
    rows = []
    for f in det["back"]["findings"]:
        wo, res = f["wo"], f["res"]
        eligible = f["credit"] > 0
        acc = res["account"] or {}
        rows.append({"wo_id": wo["wo_id"], "account_id": f["account_id"], "customer_name": acc.get("customer_name", ""),
                     "kind": "FINDING" if eligible else "REVIEW", "severity": f["severity"], "eligible": eligible,
                     "reason": None if eligible else "Out of scope – revenue protection", "credit": f2(f["credit"]),
                     "phantom_kwh": float(f["phantom_kwh"]), "unbilled_kwh": float(f["unbilled_kwh"]),
                     "bill_ids": [l["bill_id"] for l in f["lines"] if l["credit"] > 0], "subtype": wo["wo_subtype"],
                     "disconnect_at": wo["completed_at"], "premise": wo["oms_premise_id"], "suggested_premise": None})
    for um in det["back"]["unmatched"]:
        a = um.get("analysis")
        acc = (um["res"] or {}).get("account") or {}
        ok = bool(um["res"]) and um["exposure"] is not None and um["exposure"] > 0
        rows.append({"wo_id": um["wo"]["wo_id"], "account_id": um["account_id"], "customer_name": acc.get("customer_name", ""),
                     "kind": "UNMATCHED", "severity": None, "eligible": ok, "reason": None if ok else "No near-match premise to link",
                     "credit": f2(um["exposure"] or 0), "phantom_kwh": float(a["phantom_kwh"]) if a else 0.0, "unbilled_kwh": 0.0,
                     "bill_ids": [l["bill_id"] for l in a["lines"] if l["credit"] > 0] if a else [], "subtype": um["wo"]["wo_subtype"],
                     "disconnect_at": um["wo"]["completed_at"], "premise": um["wo"]["oms_premise_id"], "suggested_premise": um["suggestion"]})
    return rows


def finding_json(f):
    return {"wo_id": f["wo"]["wo_id"], "account_id": f["account_id"], "customer_name": (f["res"]["account"] or {}).get("customer_name", ""),
            "severity": f["severity"], "credit": f2(f["credit"]), "phantom_kwh": float(f["phantom_kwh"]), "unbilled_kwh": float(f["unbilled_kwh"]),
            "bills": [{"bill_id": l["bill_id"], "days": l["days"], "phantom_kwh": float(l["phantom"]), "consumption_credit": f2(l["cons"]),
                       "fixed_credit": f2(l["fixed_credit"]), "fixed_retained": f2(l["fixed_retained"]), "credit": f2(l["credit"])} for l in f["lines"]],
            "subtype": f["wo"]["wo_subtype"], "disconnect_at": f["wo"]["completed_at"], "premise": f["wo"]["oms_premise_id"]}


def answer_key_checks(ds):
    """POC 1 tests: back-scan and daily check vs answer_key.csv (one dataset). Returns check rows."""
    key = ds.table(F_KEY)
    if not key:
        return []
    hdr = next((h for h in ds.headers(F_KEY) if re.match(r"^daily_check_\d{4}_\d{2}_\d{2}$", h)), None)
    as_of = hdr[12:].replace("_", "-") if hdr else "2026-11-02"
    res = detect(View(ds), as_of)
    rows, listed = [], set()
    for k in key:
        ids = ds.accounts_by_code.get(k["case"], [])
        listed.update(ids)
        f = next((x for x in res["back"]["findings"] if x["account_id"] in ids), None)
        u = next((x for x in res["back"]["unmatched"] if x.get("account_id") in ids), None)
        tot, kind = money(k["credit_total"]), k["back_scan_result"]
        if kind == "FINDING":
            ok = bool(f) and f["severity"] == k["severity"] and abs(f["credit"] - tot) <= Decimal("0.01")
            act = "%s %s" % (f["severity"], f2(f["credit"])) if f else "none"
            if ok and k["severity"] == "REVIEW":
                ok = abs(D(f["unbilled_kwh"]) - D(re.search(r"(\d+) kWh ACTUAL", k["cause_and_notes"]).group(1))) <= 1 if re.search(r"(\d+) kWh ACTUAL", k["cause_and_notes"]) else ok
                act += " (%s kWh ACTUAL after disconnect, in scan range)" % f["unbilled_kwh"]
        elif kind == "UNMATCHED":
            ok = bool(u) and not f and abs(D(u["exposure"] or 0) - tot) <= Decimal("0.01")
            act = "UNMATCHED %s" % (f2(u["exposure"] or 0) if u else "-")
        else:
            ok = not f
            act = "no finding" if not f else "finding %s" % f2(f["credit"])
        rows.append({"group": "POC 1 back-scan - " + ds.name, "check": "%s %s" % (k["case"], k["customer_name"]),
                     "expected": "%s %s %s" % (kind, k["severity"], k["credit_total"]), "actual": act, "pass": ok})
        if hdr:
            held = any(h["account_id"] in ids for h in res["daily"]["holds"])
            dun = any(x.get("account_id") in ids for x in res["daily"]["unmatched"])
            a2 = "HOLD" if held else "UNMATCHED" if dun else "NONE"
            rows.append({"group": "POC 1 daily check %s - %s" % (as_of, ds.name), "check": "%s %s" % (k["case"], k["customer_name"]),
                         "expected": k[hdr], "actual": a2, "pass": a2 == k[hdr]})
    extra = [f["account_id"] for f in res["back"]["findings"] if f["account_id"] not in listed]
    rows.append({"group": "POC 1 back-scan - " + ds.name, "check": "No findings outside the answer key", "expected": "none",
                 "actual": ", ".join(extra) or "none", "pass": not extra})
    total = res["back"]["total_credit"]
    um = sum((D(u["exposure"] or 0) for u in res["back"]["unmatched"]), Decimal(0))
    rows.append({"group": "POC 1 back-scan - " + ds.name, "check": "Back-scan total (+ unmatched exposure)", "expected": "see prompt §18",
                 "actual": "%s + unmatched %s" % (f2(total), f2(um)), "pass": True, "total": f2(total), "unmatched": f2(um)})
    return rows
