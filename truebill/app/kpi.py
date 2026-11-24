"""KPIs and the Day-30 gate (prompt §16), calculated from the audit log, the evidence store and case state."""
import statistics
from datetime import datetime

from .util import D, f2

GATE = {"detection_min": 0.95, "false_positive_max": 0.05, "citations_min": 1.0, "packet_max_seconds": 300}


def _secs(a, b):
    if not a or not b:
        return None
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def expected_codes(cfg, ds_id, sc_id, followup_on):
    ex = cfg.expected()["scenarios"].get(ds_id, {}).get(sc_id, {})
    mode = ("followup_on" if followup_on else "followup_off") if sc_id == "PREVENTION_REPLAY" else "default"
    e = ex.get(mode, {})
    if "only" in e:
        return list(e["only"])
    if "cases_from" in e:
        a, b, c = e["cases_from"]
        e = cfg.expected()["scenarios"][a][b][c]
    return sorted({c["case"] for c in e.get("cases", [])})


def kpis(s):
    cases = list(s.state["cases"].values())
    codes = s.ds.case_codes
    packet = [x for x in (_secs(c["timings"].get("new_real"), c["timings"].get("recommended_real")) for c in cases) if x is not None]
    conflict = [x for x in (_secs(c["timings"].get("event_real"), c["timings"].get("reconciled_real")) for c in cases) if x is not None]
    exp = expected_codes(s.cfg, s.ds.id, s.sc_id, s.state["toggles"]["followup"])
    if s.sc_id == "BACKSCAN_2026-11-10" and not cases:
        exp_active = []
    else:
        exp_active = exp
    detected = sorted({codes.get(c["account_id"]) for c in cases if codes.get(c["account_id"]) in exp_active})
    fp = [c for c in cases if codes.get(c["account_id"]) not in exp]
    factual = cited = 0
    with_out = [c for c in cases if c.get("outputs") and c["outputs"].get("consistency")]
    for c in with_out:
        factual += c["outputs"]["citations"]["factual"]
        cited += c["outputs"]["citations"]["cited"]
    green = [c for c in cases if (c.get("triage") or {}).get("would_auto_approve") and c["triage"].get("agreed") is not None]
    slas = [s.sla_view(c)["worst"] for c in cases if c["state"] not in ("SEALED", "CANCELLED", "RESOLVED")]
    prevented = sum((D(p["amount"]) for p in s.state["prevented"]), D(0))
    remediated = sum((D(m["amount"]) for m in s.view().memos.values()), D(0))
    exposure = sum((D(c["amount"]) for c in cases if c["type"] == "ADVANCE" and c["state"] not in ("SEALED", "CANCELLED")), D(0))
    bills = s.state["bills"].values()
    out = {
        "packet_seconds": {"median": round(statistics.median(packet), 3) if packet else None, "max": round(max(packet), 3) if packet else None, "n": len(packet)},
        "conflict_seconds": {"median": round(statistics.median(conflict), 3) if conflict else None, "max": round(max(conflict), 3) if conflict else None},
        "detection_rate": (len(detected) / len(exp_active)) if exp_active else None, "expected": exp_active, "detected": detected,
        "false_positive_rate": (len(fp) / len(cases)) if cases else 0.0, "false_positives": [c["case_id"] for c in fp],
        "citation_coverage": (cited / factual) if factual else None, "factual_sentences": factual,
        "output_consistency": (sum(1 for c in with_out if c["outputs"]["consistency"]["pass"]) / len(with_out)) if with_out else None,
        "triage_agreement": (sum(1 for c in green if c["triage"]["agreed"]) / len(green)) if green else None, "green_cases": len(green),
        "sla_at_risk": slas.count("AT_RISK"), "sla_breached": slas.count("BREACHED"),
        "prevented": f2(prevented), "remediated": f2(remediated), "projected_exposure_open": f2(exposure),
        "bills": {k: sum(1 for b in bills if (b.get("outcome") == "HELD" if k == "held" else b.get("status") == k.upper()))
                  for k in ("held", "suppressed", "regenerated", "released")},
        "bills_passed": sum(1 for b in bills if b.get("outcome") == "PASSED"),
        "cases": len(cases),
        "by_state": _count(c["state"] for c in cases), "by_priority": _count(c["priority"] for c in cases),
        "by_triage": _count((c.get("triage") or {}).get("colour", "-") for c in cases),
        "by_type": _count(c["type"] for c in cases),
        "s_codes": _count(sc for c in cases for sc in (c.get("recommendation") or {}).get("s_codes", [])),
    }
    out["gate"] = gate(out)
    return out


def gate(k):
    rows = []

    def row(name, value, ok, target, fmt):
        rows.append({"name": name, "value": value, "display": fmt, "target": target, "pass": ok})
    d = k["detection_rate"]
    row("Detection rate", d, d is not None and d >= GATE["detection_min"], "≥ 95%", "-" if d is None else "%.0f%%" % (d * 100))
    f = k["false_positive_rate"]
    row("False positives", f, f < GATE["false_positive_max"], "< 5%", "%.0f%%" % (f * 100))
    c = k["citation_coverage"]
    row("Citation coverage", c, c is not None and c >= GATE["citations_min"], "100%", "-" if c is None else "%.1f%%" % (c * 100))
    p = k["packet_seconds"]["max"]
    row("Packet assembly (max)", p, p is not None and p < GATE["packet_max_seconds"], "< 5 min", "-" if p is None else "%.1f s" % p)
    return {"rows": rows, "pass": all(r["pass"] for r in rows)}


def _count(it):
    out = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out
