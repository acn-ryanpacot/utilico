"""Agent tools. Reads go through the point-in-time view; every record returned becomes an evidence fact
(deduplicated per case) and comes back with its fact_id so the agent can cite it. Agents never calculate money -
`calculate` and `project_exposure` do. There are no write tools: the process ends at the DRI's decision.
"""
from ..calculator import draft_correction, issued_credit, project_exposure
from ..datasource import F_BILL, F_CRM, F_GL, F_MSG, F_MTR, F_PER, F_READ, F_SA, F_TODO, F_WO, F_XW, SYSTEM_OF_FILE
from ..rules import fired, near_matches, probable_cause, run_rules, s_codes
from ..util import D, day_range, f2


class ToolError(Exception):
    pass


class GuardrailError(Exception):
    pass


SPECS = {
    "get_work_orders": "OMS work orders on a premise (point in time)",
    "get_integration_messages": "Integration messages for a work order",
    "get_todos": "CC&B To Do entries for a work order / SA",
    "get_service_agreement": "CC&B service agreement",
    "get_bills": "CC&B bill segments of an SA (draft / issued state at scenario time)",
    "get_daily_reads": "MDM daily reads of a meter in a date range (summarised as one fact)",
    "get_meter": "MDM meter",
    "get_crm_cases": "CRM cases of an account",
    "get_gl_lines": "GL journal lines of bills",
    "get_gl_period": "GL period status at scenario time",
    "find_similar_premises": "Crosswalk premises within Levenshtein 1 (O/0, I/1 equal)",
    "run_rules": "Run R1-R4 and PB1-PB3 (+ probable cause, S-codes) for the case",
    "calculate": "Deterministic money: draft correction or issued-bill credit",
    "project_exposure": "Deterministic projected exposure this cycle (ADVANCE)",
    "search_knowledge": "Similar past cases (context only)",
}
READ_TOOLS = ["get_work_orders", "get_integration_messages", "get_todos", "get_service_agreement", "get_bills", "get_daily_reads",
              "get_meter", "get_crm_cases", "get_gl_lines", "get_gl_period", "find_similar_premises"]

_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}
_ARGS = {
    "get_work_orders": ({"premise_id": _STRING, "wo_id": _STRING}, ["premise_id"]),
    "get_integration_messages": ({"wo_id": _STRING}, ["wo_id"]),
    "get_todos": ({"wo_id": _STRING, "sa_id": _STRING}, ["wo_id"]),
    "get_service_agreement": ({"sa_id": _STRING}, ["sa_id"]),
    "get_bills": ({"sa_id": _STRING, "bill_ids": _STRINGS}, ["sa_id"]),
    "get_daily_reads": ({"meter_id": _STRING, "from": _STRING, "to": _STRING, "purpose": _STRING}, ["meter_id", "from", "to"]),
    "get_meter": ({"meter_id": _STRING}, ["meter_id"]),
    "get_crm_cases": ({"account_id": _STRING}, ["account_id"]),
    "get_gl_lines": ({"bill_ids": _STRINGS}, ["bill_ids"]),
    "get_gl_period": ({"period": _STRING}, ["period"]),
    "find_similar_premises": ({"premise_id": _STRING}, ["premise_id"]),
    "run_rules": ({}, []),
    "calculate": ({"kind": {"type": "string", "enum": ["draft", "credit"]}, "bill_id": _STRING}, ["kind"]),
    "project_exposure": ({"after_period_end": {"type": ["string", "null"]}}, []),
    "search_knowledge": ({"query": _STRING, "collection": _STRING, "k": {"type": "integer"}}, ["query"]),
    "get_case_packet": ({}, []),
}


def specs(names):
    return [{"name": n, "description": SPECS.get(n, n), "input_schema": {
        "type": "object", "properties": _ARGS.get(n, ({}, []))[0], "required": _ARGS.get(n, ({}, []))[1]}} for n in names]


def pick(row, keys):
    return {k: row.get(k) for k in keys if k in row}


class ToolContext:
    def __init__(self, session, case, agent_id, lane=None):
        self.s, self.case, self.agent_id, self.lane = session, case, agent_id, lane

    def view(self, sandbox=True):
        return self.s.view(sandbox=sandbox)

    def fact(self, file, record_id, fields, value=None, kind="record"):
        f, _ = self.s.evidence.add(self.case["case_id"], SYSTEM_OF_FILE.get(file, "SANDBOX"), file, record_id, fields,
                                   value if value is not None else fields, self.s.clock, self.agent_id, kind)
        return f["fact_id"]

    def fact_for(self, record_id):
        for f in self.s.evidence.for_case(self.case["case_id"]):
            if f["record_id"] == record_id and f.get("kind", "record") == "record":
                return f["fact_id"]
        return None

    def ensure_fact(self, record_id):
        """Fact id for a record cited by a rule (registers it when no agent has read it yet)."""
        fid = self.fact_for(record_id)
        if fid:
            return fid
        hit = self.view().record(record_id)
        if not hit:
            raise ToolError("cited record %s does not exist at scenario time" % record_id)
        file, row = hit
        return self.fact(file, record_id, {k: v for k, v in row.items() if not k.startswith("_")})


