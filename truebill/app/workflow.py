"""Case lifecycle (prompt §9, §11-§13): data-driven state machine (config/workflow.json), priority / SLA / triage,
the agent pipeline, human actions with segregation of duties, execution + verifier, outputs, sealing, autopilot."""
import asyncio
from datetime import datetime, timedelta, timezone

from . import outputs as O
from .agents.models import CommunicationsOut, ExecutionOut, PlanOut, ReconciliationOut, RecommendationOut, RetrievalOut
from .agents.playbooks import build_plan
from .agents.policies import LANES
from .agents.runner import ExecGuard, run_agent
from .agents.tools import STEPS, GuardrailError, ToolContext
from .calculator import draft_correction, issued_credit, project_exposure
from .datasource import F_MEMO, F_SA, F_WO
from .pit import View, resolve_premise
from .rules import fired, run_rules
from .util import D, AppError, PermissionDenied, add_days, f2, fmt_money, money, now_utc, sha256_text

PRIORITY = {("High", "High"): "P1", ("High", "Medium"): "P2", ("Medium", "High"): "P2", ("Medium", "Medium"): "P3",
            ("Low", "High"): "P3", ("Low", "Medium"): "P4"}
LEVELS = ["Low", "Medium", "High"]
ACTIONS = {
    "CONFIRM_MATCH": ("CSR", ["TRIAGE"]), "REJECT_MATCH": ("CSR", ["TRIAGE"]), "CONFIRM_TRIAGE": ("CSR", ["TRIAGE"]),
    "DECIDE": ("DRI", ["AWAITING_DRI"]), "FINANCE_APPROVE": ("FINANCE", ["PENDING_FINANCE"]), "FINANCE_REJECT": ("FINANCE", ["PENDING_FINANCE"]),
    "APPROVE_PLAN": ("DRI", ["PLAN_APPROVAL"]), "RETURN_PLAN": ("DRI", ["PLAN_APPROVAL"]),
    "EDIT_OUTPUT": ("DRI", ["OUTPUT_APPROVAL"]), "APPROVE_OUTPUTS": ("DRI", ["OUTPUT_APPROVAL"]),
    "RELEASE_BILL": ("CSR", ["READY_FOR_CSR"]), "SEND_CUSTOMER_SUMMARY": ("CSR", ["READY_FOR_CSR"]), "SEAL": ("DRI", ["RESOLVED"]),
}
SOD = {"DECIDE": "SoD-2", "APPROVE_PLAN": "SoD-2", "RETURN_PLAN": "SoD-2", "EDIT_OUTPUT": "SoD-2", "APPROVE_OUTPUTS": "SoD-2", "SEAL": "SoD-2",
       "FINANCE_APPROVE": "SoD-3", "FINANCE_REJECT": "SoD-3", "RELEASE_BILL": "SoD-4", "SEND_CUSTOMER_SUMMARY": "SoD-4"}
OPTION_LABEL = {"PRORATE": "Prorate", "SUPPRESS": "Suppress", "FLAG": "Flag", "RESPOND": "Respond", "RELEASE_AS_IS": "Release as-is"}


