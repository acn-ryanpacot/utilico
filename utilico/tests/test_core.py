"""Unit tests: POC 1 parity, calculator, rules, point-in-time view."""
from decimal import Decimal

from app import backscan
from app.calculator import draft_correction, project_exposure
from app.config import Config
from app.pit import View, disconnect_window, resolve_premise
from app.rules import fired, near_matches, probable_cause, run_rules
from app.util import money


def test_poc1_answer_keys(registry):
    for d in registry.list():
        rows = backscan.answer_key_checks(registry.get(d["id"]))
        assert rows and all(r["pass"] for r in rows), [r for r in rows if not r["pass"]]


def test_backscan_totals(registry):
    a = backscan.detect(View(registry.get("dataset_A_with_errors")), "2026-11-02")
    b = backscan.detect(View(registry.get("dataset_B_two_errors_corrected")), "2026-11-02")
    assert a["back"]["total_credit"] == Decimal("1992.59")
    assert b["back"]["total_credit"] == Decimal("1489.42")
    assert [u["exposure"] for u in a["back"]["unmatched"]] == [Decimal("877.50")]


def test_money_half_up():
    assert money("2.345") == Decimal("2.35") and money("2.344") == Decimal("2.34")


def _ctx(v, wo_id, bill_ids):
    wo = v.wos[wo_id]
    res = resolve_premise(v, wo["oms_premise_id"], wo["completed_at"][:10])
    return {"wo_id": wo_id, "subtype": wo["wo_subtype"], "recorded_premise": wo["oms_premise_id"], "premise": wo["oms_premise_id"],
            "match_status": "NONE", "account_id": res["sa"]["account_id"], "sa_id": res["sa"]["sa_id"], "meter_id": res["meter_id"],
            "window": disconnect_window(v, wo), "bill_ids": bill_ids}, res


def test_rules_point_in_time(registry):
    ds, cfg = registry.get("dataset_A_with_errors"), Config()
    v = View(ds, "2026-11-02 01:00", drafts={"BL-4400101-202610"})
    ctx, res = _ctx(v, "WO-104242", ["BL-4400101-202610"])
    r = run_rules(v, ctx, cfg.rules())
    assert fired(r) == ["R1", "R2", "R3", "PB1", "PB2"]
    assert probable_cause(v, ctx, r, cfg.rules())[0] == "C2"
    corr = draft_correction(v, ctx["subtype"], res["sa"], v.bills["BL-4400101-202610"], ctx["window"], ctx["meter_id"])
    assert corr["after"] == 1138.46
    for T, sev in (("2026-11-04 09:40", "Medium"), ("2026-11-09 15:30", "High")):
        v2 = View(ds, T)
        r2 = run_rules(v2, _ctx(v2, "WO-104242", ["BL-4400101-202610"])[0], cfg.rules())
        assert r2["R4"]["fired"] and r2["R4"]["severity"] == sev


def test_projected_exposure(registry):
    ds = registry.get("dataset_A_with_errors")
    for wo_id, T, prem, expect, period_end in (("WO-104249", "2026-08-10 16:05", None, 145.35, None), ("WO-104256", "2026-09-23 13:10", "PRM-00417", 472.70, None),
                                               ("WO-104242", "2026-10-15 10:42", None, 1406.36, None), ("WO-104277", "2026-10-30 18:40", None, 12.32, None),
                                               ("WO-104291", "2026-11-02 01:00", None, 1553.84, "2026-10-31")):
        v = View(ds, T)
        wo = v.wos[wo_id]
        res = resolve_premise(v, prem or wo["oms_premise_id"], wo["completed_at"][:10])
        p = project_exposure(v, res["sa"], res["meter_id"], wo["completed_at"][:10], period_end)
        assert abs(p["exposure"] - expect) <= 0.02, (wo_id, p)


def test_pb3_near_match(registry):
    v = View(registry.get("dataset_A_with_errors"))
    assert [c["premise"] for c in near_matches(v, "PRM-0O417")] == ["PRM-00417"]