# ---- read tools --------------------------------------------------------------------------------
WO_F = ["wo_id", "oms_premise_id", "wo_type", "wo_subtype", "status", "created_at", "completed_at"]
MSG_F = ["msg_id", "wo_id", "message_type", "received_at", "processed_at", "status", "retry_count", "error_message"]
TODO_F = ["todo_id", "created_at", "todo_type", "related_entity", "sa_id", "message", "priority", "status", "assigned_to"]
SA_F = ["sa_id", "account_id", "ccb_sp_id", "premise_address", "tariff_code", "sa_status", "start_date", "stop_date", "stop_processed_at"]
BILL_F = ["bill_id", "sa_id", "bill_type", "period_start", "period_end", "bill_run_at", "days_billed", "kwh_billed", "usage_quality",
          "total_amount", "bill_status", "_state"]
CRM_F = ["case_id", "account_id", "opened_at", "channel", "category", "subject", "status", "closed_at", "billing_hold_set", "notes"]


def t_get_work_orders(ctx, a):
    v = ctx.view()
    rows = [w for w in v.wos_by_premise.get(a["premise_id"], []) if w["wo_type"] in ("DISCONNECT", "RECONNECT")]
    if a.get("wo_id") and a["wo_id"] in v.wos and v.wos[a["wo_id"]] not in rows:
        rows.append(v.wos[a["wo_id"]])
    out = [dict(pick(w, WO_F), fact_id=ctx.fact(F_WO, w["wo_id"], pick(w, WO_F))) for w in sorted(rows, key=lambda w: w.get("completed_at") or w["created_at"])]
    return {"work_orders": out}, "%d work order(s) on %s" % (len(out), a["premise_id"])


def t_get_integration_messages(ctx, a):
    v = ctx.view()
    rows = sorted(v.msgs_by_wo.get(a["wo_id"], []), key=lambda m: m["received_at"])
    out = [dict(pick(m, MSG_F), fact_id=ctx.fact(F_MSG, m["msg_id"], pick(m, MSG_F))) for m in rows]
    return {"messages": out}, ", ".join("%s %s" % (m["msg_id"], m["status"]) for m in out) or "no message"


def t_get_todos(ctx, a):
    v = ctx.view()
    rows = v.todos_for(a["wo_id"], a.get("sa_id"))
    out = [dict(pick(x, TODO_F), fact_id=ctx.fact(F_TODO, x["todo_id"], pick(x, TODO_F))) for x in rows]
    return {"todos": out}, ", ".join("%s %s" % (x["todo_id"], x["status"]) for x in out) or "no To Do"


def t_get_service_agreement(ctx, a):
    v = ctx.view()
    sa = v.sas.get(a["sa_id"])
    if not sa:
        raise ToolError("SA %s not visible" % a["sa_id"])
    acc = v.accounts.get(sa["account_id"], {})
    fid = ctx.fact(F_SA, sa["sa_id"], pick(sa, SA_F))
    afid = ctx.fact("ccb_accounts.csv", acc.get("account_id", sa["account_id"]), pick(acc, ["account_id", "customer_name", "customer_class", "mailing_address", "account_status"])) if acc else None
    return {"service_agreement": dict(pick(sa, SA_F), fact_id=fid), "account": dict(pick(acc, ["account_id", "customer_name", "customer_class", "mailing_address"]), fact_id=afid)}, \
        "%s %s" % (sa["sa_id"], sa["sa_status"])


def t_get_bills(ctx, a):
    v = ctx.view()
    rows = sorted(v.bills_by_sa.get(a["sa_id"], []), key=lambda b: b["period_start"])
    want = set(a.get("bill_ids") or [])
    if want:
        rows = [b for b in rows if b["bill_id"] in want]
    out = [dict(pick(b, BILL_F), fact_id=ctx.fact(F_BILL, b["bill_id"], pick(b, BILL_F))) for b in rows]
    return {"bills": out}, ", ".join("%s %s %s" % (b["bill_id"], b["_state"], b["total_amount"]) for b in out)


