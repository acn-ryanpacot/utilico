"""Deterministic MockLLMClient policies, one per agent (prompt §11 "Mock policy").

Each policy looks only at the brief it was given and the tool results in the conversation, and returns the next
tool call(s) or its final JSON. Text is templated. Money always comes from `calculate` / `project_exposure`.
"""
from .. import outputs as O
from ..util import add_days, fmt_money
from .llm import call, policy, tool_results

LANES = ("OMS", "CCB", "MDM", "CRM", "GL")


def _fact_ids(obj, acc=None):
    acc = acc if acc is not None else []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "fact_id" and isinstance(v, str) and v not in acc:
                acc.append(v)
            elif k == "fact_ids" and isinstance(v, list):
                acc.extend(x for x in v if x not in acc)
            else:
                _fact_ids(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _fact_ids(v, acc)
    return acc


def _next(messages, plan):
    """Return the first planned call not yet made (plan = list of (name, input))."""
    done = tool_results(messages)
    made = [(n, i) for n, i, _ in done]
    for k, (name, inp) in enumerate(plan):
        if (name, inp) not in made:
            return {"tool_calls": [call(name, len(made) + 1, **inp)]}
    return None


# ---- Data Retrieval (five lanes, run concurrently by the orchestrator) -------------------------------
@policy("agent.retrieval")
def retrieval(messages, tools, b):
    lane = b["lane"]
    done = tool_results(messages)
    plan = []
    dd = b["window"]["disconnect_date"]
    if lane == "OMS":
        plan.append(("get_work_orders", {"premise_id": b["recorded_premise"], "wo_id": b["wo_id"]}))
        if b.get("suggested_premise"):
            plan.append(("get_work_orders", {"premise_id": b["suggested_premise"]}))
        plan.append(("get_integration_messages", {"wo_id": b["wo_id"]}))
        if b.get("unmatched"):
            plan.append(("find_similar_premises", {"premise_id": b["recorded_premise"]}))
    elif lane == "CCB":
        plan.append(("get_service_agreement", {"sa_id": b["sa_id"]}))
        if b.get("bill_ids"):
            plan.append(("get_bills", {"sa_id": b["sa_id"], "bill_ids": b["bill_ids"]}))
        plan.append(("get_todos", {"wo_id": b["wo_id"], "sa_id": b["sa_id"]}))
    elif lane == "MDM":
        plan.append(("get_meter", {"meter_id": b["meter_id"]}))
        plan.append(("get_daily_reads", {"meter_id": b["meter_id"], "from": add_days(dd, -30), "to": dd, "purpose": "baseline"}))
        plan.append(("get_daily_reads", {"meter_id": b["meter_id"], "from": add_days(dd, 1), "to": b["window"]["end"] or b["reads_until"],
                                         "purpose": "post-disconnect"}))
    elif lane == "CRM":
        plan.append(("get_crm_cases", {"account_id": b["account_id"]}))
    elif lane == "GL":
        if b.get("issued_bill_ids"):
            plan.append(("get_gl_lines", {"bill_ids": b["issued_bill_ids"]}))
        periods = []
        for n, i, r in done:
            if n == "get_gl_lines":
                periods += [j["accounting_period"] for j in r.get("journals", [])]
        for p in sorted(set(periods)) + [b["current_period"]]:
            if p and ("get_gl_period", {"period": p}) not in plan:
                plan.append(("get_gl_period", {"period": p}))
    nxt = _next(messages, plan)
    if nxt:
        return nxt
    fids = _fact_ids([r for _, _, r in done])
    return {"final": {"lane": lane, "fact_ids": fids, "records": len(fids)}}


# ---- Reconciliation: run_rules, then one plain-English explanation per fired rule ---------------------
def explain(rule, r, b):
    d = r["details"]
    dd = b["window"]["disconnect_date"]
    if rule == "R1":
        rec = b["window"].get("reconnect_wo_id")
        return "; ".join("Bill %s covers %d day(s) after the disconnect on %s (%s to %s%s)" % (
            x["bill_id"], x["post_days"], dd, x["from"], x["to"], ", until reconnect %s" % rec if rec else "") for x in d) + "."
    if rule == "R2":
        return "; ".join("%d post-disconnect day(s) on bill %s carry ESTIMATED reads, from %s" % (x["estimated_days"], x["bill_id"], x["read_ids"][0]) for x in d) + "."
    if rule == "R3":
        return "; ".join("CRM case %s (%s, opened %s) was logged on or after the disconnect with no billing hold" % (x["case_id"], x["category"], x["opened_at"][:10]) for x in d) + "."
    if rule == "R4":
        return "; ".join("Bill %s is disputed and journal %s is posted to period %s, which is %s - severity %s" % (
            x["bill_id"], x["je_id"], x["period"], x["period_status"], r["severity"]) for x in d) + "."
    if rule == "PB1":
        return "SA %s is still ACTIVE although work order %s (%s) completed on %s." % (d[0]["sa_id"], b["wo_id"], b["subtype"], dd)
    if rule == "PB2":
        x = d[0]
        return "Integration message %s is %s at scenario time%s." % (x["msg_id"], x["status"], (": " + x["error_message"]) if x.get("error_message") else "")
    if rule == "PB3":
        x = d[0]
        return "OMS premise %s is not in the crosswalk; %s differs by one look-alike character." % (x["recorded"], x["suggested"])
    return rule


@policy("agent.reconciliation")
def reconciliation(messages, tools, b):
    done = tool_results(messages)
    if not any(n == "run_rules" for n, _, _ in done):
        return {"tool_calls": [call("run_rules", 1)]}
    res = next(r for n, _, r in done if n == "run_rules")
    exps = []
    for rule in res["fired"]:
        r = res["rules"][rule]
        exps.append({"rule": rule, "text": explain(rule, r, b), "fact_ids": r["fact_ids"]})
    return {"final": {"fired": res["fired"], "explanations": exps, "fact_ids": _fact_ids(exps), "rule_set_version": res["rule_set_version"]}}


# ---- Recommendation -----------------------------------------------------------------------------------
LABEL = {"PRORATE": "Prorate", "SUPPRESS": "Suppress", "FLAG": "Flag", "RESPOND": "Respond"}


def signals(b, res):
    """Confidence signals (prompt §11): 0.60 + 0.10 each, capped at 0.95."""
    facts = b["facts"]
    out = []
    dd = b["window"]["disconnect_date"]
    msg = next((f for f in facts if f["file"] == "integration_message_log.csv" and f["fields"].get("wo_id") == b["wo_id"] and f.get("kind", "record") == "record"), None)
    if msg:
        st, proc = msg["fields"].get("status"), msg["fields"].get("processed_at") or ""
        if st in ("ERROR", "SKIPPED", "UNPROCESSED") or (b.get("affected_run") and proc and proc > b["affected_run"]):
            out.append(("integration", "Integration message %s %s%s" % (msg["record_id"], st, " (processed after the bill run)" if st == "SUCCESS" else ""), msg["fact_id"]))
    todo = next((f for f in facts if f["file"] == "ccb_todo_entries.csv" and f["fields"].get("status") == "OPEN"), None)
    if todo:
        out.append(("todo", "Open To Do %s" % todo["record_id"], todo["fact_id"]))
    post = next((f for f in facts if f.get("kind", "").startswith("read_window") and f["fields"].get("purpose") == "post-disconnect"), None)
    pb3 = "PB3" in res["fired"]
    if not pb3 and post and post["fields"].get("from") == add_days(dd, 1) and post["fields"].get("first_read_quality") == "ESTIMATED":
        out.append(("estimates", "ESTIMATED reads start the day after the disconnect (%s)" % post["fields"]["from"], post["fact_id"]))
    phrases = b["signal_phrases"]
    for f in facts:
        if f["file"] == "crm_cases.csv":
            text = ("%s %s" % (f["fields"].get("subject", ""), f["fields"].get("notes", ""))).lower()
            hit = next((p for p in phrases if p in text), None)
            if hit:
                out.append(("crm", "CRM %s mentions \"%s\"" % (f["record_id"], hit), f["fact_id"]))
                break
    if pb3 and post and post["fields"].get("first_estimated_date") and post["fields"]["first_estimated_date"] <= add_days(dd, b["pb3_days"]):
        out.append(("pb3", "Near-match meter turns ESTIMATED on %s, within %d days of the unmatched disconnect" % (post["fields"]["first_estimated_date"], b["pb3_days"]), post["fact_id"]))
    return out


@policy("agent.recommendation")
def recommendation(messages, tools, b):
    done = tool_results(messages)
    made = {n for n, _, _ in done}
    if "run_rules" not in made:
        return {"tool_calls": [call("run_rules", 1)]}
    res = next(r for n, _, r in done if n == "run_rules")
    t = b["type"]
    if t == "REGULATORY_RESPONSE":
        calc = {"amount": b["linked_amount"], "fact_id": None}
        kind = "linked_case"
    elif t == "ADVANCE":
        if "project_exposure" not in made:
            return {"tool_calls": [call("project_exposure", 2, after_period_end=b.get("non_blocking_period_end"))]}
        calc, kind = next(r for n, _, r in done if n == "project_exposure"), "projected_exposure"
    elif t == "PREVENT":
        if "calculate" not in made:
            return {"tool_calls": [call("calculate", 2, kind="draft", bill_id=b["draft_bill_id"])]}
        calc, kind = next(r for n, _, r in done if n == "calculate"), "reduction"
    else:
        if "calculate" not in made:
            return {"tool_calls": [call("calculate", 2, kind="credit")]}
        calc, kind = next(r for n, _, r in done if n == "calculate"), "credit"
    query = "%s %s %s cause %s %s" % (b.get("segment", ""), b["subtype"].replace("_", " ").lower(), " ".join(res["fired"]), res["cause"], res["cause_label"])
    if "search_knowledge" not in made:
        return {"tool_calls": [call("search_knowledge", 3, query=query, collection="case_history", k=3)]}
    similar = next(r for n, _, r in done if n == "search_knowledge")["results"]

    dd = b["window"]["disconnect_date"]
    pb3_open = "PB3" in res["fired"] and b["match_status"] != "CONFIRMED"
    if t == "REGULATORY_RESPONSE":
        option, label = "RESPOND", "Respond to the regulator from the sealed packet of %s" % b["linked_case_id"]
    elif pb3_open:
        option, label = "FLAG", "Flag - hold and escalate (ID match %s)" % b["match_status"].lower()
    elif b["subtype"] == "NONPAY":
        option, label = "SUPPRESS", "Suppress - zero the non-pay window, keep fixed charges"
    elif t == "PREVENT" and calc.get("action") == "CANCEL":
        option, label = "SUPPRESS", "Suppress (cancel the draft - the stop is before the billing period)"
    elif t == "ADVANCE":
        option, label = "PRORATE", "Prorate (stop effective %s)" % dd
    elif t == "PREVENT":
        option, label = "PRORATE", "Prorate (regenerate the final bill to %s)" % dd
    else:
        option, label = "PRORATE", "Prorate (credit %s)" % fmt_money(calc["amount"])
    sig = signals(b, res) if t != "REGULATORY_RESPONSE" else []
    conf = round(min(b["conf_cap"], b["conf_base"] + b["conf_step"] * len(sig)), 2)
    if t == "REGULATORY_RESPONSE":
        conf = b.get("linked_confidence") or conf
    amt = calc["amount"]
    alts = []
    for o in ("PRORATE", "SUPPRESS"):
        if o == option or t == "REGULATORY_RESPONSE":
            continue
        if o == "SUPPRESS":
            alts.append({"option": o, "label": "Suppress", "impact": "zero post-disconnect consumption only%s" % (" - fixed charges kept" if b["subtype"] == "NONPAY" else ""), "amount": None})
        else:
            alts.append({"option": o, "label": "Prorate", "impact": "stop the account at %s so billing ends there" % dd, "amount": amt})
    if t != "REGULATORY_RESPONSE":
        alts.append({"option": "RELEASE_AS_IS", "label": "Release as-is (override)", "impact": "no correction - %s stays with the customer" % fmt_money(amt), "amount": 0.0})
    fids = sorted(set(_fact_ids(res)) | ({calc["fact_id"]} if calc.get("fact_id") else set()) | {s[2] for s in sig})
    rationale = "%s: %s. %s%s" % (res["cause"], res["cause_label"], "Signals: " + "; ".join(s[1] for s in sig) + ". " if sig else "",
                                  "Amount %s from %s." % (fmt_money(amt), "calculate" if kind in ("reduction", "credit") else "project_exposure" if kind == "projected_exposure" else "the linked case"))
    return {"final": {"option": option, "label": label, "probable_cause": res["cause"], "cause_label": res["cause_label"],
                      "cause_quote": res.get("cause_quote") or "", "confidence": conf, "signals": [s[1] for s in sig], "amount": amt,
                      "amount_kind": kind, "amount_fact_id": calc.get("fact_id"),
                      "calculation": {k: v for k, v in calc.items() if k in ("action", "before", "after", "reduction", "credit", "bill_ids", "period",
                                                                         "days", "avg_kwh", "rate", "fixed_daily", "consumption", "fixed", "cycle", "lines",
                                                                         "fixed_retained", "period_end_after", "reads_used")},
                      "alternatives": alts, "s_codes": res["s_codes"],
                      "similar_cases": [{"id": h["id"], "text": h["text"], "score": h["score"]} for h in similar],
                      "rationale": rationale, "fact_ids": fids, "rule_set_version": res["rule_set_version"]}}


# ---- Execution: propose the plan (server validates it), then run approved steps one at a time ---------
@policy("agent.execution")
def execution(messages, tools, b):
    if b["mode"] == "plan":
        return {"final": {"option": b["option"], "steps": b["playbook"], "fact_ids": []}}
    done = tool_results(messages)
    for n, i, r in done:
        if n == "check_step" and not r.get("passed"):
            return {"final": {"status": "STEP_FAILED", "explanation": "Step %d (%s) failed its check: %s" % (r["seq"], r["step"], r["check_result"])}}
        if n in b["step_names"] and r.get("ok") is False:
            return {"final": {"status": "STEP_FAILED", "explanation": "Step %s failed: %s" % (n, r.get("error"))}}
    executed = {r.get("seq") for n, _, r in done if n != "check_step"}
    checked = {i["seq"] for n, i, _ in done if n == "check_step"}
    steps = [s for s in b["steps"] if s["status"] != "DONE"]
    for s in steps:
        if s["seq"] not in executed:
            return {"tool_calls": [call(s["step"], len(done) + 1, **s["params"])]}
        if s["seq"] not in checked:
            return {"tool_calls": [call("check_step", len(done) + 1, seq=s["seq"])]}
    return {"final": {"status": "DONE", "explanation": "All %d approved step(s) executed and checked." % len(steps)}}


# ---- Communications: outputs from the approved packet ----------------------------------------------------
@policy("agent.communications")
def communications(messages, tools, b):
    done = tool_results(messages)
    if not done:
        return {"tool_calls": [call("get_case_packet", 1)]}
    packet = done[0][2]
    out = O.render_all(packet)
    return {"final": dict(out, fact_ids=sorted(set(packet["cited_fact_ids"])))}
