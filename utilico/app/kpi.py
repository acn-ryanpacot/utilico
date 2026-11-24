"""KPIs and the Day-30 gate, calculated from the audit log, the evidence store and case state.

Money is counted when the DRI approves: $ remediated = approved REMEDIATE credits, $ exposure addressed = approved
ADVANCE projections. Detection rate = expected back-scan cases detected (once findings were sent); a false positive
is a case on an account outside the in-scope sample cases. CSR lookups that found nothing (NO_ISSUE) are not cases.
"""
import statistics
from datetime import datetime

from .util import D, f2

GATE = {"detection_min": 0.95, "false_positive_max": 0.05, "citations_min": 1.0, "packet_max_seconds": 300}
IN_SCOPE = {"E1", "E2", "E3", "E4", "E5", "E7"}


def _secs(a, b):
    if not a or not b:
        return None
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def expected_backscan(cfg, ds_id):
    e = cfg.expected()["backscan"].get(ds_id, {})
    if "only" in e:
        return list(e["only"])
    return sorted({c["case"] for c in e.get("cases", [])})


def citation_stats(s, cases):
    total = cited = 0
    for c in cases:
        stmts = [e["fact_ids"] for e in (c.get("reconciliation") or {}).get("explanations", [])]
        if c.get("recommendation"):
            stmts.append(c["recommendation"].get("fact_ids") or [])
        for ids in stmts:
            total += 1
            if ids and all(f in s.evidence.by_id for f in ids):
                cited += 1
    return total, cited


def kpis(s):
    cases = [c for c in s.state["cases"].values() if c["state"] != "NO_ISSUE"]
    codes = s.ds.case_codes
    packet = [x for x in (_secs(c["timings"].get("new_real"), c["timings"].get("recommended_real")) for c in cases) if x is not None]
    conflict = [x for x in (_secs(c["timings"].get("event_real"), c["timings"].get("reconciled_real")) for c in cases) if x is not None]
    exp = expected_backscan(s.cfg, s.ds.id) if s.state.get("backscan_sent") else []
    detected = sorted({codes.get(c["account_id"]) for c in cases if codes.get(c["account_id"]) in exp})
    fp = [c for c in cases if codes.get(c["account_id"]) not in IN_SCOPE]
    total, cited = citation_stats(s, cases)
    green = [c for c in cases if (c.get("triage") or {}).get("would_auto_approve") and c["triage"].get("agreed") is not None]
    slas = [s.sla_view(c)["worst"] for c in cases if c["state"] not in ("APPROVED", "REJECTED")]
    appr = s.state.get("approved", [])

    def total_of(kind):
        return f2(sum((D(a["amount"]) for a in appr if a["kind"] == kind), D(0)))
    open_amt = sum((D(c["amount"]) for c in cases if c["state"] not in ("APPROVED", "REJECTED")), D(0))
    out = {
        "packet_seconds": {"median": round(statistics.median(packet), 3) if packet else None, "max": round(max(packet), 3) if packet else None, "n": len(packet)},
        "conflict_seconds": {"median": round(statistics.median(conflict), 3) if conflict else None, "max": round(max(conflict), 3) if conflict else None},
        "detection_rate": (len(detected) / len(exp)) if exp else None, "expected": exp, "detected": detected,
        "false_positive_rate": (len(fp) / len(cases)) if cases else 0.0, "false_positives": [c["case_id"] for c in fp],
        "citation_coverage": (cited / total) if total else None, "agent_statements": total,
        "triage_agreement": (sum(1 for c in green if c["triage"]["agreed"]) / len(green)) if green else None, "green_cases": len(green),
        "sla_at_risk": slas.count("AT_RISK"), "sla_breached": slas.count("BREACHED"),
        "remediated": total_of("remediated"), "exposure_addressed": total_of("exposure_addressed"), "open_amount": f2(open_amt),
        "approved": sum(1 for c in cases if c["state"] == "APPROVED"), "rejected": sum(1 for c in cases if c["state"] == "REJECTED"),
        "lookups": sum(1 for c in s.state["cases"].values() if c.get("lookup")),
        "lookups_no_issue": sum(1 for c in s.state["cases"].values() if c["state"] == "NO_ISSUE"),
        "cases": len(cases), "backscan_sent": len(s.state.get("backscan_sent", [])),
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