def t_get_daily_reads(ctx, a):
    v = ctx.view()
    reads = v.reads_by_meter.get(a["meter_id"], {})
    frm, to = a["from"], min(a["to"], v.reads_until() or a["to"])
    days = [reads[d] for d in day_range(frm, to) if d in reads] if frm <= to else []
    if not days:
        return {"meter_id": a["meter_id"], "from": frm, "to": to, "days": 0, "fact_id": None}, "no reads visible %s..%s" % (frm, to)
    est = [r for r in days if r["read_quality"] == "ESTIMATED"]
    fields = {"meter_id": a["meter_id"], "from": days[0]["read_date"], "to": days[-1]["read_date"], "days": len(days),
              "estimated_days": len(est), "actual_days": sum(1 for r in days if r["read_quality"] == "ACTUAL"),
              "corrected_days": sum(1 for r in days if r["read_quality"] == "CORRECTED"),
              "kwh_total": str(sum((D(r["kwh"]) for r in days), D(0))), "first_read_id": days[0]["read_id"],
              "first_read_quality": days[0]["read_quality"], "first_estimated_date": est[0]["read_date"] if est else None,
              "first_estimated_read_id": est[0]["read_id"] if est else None, "purpose": a.get("purpose", "")}
    fid = ctx.fact(F_READ, days[0]["read_id"], fields, kind="read_window:%s:%s" % (frm, to))
    return dict(fields, fact_id=fid), "%d day(s), %d ESTIMATED" % (len(days), len(est))


def t_get_meter(ctx, a):
    v = ctx.view()
    m = v.meters.get(a["meter_id"])
    if not m:
        raise ToolError("meter %s not visible" % a["meter_id"])
    f = pick(m, ["mdm_meter_id", "mdm_sp_id", "meter_type", "connection_state", "connection_state_updated_at"])
    return dict(f, fact_id=ctx.fact(F_MTR, m["mdm_meter_id"], f)), "%s %s" % (m["mdm_meter_id"], m["connection_state"])


def t_get_crm_cases(ctx, a):
    v = ctx.view()
    rows = sorted(v.crm_by_account.get(a["account_id"], []), key=lambda c: c["opened_at"])
    out = [dict(pick(c, CRM_F), fact_id=ctx.fact(F_CRM, c["case_id"], pick(c, CRM_F))) for c in rows]
    return {"crm_cases": out}, ", ".join("%s %s" % (c["case_id"], c["category"]) for c in out) or "no CRM cases"


def t_get_gl_lines(ctx, a):
    v = ctx.view()
    out = []
    for b in a.get("bill_ids") or []:
        lines = v.gl_by_ref.get(b, [])
        if not lines:
            continue
        f = {"je_id": lines[0]["je_id"], "source_ref": b, "posting_date": lines[0]["posting_date"], "accounting_period": lines[0]["accounting_period"],
             "lines": [pick(x, ["je_line", "gl_account", "gl_account_name", "debit", "credit"]) for x in lines]}
        out.append(dict(f, fact_id=ctx.fact(F_GL, lines[0]["je_id"], f)))
    return {"journals": out}, ", ".join("%s %s" % (j["je_id"], j["accounting_period"]) for j in out) or "no journal posted"


def t_get_gl_period(ctx, a):
    v = ctx.view()
    p = v.periods.get(a["period"])
    if not p:
        raise ToolError("period %s unknown" % a["period"])
    f = pick(p, ["accounting_period", "status", "closed_at"])
    return dict(f, fact_id=ctx.fact(F_PER, p["accounting_period"], f)), "%s %s" % (p["accounting_period"], p["status"])


def t_find_similar_premises(ctx, a):
    v = ctx.view(sandbox=False)
    out = []
    for c in near_matches(v, a["premise_id"]):
        xw = v.xw[c["premise"]]
        f = dict(c, recorded=a["premise_id"], ccb_sp_id=xw["ccb_sp_id"], mdm_meter_id=xw["mdm_meter_id"])
        out.append(dict(f, fact_id=ctx.fact(F_XW, c["premise"], f, kind="near_match")))
    return {"candidates": out}, ", ".join(c["premise"] for c in out) or "no near match"


# ---- deterministic tools -------------------------------------------------------------------------
def t_run_rules(ctx, a):
    s, case = ctx.s, ctx.case
    # a regulatory response reports the findings as they happened (the linked case's remediation is in the sandbox)
    v = ctx.view(sandbox=case["type"] != "REGULATORY_RESPONSE")
    rc = s.cfg.rules()
    rctx = s.rule_ctx(case)
    res = run_rules(v, rctx, rc)
    for r in res.values():
        r["fact_ids"] = sorted({ctx.ensure_fact(x) for x in r["records"] if x})
    cause, label, quote = probable_cause(v, rctx, res, rc, s.affected_run(case))
    sc = s_codes(cause, res, rc)
    return {"rules": res, "fired": fired(res), "cause": cause, "cause_label": label, "cause_quote": quote, "s_codes": sc,
            "rule_set_version": rc["rule_set_version"]}, "fired " + (", ".join(fired(res)) or "none") + " · cause " + cause


