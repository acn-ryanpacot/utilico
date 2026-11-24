"""Execution playbooks (prompt §12). The server builds the canonical plan for the approved option and validates the
Execution agent's proposal against it; the agent may only run the next approved step."""
from ..util import add_days, fmt_money

CHECKS = {
    "CORRECT_PREMISE_REFERENCE": "The crosswalk now resolves",
    "REPROCESS_INTEGRATION_MESSAGE": "The sandbox status is SUCCESS",
    "STOP_SA": "SA is STOPPED with that date",
    "ZERO_POST_DISCONNECT_READS": "The read rule is active; visible reads in the window are 0 and CORRECTED",
    "REGENERATE_DRAFT_BILL": "Total = original − calculated reduction",
    "CANCEL_DRAFT_BILL": "The draft is cancelled",
    "CLOSE_TODO": "Status is COMPLETE",
    "CREATE_CREDIT_MEMO": "Amount = calculated credit, and the period is OPEN",
    "ADD_CRM_NOTE": "The note exists",
}


def build_plan(case, view):
    """Canonical step list for the case's approved option (empty for Release as-is / Flag / Respond)."""
    opt = (case.get("decision") or {}).get("option")
    if opt not in ("PRORATE", "SUPPRESS"):
        return []
    rec = case.get("recommendation") or {}
    calc = rec.get("calculation") or {}
    dd = case["window"]["disconnect_date"]
    sa = view.sas.get(case["sa_id"]) or {}
    msg = view.latest_msg(case["wo_id"])
    todos = view.open_todos(case["wo_id"], case["sa_id"])
    t = case["type"]
    steps = []

    def add(step, **params):
        steps.append({"step": step, "params": params})

    note = "AI-TrueBILL %s: %s - service disconnected %s (%s). %s" % (
        case["case_id"], "billing stopped at the disconnect date" if opt == "PRORATE" else "post-disconnect consumption removed",
        dd, case["wo_id"], ("Credit %s." % fmt_money(rec.get("amount", 0))) if t == "REMEDIATE" else "")

    def advance_steps(with_note=True):
        if (case.get("match") or {}).get("status") == "CONFIRMED":
            add("CORRECT_PREMISE_REFERENCE", wo_id=case["wo_id"], **{"from": case["recorded_premise"], "to": case["suggested_premise"]})
        if msg and msg["status"] != "SUCCESS":
            add("REPROCESS_INTEGRATION_MESSAGE", msg_id=msg["msg_id"])
        add("STOP_SA", sa_id=case["sa_id"], stop_date=dd)
        add("ZERO_POST_DISCONNECT_READS", meter=case["meter_id"], **{"from": add_days(dd, 1), "to": None})
        for x in todos:
            add("CLOSE_TODO", todo_id=x["todo_id"])
        if with_note:
            add("ADD_CRM_NOTE", account_id=case["account_id"], text=note.strip())

    if opt == "PRORATE":
        if t in ("ADVANCE", "PREVENT"):
            advance_steps()
            if t == "PREVENT":
                b = view.bills.get(case["draft_bill_id"]) or {}
                if dd < b.get("period_start", dd):
                    add("CANCEL_DRAFT_BILL", bill_id=case["draft_bill_id"])
                else:
                    add("REGENERATE_DRAFT_BILL", bill_id=case["draft_bill_id"])
        elif t == "REMEDIATE":
            if sa.get("sa_status") == "ACTIVE":
                advance_steps(with_note=False)
            add("CREATE_CREDIT_MEMO", bill_ids=list(calc.get("bill_ids") or case["bill_ids"]), amount=rec.get("amount"), period=view.current_period())
            add("ADD_CRM_NOTE", account_id=case["account_id"], text=note.strip())
    else:   # SUPPRESS
        nonpay = case["subtype"] == "NONPAY"
        add("ZERO_POST_DISCONNECT_READS", meter=case["meter_id"], **{"from": case["window"]["start"], "to": case["window"]["end"] if nonpay else None})
        if t == "PREVENT":
            b = view.bills.get(case["draft_bill_id"]) or {}
            if not nonpay and dd < b.get("period_start", dd):
                add("CANCEL_DRAFT_BILL", bill_id=case["draft_bill_id"])
            else:
                add("REGENERATE_DRAFT_BILL", bill_id=case["draft_bill_id"])
        elif t == "REMEDIATE":
            add("CREATE_CREDIT_MEMO", bill_ids=list(calc.get("bill_ids") or case["bill_ids"]), amount=rec.get("amount"), period=view.current_period())
            add("ADD_CRM_NOTE", account_id=case["account_id"], text=note.strip())
        else:
            add("ADD_CRM_NOTE", account_id=case["account_id"], text=note.strip())
    return [dict(s, seq=i + 1, check=CHECKS[s["step"]]) for i, s in enumerate(steps)]