def real_hours_between(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600.0


class CaseMixin:
    # ---- rule contexts --------------------------------------------------------------------------------
    def rule_ctx_from_seed(self, s, bill_ids):
        res = s["res"]
        return {"wo_id": s["wo_id"], "subtype": s["subtype"], "recorded_premise": s["recorded_premise"],
                "premise": s["suggested_premise"] if s["match_status"] in ("PENDING", "CONFIRMED", "REJECTED") and s.get("suggested_premise") else s["recorded_premise"],
                "match_status": s["match_status"], "suggested_premise": s.get("suggested_premise"), "account_id": res["sa"]["account_id"],
                "sa_id": res["sa"]["sa_id"], "meter_id": res["meter_id"], "sp_sa_ids": [x["sa_id"] for x in res["sp_sas"]],
                "window": s["window"], "bill_ids": list(bill_ids), "case_type": s["type"]}

    def rule_ctx(self, case, view=None, verify=False):
        ctx = {k: case[k] for k in ("wo_id", "subtype", "recorded_premise", "suggested_premise", "account_id", "sa_id", "meter_id", "window")}
        ctx.update(premise=case["premise"], match_status=case["match"]["status"], sp_sa_ids=case["sp_sa_ids"], case_type=case["type"],
                   bill_ids=list(case["bill_ids"]))
        if verify and view is not None:
            wo = view.wos.get(case["wo_id"]) or {}
            if wo.get("oms_premise_id") in view.xw:
                ctx.update(recorded_premise=wo["oms_premise_id"], premise=wo["oms_premise_id"], match_status="NONE")
        return ctx

    def affected_run(self, case):
        runs = [b["bill_run_at"] for b in self.ds.table("ccb_bill_segments.csv") if b["bill_id"] in case["bill_ids"]]
        return min(runs) if runs else None

    # ---- open --------------------------------------------------------------------------------------------
    def open_case(self, seed, ev):
        st = self.state
        st["case_seq"] += 1
        n = st["case_seq"]
        res = seed["res"]
        v = self.view()
        acc = res["account"] or v.accounts.get(res["sa"]["account_id"], {})
        sa = res["sa"]
        case = {"case_id": "TB-%04d" % (1000 + n), "jira_id": "BILL-NEW-%d" % (1146 + n), "type": seed["type"], "state": "NEW", "returned": False,
                "opened_at": self.clock, "opened_real": now_utc(), "origin": {"event_id": ev.get("event_id"), "type": ev.get("type"), "ref": ev.get("ref")},
                "account_id": sa["account_id"], "customer_name": acc.get("customer_name", ""), "customer_class": acc.get("customer_class", ""),
                "sa_id": sa["sa_id"], "sp_sa_ids": [x["sa_id"] for x in res["sp_sas"]], "premise_address": sa.get("premise_address", ""),
                "meter_id": res["meter_id"], "wo_id": seed["wo_id"], "subtype": seed["subtype"], "recorded_premise": seed["recorded_premise"],
                "suggested_premise": seed.get("suggested_premise"), "premise": seed.get("suggested_premise") or seed["recorded_premise"],
                "match": {"status": seed["match_status"], "suggested": seed.get("suggested_premise"), "by": None, "at": None, "note": None},
                "window": seed["window"], "bill_ids": list(seed.get("bill_ids") or []), "draft_bill_id": seed.get("draft_bill_id"),
                "run_ts": seed.get("run_ts"), "non_blocking_period_end": seed.get("non_blocking_period_end"),
                "linked_case_id": seed.get("linked_case_id"), "dispute_case_ids": list(seed.get("dispute_case_ids") or []),
                "flags": {"dispute": False, "regulatory": False}, "history": [], "activity": [], "rule_set_version": self.cfg.rules()["rule_set_version"],
                "timings": {"event_real": st.get("last_event_real") or now_utc(), "new_real": now_utc()}, "plan": None, "decision": None,
                "finance": None, "verification": None, "outputs": None, "csr": {"released": False, "summary_sent": False}, "seal": None,
                "triage": None, "recommendation": None, "reconciliation": None, "retrieval": None, "agent_error": None, "edits": [], "reruns": 0,
                "sla": {}, "first_action_real": None}
        self.refresh_flags(case, v)
        amt, detail = self.compute_amount(case)
        case["amount"], case["amount_detail"] = amt, detail
        case["impact"], case["urgency"], case["priority"] = self.compute_priority(case)
        tg = self.cfg.sla()["targets_hours"][case["priority"]]
        o = datetime.fromisoformat(case["opened_real"])
        case["sla"] = {"first_action_due": (o + timedelta(hours=tg["first_action"])).isoformat(timespec="seconds"),
                       "resolve_due": (o + timedelta(hours=tg["resolve"])).isoformat(timespec="seconds"),
                       "first_action_hours": tg["first_action"], "resolve_hours": tg["resolve"]}
        st["cases"][case["case_id"]] = case
        case["history"].append({"from": None, "to": "NEW", "by": "agent.orchestrator", "scenario_time": self.clock, "at": now_utc()})
        self.log("agent.orchestrator", "CASE_CREATED", "case", case["case_id"], case["case_id"], None,
                 {"type": case["type"], "account_id": case["account_id"], "wo_id": case["wo_id"], "amount": case["amount"], "priority": case["priority"],
                  "impact": case["impact"], "urgency": case["urgency"], "assigned_dri": "u.dri", "jira_id": case["jira_id"], "origin": case["origin"]},
                 "Opened by %s %s; DRI u.dri assigned" % (ev.get("type"), ev.get("ref")))
        self.save()
        self.publish({"type": "case", "case_id": case["case_id"], "state": case["state"]})
        self.spawn(self.pipeline(case["case_id"]))
        return case

    def refresh_flags(self, case, v):
        opn = [c for c in v.crm_by_account.get(case["account_id"], []) if c["status"] != "CLOSED"]
        if case["dispute_case_ids"] or any(c["category"] in ("BILLING_DISPUTE", "REGULATORY_COMPLAINT") for c in opn):
            case["flags"]["dispute"] = True
        if any(c["category"] == "REGULATORY_COMPLAINT" for c in opn) or case["type"] == "REGULATORY_RESPONSE":
            case["flags"]["regulatory"] = True

    def compute_amount(self, case):
        base = self.view(sandbox=False)
        sa = base.sas.get(case["sa_id"])
        t = case["type"]
        if t == "PREVENT":
            corr = draft_correction(base, case["subtype"], sa, base.bills[case["draft_bill_id"]], case["window"], case["meter_id"],
                                    stop_date=case["window"]["disconnect_date"])
            return corr["reduction"], {"kind": "reduction", "before": corr["before"], "after": corr["after"], "action": corr["action"]}
        if t == "REMEDIATE":
            sp = [base.sas[x] for x in case["sp_sa_ids"] if x in base.sas]
            cr = issued_credit(base, case["subtype"], sp, case["window"], case["meter_id"])
            lines = [l for l in cr["lines"] if l["bill_id"] in case["bill_ids"]]
            return f2(sum((D(l["credit"]) for l in lines), D(0))), {"kind": "credit", "bill_ids": [l["bill_id"] for l in lines]}
        if t == "ADVANCE":
            p = project_exposure(base, sa, case["meter_id"], case["window"]["disconnect_date"], case.get("non_blocking_period_end"))
            return p["exposure"], {"kind": "projected_exposure", "days": p["days"]}
        link = self.state["cases"].get(case["linked_case_id"]) or {}
        return link.get("amount", 0.0), {"kind": "linked_case", "case_id": case["linked_case_id"]}

    def compute_priority(self, case):
        tc = self.cfg.triage()["priority"]
        a = D(case["amount"])
        flag = case["flags"]["dispute"] or case["flags"]["regulatory"]
        impact = "High" if a >= D(tc["impact_high_min"]) or flag else "Medium" if a >= D(tc["impact_medium_min"]) else "Low"
        bills = 0 if case["type"] == "ADVANCE" else len(case["bill_ids"])
        if bills >= tc["bills_modifier_min"]:
            impact = LEVELS[min(2, LEVELS.index(impact) + 1)]
        urgency = "High" if case["type"] in ("PREVENT", "REGULATORY_RESPONSE") or flag else "Medium"
        return impact, urgency, PRIORITY[(impact, urgency)]

    def sla_view(self, case, now=None):
        now = now or now_utc()
        cfg = self.cfg.sla()
        out = {}
        for k, hours_key, met in (("first_action", "first_action_hours", case.get("first_action_real")),
                                  ("resolve", "resolve_hours", (case.get("timings") or {}).get("resolved_real"))):
            due = case["sla"].get(k + "_due")
            if not due:
                continue
            total = case["sla"][hours_key]
            end = met or now
            el = real_hours_between(case["opened_real"], end)
            pct = el / total if total else 0
            status = "MET" if met and pct < cfg["breached_pct"] else "BREACHED" if pct >= cfg["breached_pct"] else "AT_RISK" if pct >= cfg["at_risk_pct"] else "ON_TRACK"
            out[k] = {"due": due, "pct": round(pct * 100, 1), "status": status, "remaining_hours": round(max(0.0, total - el), 2)}
        active = [v for k, v in out.items() if v["status"] != "MET"]
        out["worst"] = "BREACHED" if any(v["status"] == "BREACHED" for v in active) else "AT_RISK" if any(v["status"] == "AT_RISK" for v in active) else "ON_TRACK"
        if case["state"] in ("SEALED", "CANCELLED", "RESOLVED"):
            out["worst"] = "MET"
        return out

    def compute_triage(self, case):
        tc = self.cfg.triage()
        rec = case.get("recommendation") or {}
        a = D(case["amount"])
        flag = case["flags"]["dispute"] or case["flags"]["regulatory"]
        pb3 = "PB3" in (case.get("rules_fired") or [])
        reasons = []
        if a >= D(tc["red"]["amount_min"]):
            reasons.append("amount ≥ %s" % fmt_money(tc["red"]["amount_min"]))
        if flag:
            reasons.append("dispute / regulatory flag")
        if pb3:
            reasons.append("PB3 fired")
        if rec.get("option") == "FLAG":
            reasons.append("recommendation is Flag")
        if reasons:
            colour = "Red"
        elif a < D(tc["green"]["amount_max_exclusive"]) and rec.get("confidence", 0) >= tc["green"]["confidence_min"]:
            colour, reasons = "Green", ["amount < %s, confidence %.2f, no flags" % (fmt_money(tc["green"]["amount_max_exclusive"]), rec.get("confidence", 0))]
        else:
            colour, reasons = "Amber", ["not Red, not Green"]
        return {"colour": colour, "reasons": reasons, "would_auto_approve": colour == "Green", "agreed": None, "at": now_utc()}

    # ---- state machine ---------------------------------------------------------------------------------
    def transition(self, case, to, actor_id, reason=None, after=None, action=None):
        wf = self.cfg.workflow()
        persona = self.persona_of(actor_id)
        ok = any(t["from"] == case["state"] and t["to"] == to and (t["actor"] == actor_id or t["actor"] == persona) for t in wf["transitions"])
        if not ok:
            raise AppError("Transition %s → %s by %s is not in config/workflow.json" % (case["state"], to, actor_id))
        frm = case["state"]
        case["state"] = to
        if to == "AWAITING_DRI" and frm not in ("TRIAGE",):
            case["returned"] = True
        case["history"].append({"from": frm, "to": to, "by": actor_id, "scenario_time": self.clock, "at": now_utc(), "reason": reason})
        if to == "RESOLVED":
            case["timings"]["resolved_real"] = now_utc()
        if action:
            self.log(actor_id, action, "case", case["case_id"], case["case_id"], {"state": frm}, dict({"state": to}, **(after or {})), reason)
        self.save()
        self.publish({"type": "case", "case_id": case["case_id"], "state": to})

    def persona_of(self, actor_id):
        u = self.cfg.user(actor_id)
        return u["persona"] if u else None

    # ---- agent pipeline: RETRIEVING -> RECONCILING -> RECOMMENDING -> TRIAGE -----------------------------
    def brief(self, case, **extra):
        v = self.view()
        b = {k: case[k] for k in ("case_id", "type", "account_id", "sa_id", "sp_sa_ids", "meter_id", "wo_id", "subtype", "recorded_premise",
                                  "suggested_premise", "premise", "window", "bill_ids", "draft_bill_id", "linked_case_id")}
        b.update(match_status=case["match"]["status"], unmatched=case["recorded_premise"] not in v.xw,
                 issued_bill_ids=[x for x in case["bill_ids"] if (v.bills.get(x) or {}).get("_state") == "ISSUED"],
                 current_period=v.current_period(), reads_until=v.reads_until(), scenario_time=self.clock,
                 non_blocking_period_end=case.get("non_blocking_period_end"), segment=(case.get("customer_class") or "").title())
        b.update(extra)
        return b

    async def pipeline(self, case_id, rerun=False):
        case = self.state["cases"][case_id]
        try:
            if not rerun:
                self.transition(case, "RETRIEVING", "agent.orchestrator", after={"lanes": list(LANES)})
            started = now_utc()
            runs = await asyncio.gather(*[run_agent(self, case, "agent.retrieval", self.brief(case, lane=lane), self.lane_tools(lane), RetrievalOut, lane=lane)
                                          for lane in LANES])
            case["retrieval"] = {"started": started, "finished": now_utc(), "lanes": {r["lane"]: len(r["fact_ids"]) for r in runs},
                                 "fact_ids": sorted({f for r in runs for f in r["fact_ids"]})}
            self.refresh_flags(case, self.view())
            self.log("agent.retrieval", "RETRIEVAL_COMPLETED", "case", case_id, case_id, None,
                     {"lanes": case["retrieval"]["lanes"], "facts": len(case["retrieval"]["fact_ids"]), "parallel": True})
            self.transition(case, "RECONCILING", "agent.retrieval")
            recon = await run_agent(self, case, "agent.reconciliation", self.brief(case), ["run_rules"], ReconciliationOut)
            case["reconciliation"] = recon
            case["rules_fired"] = recon["fired"]
            if not rerun:
                case["rules_initial"] = list(recon["fired"])
            r4 = next((r for r in recon["explanations"] if r["rule"] == "R4"), None)
            recon["severity"] = "High" if r4 and "severity High" in r4["text"] else "Medium" if r4 else None
            case["timings"]["reconciled_real"] = now_utc()
            self.log("agent.reconciliation", "RECONCILIATION_COMPLETED", "case", case_id, case_id, None,
                     {"fired": recon["fired"], "explanations": len(recon["explanations"]), "rule_set_version": recon["rule_set_version"]})
            self.transition(case, "RECOMMENDING", "agent.reconciliation")
            rc = self.cfg.rules()["mock_policy"]
            facts = [{k: f[k] for k in ("fact_id", "file", "record_id", "kind", "fields")} for f in self.evidence.for_case(case_id)]
            link = self.state["cases"].get(case.get("linked_case_id")) or {}
            rec = await run_agent(self, case, "agent.recommendation",
                                  self.brief(case, facts=facts, affected_run=self.affected_run(case), signal_phrases=rc["crm_signal_phrases"],
                                             conf_base=rc["confidence_base"], conf_step=rc["confidence_step"], conf_cap=rc["confidence_cap"],
                                             pb3_days=rc["pb3_corroboration_days"], linked_amount=link.get("amount"),
                                             linked_confidence=(link.get("recommendation") or {}).get("confidence")),
                                  ["run_rules", "calculate", "project_exposure", "search_knowledge"], RecommendationOut)
            case["recommendation"] = rec
            if not rerun:
                case["option_initial"] = rec["option"]
                case["timings"]["recommended_real"] = now_utc()
            self.log("agent.recommendation", "RECOMMENDATION_COMPLETED", "case", case_id, case_id, None,
                     {"option": rec["option"], "label": rec["label"], "cause": rec["probable_cause"], "confidence": rec["confidence"], "amount": rec["amount"],
                      "s_codes": rec["s_codes"], "similar_cases": [s["id"] for s in rec["similar_cases"]],
                      "packet_seconds": round(real_hours_between(case["timings"]["new_real"], case["timings"]["recommended_real"]) * 3600, 3)})
            self.transition(case, "TRIAGE", "agent.recommendation")
        except GuardrailError as e:
            case["agent_error"] = str(e)
            self.log("agent.orchestrator", "AGENT_TOOL_CALL", "case", case_id, case_id, None, {"guardrail": str(e)}, "Guardrail stopped the agent")
            self.save()
            self.publish({"type": "case", "case_id": case_id, "state": case["state"]})

    def lane_tools(self, lane):
        return {"OMS": ["get_work_orders", "get_integration_messages", "find_similar_premises"],
                "CCB": ["get_service_agreement", "get_bills", "get_todos"], "MDM": ["get_meter", "get_daily_reads"],
                "CRM": ["get_crm_cases"], "GL": ["get_gl_lines", "get_gl_period"]}[lane]

    # ---- human actions -----------------------------------------------------------------------------------
    def allowed_actions(self, user, case):
        out = []
        for a, (persona, states) in ACTIONS.items():
            if user["persona"] != persona or case["state"] not in states:
                continue
            if a in ("CONFIRM_MATCH", "REJECT_MATCH") and case["match"]["status"] != "PENDING":
                continue
            if a == "CONFIRM_TRIAGE" and case["match"]["status"] == "PENDING":
                continue
            if a == "APPROVE_PLAN" and not case.get("plan"):
                continue
            if a == "RELEASE_BILL" and (case["type"] != "PREVENT" or case["csr"]["released"]):
                continue
            if a == "SEND_CUSTOMER_SUMMARY" and case["csr"]["summary_sent"]:
                continue
            out.append(a)
        return out

    def act(self, user, case_id, action, p, autopilot=False):
        case = self.state["cases"].get(case_id)
        if not case:
            raise AppError("Unknown case %s" % case_id)
        if action not in ACTIONS:
            raise AppError("Unknown action %s" % action)
        persona, states = ACTIONS[action]
        if user["persona"] != persona:
            self.deny(user, "%s (%s: only %s may)" % (action, SOD.get(action, "permission"), persona), case_id)
        if case["state"] not in states:
            raise AppError("%s is not allowed while the case is %s" % (action, case["state"]))
        reason = (p.get("reason") or "").strip()
        uid = user["user_id"]
        self._autopilot_flag = autopilot
        try:
            if not case.get("first_action_real"):
                case["first_action_real"] = now_utc()
            getattr(self, "_a_" + action.lower())(user, case, p, reason)
        finally:
            self._autopilot_flag = False
        self.save()
        self.publish({"type": "case", "case_id": case_id, "state": case["state"]})
        return case

    def need_reason(self, reason, what):
        if not reason:
            raise AppError("A written reason is required for %s (SoD-5)." % what)

    def _a_confirm_match(self, user, case, p, reason):
        note = (p.get("note") or reason or "").strip() or "Evidence reviewed"
        case["match"].update(status="CONFIRMED", by=user["user_id"], at=now_utc(), note=note)
        self.log(user["user_id"], "MATCH_CONFIRMED", "premise", case["recorded_premise"], case["case_id"], {"premise": case["recorded_premise"]},
                 {"premise": case["suggested_premise"]}, note)
        case["reruns"] += 1
        self.transition(case, "RETRIEVING", user["user_id"], reason="ID match confirmed - re-running retrieval, reconciliation and recommendation once")
        self.spawn(self.pipeline(case["case_id"], rerun=True))

    def _a_reject_match(self, user, case, p, reason):
        self.need_reason(reason, "rejecting an ID match")
        case["match"].update(status="REJECTED", by=user["user_id"], at=now_utc(), note=reason)
        self.log(user["user_id"], "MATCH_REJECTED", "premise", case["recorded_premise"], case["case_id"], None, {"premise": case["suggested_premise"]}, reason)

    def _a_confirm_triage(self, user, case, p, reason):
        case["triage_confirm"] = {"by": user["user_id"], "at": now_utc(), "note": (p.get("note") or reason or None)}
        case["triage"] = self.compute_triage(case)
        self.transition(case, "AWAITING_DRI", user["user_id"], reason or None, {"triage": case["triage"]["colour"]}, action="TRIAGE_CONFIRMED")

    def _a_decide(self, user, case, p, reason):
        rec = case.get("recommendation") or {}
        kind = p.get("kind") or "APPROVE_RECOMMENDED"
        if kind == "CANCEL_FALSE_POSITIVE":
            self.need_reason(reason, "cancelling as a false positive")
            case["decision"] = {"kind": kind, "option": None, "by": user["user_id"], "at": now_utc(), "reason": reason}
            self.transition(case, "CANCELLED", user["user_id"], reason, {"closure": "CANCELLED_FALSE_POSITIVE"}, action="CASE_CANCELLED")
            return
        if kind == "APPROVE_RECOMMENDED":
            if rec.get("option") == "FLAG":
                raise AppError("A Flag recommendation cannot be approved as an action - choose Prorate, Suppress or Release as-is.")
            option = rec.get("option")
        elif kind == "CHOOSE_OTHER":
            self.need_reason(reason, "choosing a non-recommended option")
            option = p.get("option")
            if option not in ("PRORATE", "SUPPRESS"):
                raise AppError("Choose Prorate or Suppress")
            if case["match"]["status"] in ("PENDING", "REJECTED"):
                raise AppError("The ID match is not confirmed - only Release as-is or cancel are possible")
        elif kind == "RELEASE_AS_IS":
            self.need_reason(reason, "releasing as-is (override)")
            option = "RELEASE_AS_IS"
        else:
            raise AppError("Unknown decision %s" % kind)
        case["decision"] = {"kind": kind, "option": option, "label": OPTION_LABEL.get(option, option) if kind != "APPROVE_RECOMMENDED" else rec.get("label"),
                            "by": user["user_id"], "at": now_utc(), "scenario_time": self.clock, "reason": reason or None,
                            "recommended": rec.get("option"), "override": kind != "APPROVE_RECOMMENDED"}
        if case.get("triage") and case["triage"]["would_auto_approve"]:
            case["triage"]["agreed"] = kind == "APPROVE_RECOMMENDED"
        action = "DECISION_RECORDED" if kind == "APPROVE_RECOMMENDED" else "OVERRIDE_RECORDED"
        after = {"option": option, "kind": kind, "label": case["decision"]["label"]}
        if option in ("RELEASE_AS_IS", "RESPOND"):
            self.transition(case, "DRAFTING_OUTPUTS", user["user_id"], reason or None, after, action=action)
            self.spawn(self.draft_outputs(case["case_id"]))
        elif case["type"] == "REMEDIATE":
            self.transition(case, "PENDING_FINANCE", user["user_id"], reason or None, after, action=action)
        else:
            case["plan"] = None
            self.transition(case, "PLAN_APPROVAL", user["user_id"], reason or None, after, action=action)
            self.spawn(self.propose_plan(case["case_id"]))

    def _a_finance_approve(self, user, case, p, reason):
        v = self.view()
        per = v.current_period()
        case["finance"] = {"outcome": "APPROVED", "by": user["user_id"], "at": now_utc(), "amount": case["amount"], "period": per, "note": reason or None}
        case["plan"] = None
        self.transition(case, "PLAN_APPROVAL", user["user_id"], reason or None, {"amount": case["amount"], "period": per}, action="FINANCE_APPROVED")
        self.spawn(self.propose_plan(case["case_id"]))

    def _a_finance_reject(self, user, case, p, reason):
        self.need_reason(reason, "a Finance rejection")
        case["finance"] = {"outcome": "REJECTED", "by": user["user_id"], "at": now_utc(), "note": reason}
        self.transition(case, "AWAITING_DRI", user["user_id"], reason, {"returned": True}, action="FINANCE_REJECTED")

    def _a_approve_plan(self, user, case, p, reason):
        case["plan"]["approved_by"], case["plan"]["approved_at"] = user["user_id"], now_utc()
        self.transition(case, "EXECUTING", user["user_id"], reason or None, {"steps": [s["step"] for s in case["plan"]["steps"]]}, action="PLAN_APPROVED")
        self.spawn(self.execute(case["case_id"]))

    def _a_return_plan(self, user, case, p, reason):
        self.need_reason(reason, "sending a plan back")
        self.transition(case, "AWAITING_DRI", user["user_id"], reason, {"returned": True}, action="DECISION_RECORDED")

    def _a_edit_output(self, user, case, p, reason):
        self.need_reason(reason, "editing an output")
        name, text = p.get("name"), p.get("text")
        if name not in (case["outputs"] or {}).get("files", {}) or text is None:
            raise AppError("Unknown output %s" % name)
        f = case["outputs"]["files"][name]
        before = f["sha256"]
        self.write_output(case, name, text)
        case["edits"].append({"name": name, "by": user["user_id"], "at": now_utc(), "reason": reason, "before_sha256": before, "after_sha256": case["outputs"]["files"][name]["sha256"]})
        self.log(user["user_id"], "OUTPUT_EDITED", "output", "%s/%s" % (case["case_id"], name), case["case_id"], {"sha256": before},
                 {"sha256": case["outputs"]["files"][name]["sha256"]}, reason)
        self.check_outputs(case)

    def _a_approve_outputs(self, user, case, p, reason):
        cc = case["outputs"]["consistency"]
        if not cc["pass"]:
            raise AppError("Consistency check failed - fix the outputs first: %s" % "; ".join(cc["issues"][:3]))
        case["outputs"]["approved_by"], case["outputs"]["approved_at"] = user["user_id"], now_utc()
        self.transition(case, "READY_FOR_CSR", user["user_id"], reason or None,
                        {"outputs": [n for n, f in case["outputs"]["files"].items()]}, action="OUTPUTS_APPROVED")

    def _a_release_bill(self, user, case, p, reason):
        v = self.view()
        b = v.bills.get(case["draft_bill_id"]) or {}
        before = money(b.get("original_total") or b.get("total_amount"))
        after = money(b.get("total_amount"))
        entry = self.state["bills"].setdefault(case["draft_bill_id"], {})
        entry.update(status="RELEASED" if b.get("bill_status") != "CANCELLED" else "SUPPRESSED", released_amount=f2(after), case_id=case["case_id"])
        if case["draft_bill_id"] in self.state["unissued"] and b.get("bill_status") != "CANCELLED":
            self.state["unissued"].remove(case["draft_bill_id"])
        case["csr"]["released"] = True
        if before > after:
            self.credit_prevented(case["case_id"], case["draft_bill_id"], before - after, "released corrected" if after > 0 else "draft cancelled")
        self.log(user["user_id"], "BILL_RELEASED", "bill", case["draft_bill_id"], case["case_id"], {"total": f2(before)},
                 {"total": f2(after), "status": b.get("bill_status")}, reason or None)
        self._maybe_resolve(user, case)

    def _a_send_customer_summary(self, user, case, p, reason):
        case["csr"]["summary_sent"] = True
        name = "regulator_response" if case["type"] == "REGULATORY_RESPONSE" else "customer_summary"
        self.log(user["user_id"], "CUSTOMER_SUMMARY_SENT", "output", "%s/%s" % (case["case_id"], name), case["case_id"], None,
                 {"output": name, "sha256": case["outputs"]["files"][name]["sha256"]}, reason or None)
        self._maybe_resolve(user, case)

    def _maybe_resolve(self, user, case):
        if case["csr"]["summary_sent"] and (case["type"] != "PREVENT" or case["csr"]["released"]):
            self.transition(case, "RESOLVED", user["user_id"])

    def _a_seal(self, user, case, p, reason):
        h = self.case_hash(case)
        case["seal"] = dict(h, by=user["user_id"], at=now_utc())
        self.transition(case, "SEALED", user["user_id"], reason or None, None)
        self.log(user["user_id"], "CASE_SEALED", "case", case["case_id"], case["case_id"], None,
                 {"case_hash": h["case_hash"], "audit_events": h["audit_events"], "facts": h["facts"], "outputs": h["outputs"]})
        rec = case.get("recommendation") or {}
        self.knowledge.add_case({"id": case["case_id"], "closed": self.clock[:10], "segment": (case.get("customer_class") or "").title() or "-",
                                 "pattern": "%s case: %s" % (case["type"], ", ".join(case.get("rules_fired") or [])), "cause": rec.get("probable_cause", "-"),
                                 "resolution": (case.get("decision") or {}).get("label") or "-", "amount": case["amount"], "manual_days": None,
                                 "source": "sealed"})

    def case_hash(self, case, upto_event=None):
        evs = []
        for e in self.engine.audit.events:
            if upto_event and e["event_id"] == upto_event:
                break
            if e.get("case_id") == case["case_id"] and e.get("scenario_id") == self.sc_id and e.get("dataset_id") == self.ds.id:
                evs.append(e["hash"])
        facts = [f["hash"] for f in self.evidence.for_case(case["case_id"])]
        outs = [case["outputs"]["files"][n]["sha256"] for n in sorted((case.get("outputs") or {}).get("files", {}))]
        return {"case_hash": sha256_text("".join(evs + facts + outs)), "audit_events": len(evs), "facts": len(facts), "outputs": len(outs)}

    def deny(self, user, what, case_id=None):
        self.log(user["user_id"], "PERMISSION_DENIED", "case" if case_id else "api", case_id or what, case_id, None, {"attempted": what})
        raise PermissionDenied("%s may not %s" % (user["persona"], what))

    # ---- execution agent: plan, then approved steps only --------------------------------------------------
    async def propose_plan(self, case_id):
        case = self.state["cases"][case_id]
        await self.latency()
        expected = build_plan(case, self.view())
        try:
            out = await run_agent(self, case, "agent.execution", {"mode": "plan", "option": case["decision"]["option"], "playbook": expected},
                                  [], PlanOut)
        except GuardrailError as e:
            case["agent_error"] = str(e)
            self.save()
            return
        got = [(s["step"], s["params"]) for s in out["steps"]]
        if got != [(s["step"], s["params"]) for s in expected]:
            case["agent_error"] = "Plan rejected: it does not match the playbook for %s" % case["decision"]["option"]
            self.log("agent.execution", "PLAN_PROPOSED", "plan", case_id, case_id, None, {"valid": False}, case["agent_error"])
            self.save()
            return
        case["plan"] = {"option": case["decision"]["option"], "steps": [dict(s, status="PENDING", result=None, check_result=None) for s in out["steps"]],
                        "proposed_at": now_utc(), "approved_by": None, "validated": True}
        self.log("agent.execution", "PLAN_PROPOSED", "plan", case_id, case_id, None,
                 {"option": case["plan"]["option"], "steps": [s["step"] for s in case["plan"]["steps"]], "validated_against_playbook": True})
        self.save()
        self.publish({"type": "case", "case_id": case_id, "state": case["state"]})
        self.publish({"type": "session"})

    def _on_step(self, ctx, name, args, st, guard):
        case = ctx.case
        if name == "check_step":
            ok, text = STEPS[st["step"]][1](ctx, st["params"])
            if self.state["toggles"]["inject_failure"]:
                self.state["toggles"]["inject_failure"] = False
                ok, text = False, "Injected failure (demo): %s check did not pass - the sandbox did not confirm the change" % st["step"]
            st["status"], st["check_result"] = ("DONE" if ok else "FAILED"), text
            guard.awaiting_check = None
            guard.next = next((s["seq"] for s in guard.plan["steps"] if s["status"] not in ("DONE",) and s["seq"] > st["seq"]), None)
            if not ok:
                self.log("agent.execution", "STEP_FAILED", "step", "%s#%d" % (case["case_id"], st["seq"]), case["case_id"], None,
                         {"step": st["step"], "params": st["params"]}, text)
            self.save()
            self.publish({"type": "case", "case_id": case["case_id"], "state": case["state"]})
            return {"seq": st["seq"], "step": st["step"], "passed": ok, "check_result": text}, text, ok
        summary = STEPS[name][0](ctx, args)
        st["status"], st["result"] = "EXECUTED", summary
        guard.awaiting_check = st["seq"]
        self.log("agent.execution", "STEP_EXECUTED", "step", "%s#%d" % (case["case_id"], st["seq"]), case["case_id"], None,
                 {"step": name, "params": args, "result": summary})
        self.save()
        return {"seq": st["seq"], "step": name, "ok": True, "result": summary}, summary, True

    async def execute(self, case_id):
        case = self.state["cases"][case_id]
        plan = case["plan"]
        for st in plan["steps"]:
            if st["status"] == "DONE":
                continue
            st["status"] = "PENDING"
            guard = ExecGuard(plan)
            try:
                out = await run_agent(self, case, "agent.execution",
                                      {"mode": "execute", "steps": [st], "step_names": [st["step"]]}, [st["step"], "check_step"], ExecutionOut,
                                      guard=guard, on_step=self._on_step)
            except GuardrailError as e:
                out = {"status": "STEP_FAILED", "explanation": str(e)}
                st["status"], st["check_result"] = "FAILED", str(e)
                self.log("agent.execution", "STEP_FAILED", "step", "%s#%d" % (case_id, st["seq"]), case_id, None, {"step": st["step"]}, str(e))
            if out["status"] == "STEP_FAILED" or st["status"] != "DONE":
                case["execution"] = {"status": "STEP_FAILED", "explanation": out["explanation"], "at": now_utc()}
                self.transition(case, "AWAITING_DRI", "agent.execution", out["explanation"], {"returned": True})
                return
        case["execution"] = {"status": "DONE", "at": now_utc()}
        self.transition(case, "VERIFYING", "agent.execution")
        await self.verify(case_id)

    async def verify(self, case_id):
        case = self.state["cases"][case_id]
        await self.latency()
        v = self.view()
        ctx = self.rule_ctx(case, v, verify=True)
        res = run_rules(v, ctx, self.cfg.rules())
        f = fired(res)
        # PB1-PB3 must be cleared; R1/R2 on a draft must be cleared; on issued bills (REMEDIATE) they are remediated
        # by the credit memo, which must equal `calculate`
        problems = [r for r in ("PB1", "PB2", "PB3") if r in f]
        if case["type"] in ("PREVENT", "ADVANCE"):
            problems += [r for r in ("R1", "R2") if r in f]
        detail = ""
        if case["type"] == "REMEDIATE" and case["decision"]["option"] != "RELEASE_AS_IS":
            memo = v.memos.get("CM-%s" % case_id) or {}
            calc = (case.get("recommendation") or {}).get("amount")
            ok_memo = bool(memo) and money(memo.get("amount")) == money(calc)
            detail = " Credit memo %s %s = calculated %s: %s." % (memo.get("memo_id"), memo.get("amount"), calc, "yes" if ok_memo else "no")
            if not ok_memo:
                problems.append("CREDIT")
        if case["type"] == "PREVENT":
            b = v.bills.get(case["draft_bill_id"]) or {}
            detail += " Draft %s now %s %s." % (case["draft_bill_id"], b.get("bill_status"), b.get("total_amount"))
        passed = not problems
        case["verification"] = {"passed": passed, "fired_after": f, "problems": problems, "detail": ("Re-ran R1, R2, PB1-PB3 on the sandboxed state: %s." % (
            "nothing blocking" if passed else "still firing " + ", ".join(problems))) + detail, "at": now_utc()}
        self.log("sys.verifier", "VERIFICATION_PASSED" if passed else "VERIFICATION_FAILED", "case", case_id, case_id, None,
                 {"fired_after": f, "problems": problems}, case["verification"]["detail"])
        if passed:
            self.transition(case, "DRAFTING_OUTPUTS", "sys.verifier")
            await self.draft_outputs(case_id)
        else:
            self.transition(case, "AWAITING_DRI", "sys.verifier", case["verification"]["detail"], {"returned": True})

    # ---- communications ------------------------------------------------------------------------------------
    async def draft_outputs(self, case_id):
        case = self.state["cases"][case_id]
        try:
            out = await run_agent(self, case, "agent.communications", self.brief(case), ["get_case_packet"], CommunicationsOut)
        except GuardrailError as e:
            case["agent_error"] = str(e)
            self.save()
            return
        case["outputs"] = {"files": {}, "approved_by": None, "consistency": None, "citations": None}
        for name in ("customer_summary", "executive_brief", "jira_ticket", "regulator_response"):
            if out.get(name):
                self.write_output(case, name, out[name])
        self.log("agent.communications", "OUTPUTS_DRAFTED", "case", case_id, case_id, None,
                 {"outputs": sorted(case["outputs"]["files"]), "jira_id": case["jira_id"] if "jira_ticket" in case["outputs"]["files"] else None})
        self.check_outputs(case)
        self.transition(case, "OUTPUT_APPROVAL", "agent.communications")

    def write_output(self, case, name, text):
        path = self.write_output_file(case["case_id"], name, text)
        case["outputs"]["files"][name] = {"text": text, "sha256": sha256_text(text), "path": path, "label": O.NAMES[name]}

    def check_outputs(self, case):
        texts = {n: f["text"] for n, f in case["outputs"]["files"].items()}
        packet = case.get("packet_snapshot") or self.packet(case)
        cc = O.consistency(texts, packet)
        cs = O.citation_stats(texts, set(self.evidence.by_id))
        case["outputs"]["consistency"], case["outputs"]["citations"] = cc, cs
        self.log("agent.communications", "CONSISTENCY_CHECK", "case", case["case_id"], case["case_id"], None,
                 {"pass": cc["pass"], "issues": cc["issues"][:5], "citation_coverage": round(cs["coverage"], 4), "factual_sentences": cs["factual"]})

    # ---- the approved packet (input of the Communications agent) -------------------------------------------
    def packet(self, case):
        v = self.view()
        ev = self.evidence
        ctx = ToolContext(self, case, "agent.communications")

        def fid(record_id, kind="record"):
            for f in ev.for_case(case["case_id"]):
                if f["record_id"] == record_id and f.get("kind", "record") == kind:
                    return f["fact_id"]
            return None

        rec = case.get("recommendation") or {}
        calc = rec.get("calculation") or {}
        msg = next((f for f in ev.for_case(case["case_id"]) if f["file"] == "integration_message_log.csv" and f["fields"].get("wo_id") == case["wo_id"]), None)
        todos = [{"todo_id": f["record_id"], "status": f["fields"].get("status"), "fact_id": f["fact_id"]} for f in ev.for_case(case["case_id"])
                 if f["file"] == "ccb_todo_entries.csv" and f["fields"].get("related_entity") == case["wo_id"]]
        base = self.view(sandbox=False)
        bills = []
        if case["type"] == "PREVENT":
            b = base.bills.get(case["draft_bill_id"]) or {}
            bills.append({"bill_id": case["draft_bill_id"], "period_start": b.get("period_start"), "period_end": b.get("period_end"),
                          "before": calc.get("before"), "after": calc.get("after"), "action": calc.get("action"),
                          "post_days": len(_post_days(b, case["window"])), "fact_id": fid(case["draft_bill_id"])})
        elif case["type"] in ("REMEDIATE", "REGULATORY_RESPONSE"):
            lines = calc.get("lines") or (self.state["cases"].get(case.get("linked_case_id"), {}).get("recommendation") or {}).get("calculation", {}).get("lines") or []
            for l in lines:
                b = base.bills.get(l["bill_id"]) or {}
                bills.append({"bill_id": l["bill_id"], "period_start": b.get("period_start"), "period_end": b.get("period_end"), "credit": l["credit"],
                              "post_days": l["overlap_days"], "fact_id": fid(l["bill_id"]) or ctx.ensure_fact(l["bill_id"])})
        nxt = add_days(self.clock[:10], 3)
        nfact, _ = ev.add(case["case_id"], "CCB", "ccb_accounts.csv", case["account_id"], {"next_update_date": nxt, "basis": "scenario date + 3 days"},
                          nxt, self.clock, "agent.communications", kind="commitment")
        rules_cfg = self.cfg.rules()
        names = dict(rules_cfg["reconciliation"], **rules_cfg["prebill"])
        rules = [{"rule": e["rule"], "name": names[e["rule"]]["name"], "text": e["text"] + O.c(*e["fact_ids"]),
                  "severity": (case.get("reconciliation") or {}).get("severity")} for e in (case.get("reconciliation") or {}).get("explanations", [])]
        reg = None
        for c in v.crm_by_account.get(case["account_id"], []):
            if c["category"] == "REGULATORY_COMPLAINT" and c["status"] != "CLOSED":
                reg = {"case_id": c["case_id"], "opened_at": c["opened_at"], "subject": c["subject"], "fact_id": fid(c["case_id"]) or ctx.ensure_fact(c["case_id"])}
        if case["type"] != "REGULATORY_RESPONSE" and not case["flags"]["regulatory"]:
            reg = None
        facts = {"account": fid(case["account_id"]), "sa": fid(case["sa_id"]), "wo": fid(case["wo_id"]), "msg": msg["fact_id"] if msg else None,
                 "calc": rec.get("amount_fact_id") if rec.get("amount_kind") in ("reduction", "credit") else None,
                 "projection": rec.get("amount_fact_id") if rec.get("amount_kind") == "projected_exposure" else None,
                 "period": fid(v.current_period() or ""), "next_update": nfact["fact_id"]}
        if case["type"] == "REGULATORY_RESPONSE":
            link = self.state["cases"].get(case["linked_case_id"]) or {}
            lrec = link.get("recommendation") or {}
            facts["calc"] = lrec.get("amount_fact_id")
        chronology, remediation = self._chronology(case, v, fid, ctx)
        p = {"case_id": case["case_id"], "type": case["type"], "jira_id": case["jira_id"], "account_id": case["account_id"],
             "customer_name": case["customer_name"], "customer_class": case["customer_class"], "premise_address": case["premise_address"],
             "sa_id": case["sa_id"], "meter_id": case["meter_id"], "wo_id": case["wo_id"], "subtype": case["subtype"],
             "disconnect_date": case["window"]["disconnect_date"], "window": case["window"], "msg_id": msg["record_id"] if msg else None,
             "msg_status": msg["fields"].get("status") if msg else None, "todos": todos, "bills": bills, "amount": case["amount"],
             "credit_period": (case.get("finance") or {}).get("period") or v.current_period(), "priority": case["priority"],
             "triage": (case.get("triage") or {}).get("colour", "-"), "confidence": rec.get("confidence", 0.0), "cause": rec.get("probable_cause", "-"),
             "cause_label": rec.get("cause_label", ""), "cause_quote": rec.get("cause_quote", ""), "s_codes": rec.get("s_codes", []),
             "s_code_names": rules_cfg["s_codes"], "rules": rules, "facts": facts, "next_update": nxt, "regulatory_case": reg,
             "chronology": chronology, "remediation": remediation, "dri_name": (self.cfg.user("u.dri") or {}).get("name", "DRI"),
             "rule_set_version": case["rule_set_version"], "decision": case.get("decision"),
             "cause_fact_ids": [x for x in [msg["fact_id"] if msg else None] + [t["fact_id"] for t in todos] if x]}
        p["cited_fact_ids"] = sorted({x for x in _walk_ids(p) if x})
        p["allowed"] = _allowed_tokens(p)
        case["packet_snapshot"] = p
        return p

    def _chronology(self, case, v, fid, ctx):
        """Chronology + remediation lines for the regulator response (cites the sealed packet for a linked case)."""
        src = self.state["cases"].get(case.get("linked_case_id")) if case["type"] == "REGULATORY_RESPONSE" else case
        sid = src["case_id"]
        ev = [f for f in self.evidence.for_case(sid)]

        def f_of(file, pred):
            return next((f for f in ev if f["file"] == file and f.get("kind", "record") == "record" and pred(f)), None)

        out = []
        wo = f_of("oms_work_orders.csv", lambda f: f["record_id"] == src["wo_id"])
        if wo:
            out.append({"date": wo["fields"]["completed_at"][:10], "text": "Supply disconnected, work order %s (%s)." % (src["wo_id"], src["subtype"]), "fact_ids": [wo["fact_id"]]})
        msg = f_of("integration_message_log.csv", lambda f: f["fields"].get("wo_id") == src["wo_id"])
        if msg:
            out.append({"date": msg["fields"]["received_at"][:10], "text": "Integration message %s %s." % (msg["record_id"], msg["fields"].get("status")), "fact_ids": [msg["fact_id"]]})
        for f in ev:
            if f["file"] == "ccb_todo_entries.csv" and f["fields"].get("related_entity") == src["wo_id"]:
                out.append({"date": f["fields"]["created_at"][:10], "text": "To Do %s raised and left %s." % (f["record_id"], (f["fields"].get("status") or "").lower()), "fact_ids": [f["fact_id"]]})
        for f in ev:
            if f["file"] == "crm_cases.csv" and f["fields"].get("opened_at", "")[:10] >= src["window"]["disconnect_date"]:
                out.append({"date": f["fields"]["opened_at"][:10], "text": "CRM case %s (%s): %s." % (f["record_id"], f["fields"].get("category"), f["fields"].get("subject")), "fact_ids": [f["fact_id"]]})
        for b in src["bill_ids"]:
            bf = f_of("ccb_bill_segments.csv", lambda f, b=b: f["record_id"] == b)
            if bf:
                out.append({"date": bf["fields"]["bill_run_at"][:10], "text": "Bill %s issued for %s." % (b, bf["fields"].get("period_start", "") + " to " + bf["fields"].get("period_end", "")), "fact_ids": [bf["fact_id"]]})
        if case["type"] == "REGULATORY_RESPONSE":
            for cid in case["dispute_case_ids"]:
                fx = fid(cid) or ctx.ensure_fact(cid)
                c = v.crm.get(cid) or {}
                out.append({"date": (c.get("opened_at") or self.clock)[:10], "text": "Regulatory complaint %s received." % cid, "fact_ids": [fx]})
        out.sort(key=lambda x: x["date"])
        rem = []
        memo = v.memos.get("CM-%s" % sid)
        if memo:
            mf = ctx.ensure_fact(memo["memo_id"])
            rem.append({"text": "Credit memo %s for %s posted to period %s." % (memo["memo_id"], fmt_money(memo["amount"]), memo["accounting_period"]), "fact_ids": [mf]})
        sa = v.sas.get(src["sa_id"]) or {}
        if sa.get("sa_status") == "STOPPED" and sa.get("_overlay"):
            sf, _ = self.evidence.add(case["case_id"], "CCB", F_SA, sa["sa_id"], {"sa_status": "STOPPED", "stop_date": sa.get("stop_date"), "overlay": sa.get("_overlay")},
                                      "STOPPED", self.clock, "agent.communications", kind="corrected")
            rem.append({"text": "Account %s stopped with effect from %s." % (sa["sa_id"], sa.get("stop_date")), "fact_ids": [sf["fact_id"]]})
        if src.get("seal") and case["type"] == "REGULATORY_RESPONSE":
            wof = wo["fact_id"] if wo else None
            rem.append({"text": "Case %s was sealed with hash %s..." % (sid, src["seal"]["case_hash"][:12]), "fact_ids": [x for x in [wof] if x]})
        return out, rem

    # ---- autopilot -----------------------------------------------------------------------------------------
    def autopilot_step(self):
        """One recommended human action, or None. Order: oldest case first."""
        for case in sorted(self.state["cases"].values(), key=lambda c: c["case_id"]):
            s = case["state"]
            try:
                if s == "TRIAGE":
                    if case["match"]["status"] == "PENDING":
                        return self.act(self.cfg.user("u.csr"), case["case_id"], "CONFIRM_MATCH",
                                        {"note": "Autopilot: near-match meter evidence reviewed"}, autopilot=True)
                    return self.act(self.cfg.user("u.csr"), case["case_id"], "CONFIRM_TRIAGE", {"note": "Autopilot: evidence and customer contact confirmed"}, autopilot=True)
                if s == "AWAITING_DRI":
                    if case["returned"]:
                        continue            # a failed step / verification / rejection needs a person, not the autopilot
                    rec = case.get("recommendation") or {}
                    if rec.get("option") == "FLAG":
                        return self.act(self.cfg.user("u.dri"), case["case_id"], "DECIDE", {"kind": "RELEASE_AS_IS", "reason": "Autopilot: ID match not confirmed"}, autopilot=True)
                    return self.act(self.cfg.user("u.dri"), case["case_id"], "DECIDE", {"kind": "APPROVE_RECOMMENDED"}, autopilot=True)
                if s == "PENDING_FINANCE":
                    return self.act(self.cfg.user("u.finance"), case["case_id"], "FINANCE_APPROVE", {}, autopilot=True)
                if s == "PLAN_APPROVAL" and case.get("plan"):
                    return self.act(self.cfg.user("u.dri"), case["case_id"], "APPROVE_PLAN", {}, autopilot=True)
                if s == "OUTPUT_APPROVAL" and case["outputs"] and case["outputs"]["consistency"]["pass"]:
                    return self.act(self.cfg.user("u.dri"), case["case_id"], "APPROVE_OUTPUTS", {}, autopilot=True)
                if s == "READY_FOR_CSR":
                    if case["type"] == "PREVENT" and not case["csr"]["released"]:
                        return self.act(self.cfg.user("u.csr"), case["case_id"], "RELEASE_BILL", {}, autopilot=True)
                    return self.act(self.cfg.user("u.csr"), case["case_id"], "SEND_CUSTOMER_SUMMARY", {}, autopilot=True)
                if s == "RESOLVED":
                    return self.act(self.cfg.user("u.dri"), case["case_id"], "SEAL", {}, autopilot=True)
            except AppError:
                continue
        return None


def _post_days(b, win):
    from .rules import post_days
    return post_days(b, win) if b else []


def _walk_ids(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("fact_id",) and isinstance(v, str):
                yield v
            elif k == "fact_ids" and isinstance(v, list):
                yield from v
            elif k == "facts" and isinstance(v, dict):
                yield from (x for x in v.values() if isinstance(x, str))
            else:
                yield from _walk_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_ids(v)
    elif isinstance(obj, str):
        yield from O.CITE_ANY.findall(obj)


def _allowed_tokens(p):
    acc = {"amounts": set(), "dates": set(), "bill_ids": set(), "account_ids": {p["account_id"]}}

    def walk(o, key=None):
        if isinstance(o, dict):
            for k, v in o.items():
                if k not in ("allowed",):
                    walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, bool) or o is None:
            return
        elif isinstance(o, (int, float)):
            if key in ("amount", "before", "after", "credit", "reduction"):
                acc["amounts"].add(fmt_money(o))
        elif isinstance(o, str):
            acc["amounts"].update(O.MONEY.findall(o))
            acc["dates"].update(O.DATE.findall(o))
            acc["bill_ids"].update(O.BILL.findall(o))
    walk(p)
    return {k: sorted(v) for k, v in acc.items()}