def t_calculate(ctx, a):
    s, case = ctx.s, ctx.case
    base = s.view(sandbox=False)           # money on the bills as issued / drafted, never on corrected reads
    wo = base.wos.get(case["wo_id"])
    win = case["window"]
    sa = base.sas[case["sa_id"]]
    out = {"kind": a["kind"]}
    if a["kind"] == "draft":
        b = base.bills[a["bill_id"]]
        stop = case["window"]["disconnect_date"]
        corr = draft_correction(base, case["subtype"], sa, b, win, case["meter_id"], stop_date=stop)
        if case["subtype"] == "NONPAY":
            corr = dict(corr, action="REGENERATE") if corr["action"] == "CANCEL" else corr
        out.update(corr)
        amount = corr["reduction"]
        fid = ctx.fact(F_BILL, b["bill_id"], {"before": corr["before"], "after": corr["after"], "reduction": corr["reduction"],
                                              "action": corr["action"], "fixed_retained": corr["fixed_retained"],
                                              "overlap": corr["line"] and {k: corr["line"][k] for k in ("from", "to", "overlap_days", "phantom_kwh")}},
                       value=amount, kind="calculation")
        summary = "%s %s → %s" % (corr["action"], corr["before"], corr["after"])
    else:
        sp = [base.sas[x] for x in case["sp_sa_ids"] if x in base.sas]
        cr = issued_credit(base, case["subtype"], sp, win, case["meter_id"])
        lines = [l for l in cr["lines"] if not case.get("bill_ids") or l["bill_id"] in case["bill_ids"]]
        credit = f2(sum((D(l["credit"]) for l in lines), D(0)))
        out.update(credit=credit, lines=lines, bill_ids=[l["bill_id"] for l in lines], period=base.current_period())
        amount = credit
        fid = ctx.fact(F_BILL, lines[0]["bill_id"] if lines else case["bill_ids"][0],
                       {"credit": credit, "bill_ids": out["bill_ids"], "period": out["period"],
                        "per_bill": [{k: l[k] for k in ("bill_id", "credit", "consumption_credit", "fixed_credit", "phantom_kwh", "overlap_days")} for l in lines]},
                       value=amount, kind="calculation")
        summary = "credit %s on %s" % (credit, ", ".join(out["bill_ids"]))
    out["amount"], out["fact_id"] = amount, fid
    return out, summary


def t_project_exposure(ctx, a):
    s, case = ctx.s, ctx.case
    v = ctx.view(sandbox=False)
    sa = v.sas[case["sa_id"]]
    p = project_exposure(v, sa, case["meter_id"], case["window"]["disconnect_date"], a.get("after_period_end"))
    fid = ctx.fact(F_SA, sa["sa_id"], p, value=p["exposure"], kind="projection")
    return dict(p, fact_id=fid, amount=p["exposure"]), "projected %s over %d day(s)" % (p["exposure"], p["days"])


def t_search_knowledge(ctx, a):
    hits = ctx.s.knowledge.search(a["query"], a.get("collection", "case_history"), a.get("k", 3))
    return {"results": hits, "context_only": True}, ", ".join(h["id"] for h in hits)


def t_get_case_packet(ctx, a):
    return ctx.s.packet(ctx.case), "packet for %s" % ctx.case["case_id"]


READ = {"get_work_orders": t_get_work_orders, "get_integration_messages": t_get_integration_messages, "get_todos": t_get_todos,
        "get_service_agreement": t_get_service_agreement, "get_bills": t_get_bills, "get_daily_reads": t_get_daily_reads,
        "get_meter": t_get_meter, "get_crm_cases": t_get_crm_cases, "get_gl_lines": t_get_gl_lines, "get_gl_period": t_get_gl_period,
        "find_similar_premises": t_find_similar_premises, "run_rules": t_run_rules, "calculate": t_calculate,
        "project_exposure": t_project_exposure, "search_knowledge": t_search_knowledge, "get_case_packet": t_get_case_packet}


def describe_args(a):
    return " ".join("%s=%s" % (k, ",".join(v) if isinstance(v, list) else v) for k, v in (a or {}).items() if k not in ("text", "purpose"))
