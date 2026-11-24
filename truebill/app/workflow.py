"""Case lifecycle: data-driven state machine (config/workflow.json), priority / SLA / triage, the agent pipeline,
human actions with segregation of duties, the CSR account lookup, sealing and the autopilot.

The process ends at the DRI's decision: APPROVED (recommended or another option) or REJECTED (release as-is,
cancel as false positive). The decision seals the case. There is no execution, Finance step or output drafting.
"""
import asyncio
from datetime import datetime, timedelta

from .agents.models import ReconciliationOut, RecommendationOut, RetrievalOut
from .agents.policies import LANES
from .agents.runner import run_agent
from .agents.tools import GuardrailError
from .calculator import draft_correction, issued_credit, project_exposure
from .pit import STOP_SUBTYPES, resolve_premise
from .rules import near_matches
from .util import D, AppError, PermissionDenied, f2, fmt_money, now_utc, sha256_text

PRIORITY = {("High", "High"): "P1", ("High", "Medium"): "P2", ("Medium", "High"): "P2", ("Medium", "Medium"): "P3",
            ("Low", "High"): "P3", ("Low", "Medium"): "P4"}
LEVELS = ["Low", "Medium", "High"]
ACTIONS = {"CONFIRM_MATCH": ("CSR", ["TRIAGE"]), "REJECT_MATCH": ("CSR", ["TRIAGE"]), "CONFIRM_TRIAGE": ("CSR", ["TRIAGE"]),
           "DECIDE": ("DRI", ["AWAITING_DRI"])}
SOD = {"DECIDE": "SoD-2", "CONFIRM_MATCH": "SoD-3", "REJECT_MATCH": "SoD-3", "CONFIRM_TRIAGE": "SoD-3"}
OPTION_LABEL = {"PRORATE": "Prorate", "SUPPRESS": "Suppress", "FLAG": "Flag", "RESPOND": "Respond", "RELEASE_AS_IS": "Release as-is"}
DECIDED = ("APPROVED", "REJECTED")
TERMINAL = DECIDED + ("NO_ISSUE",)


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
        return ctx

    def affected_run(self, case):
        runs = [b["bill_run_at"] for b in self.ds.table("ccb_bill_segments.csv") if b["bill_id"] in case["bill_ids"]]
        return min(runs) if runs else None

    # ---- open --------------------------------------------------------------------------------------------
    def open_case(self, seed, ev, lookup=False):
        st = self.state
        if lookup:
            st["lookup_seq"] = st.get("lookup_seq", 0) + 1
            case_id = "LK-%04d" % st["lookup_seq"]
            jira = None
        else:
            st["case_seq"] += 1
            case_id = "TB-%04d" % (1000 + st["case_seq"])
            jira = "BILL-NEW-%d" % (1146 + st["case_seq"])
        res = seed["res"]
        v = self.view()
        acc = res["account"] or v.accounts.get(res["sa"]["account_id"], {})
        sa = res["sa"]
        case = {"case_id": case_id, "jira_id": jira, "type": seed["type"], "state": "NEW", "returned": False, "lookup": lookup,
                "opened_at": self.clock, "opened_real": now_utc(),
                "origin": {"event_id": ev.get("event_id"), "type": ev.get("type"), "ref": ev.get("ref"), "by": seed.get("origin_user")},
                "account_id": sa["account_id"], "customer_name": acc.get("customer_name", ""), "customer_class": acc.get("customer_class", ""),
                "sa_id": sa["sa_id"], "sp_sa_ids": [x["sa_id"] for x in res["sp_sas"]], "premise_address": sa.get("premise_address", ""),
                "meter_id": res["meter_id"], "wo_id": seed["wo_id"], "subtype": seed["subtype"], "recorded_premise": seed["recorded_premise"],
                "suggested_premise": seed.get("suggested_premise"), "premise": seed.get("suggested_premise") or seed["recorded_premise"],
                "match": {"status": seed["match_status"], "suggested": seed.get("suggested_premise"), "by": None, "at": None, "note": None},
                "window": seed["window"], "bill_ids": list(seed.get("bill_ids") or []), "draft_bill_id": seed.get("draft_bill_id"),
                "run_ts": seed.get("run_ts"), "non_blocking_period_end": seed.get("non_blocking_period_end"),
                "linked_case_id": seed.get("linked_case_id"), "dispute_case_ids": list(seed.get("dispute_case_ids") or []),
                "flags": {"dispute": False, "regulatory": False}, "history": [], "activity": [], "rule_set_version": self.cfg.rules()["rule_set_version"],
                "timings": {"event_real": st.get("last_event_real") or now_utc(), "new_real": now_utc()}, "decision": None, "seal": None,
                "triage": None, "recommendation": None, "reconciliation": None, "retrieval": None, "agent_error": None, "reruns": 0,
                "attached_bills": [], "sla": {}, "first_action_real": None}
        if lookup:
            case["timings"]["event_real"] = now_utc()
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
        self.log("agent.orchestrator", "LOOKUP_REQUESTED" if lookup else "CASE_CREATED", "case", case_id, case_id, None,
                 {"type": case["type"], "account_id": case["account_id"], "wo_id": case["wo_id"], "amount": case["amount"], "priority": case["priority"],
                  "impact": case["impact"], "urgency": case["urgency"], "assigned_dri": "u.dri", "jira_id": jira, "origin": case["origin"]},
                 ("Account lookup by %s" % seed.get("origin_user")) if lookup else "Opened by %s %s; DRI u.dri assigned" % (ev.get("type"), ev.get("ref")))
        self.save()
        self.publish({"type": "case", "case_id": case_id, "state": case["state"]})
        self.spawn(self.pipeline(case_id))
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
                                  ("resolve", "resolve_hours", (case.get("timings") or {}).get("decided_real"))):
            due = case["sla"].get(k + "_due")
            if not due:
                continue
            total = case["sla"][hours_key]
            el = real_hours_between(case["opened_real"], met or now)
            pct = el / total if total else 0
            status = "MET" if met and pct < cfg["breached_pct"] else "BREACHED" if pct >= cfg["breached_pct"] else "AT_RISK" if pct >= cfg["at_risk_pct"] else "ON_TRACK"
            out[k] = {"due": due, "pct": round(pct * 100, 1), "status": status, "remaining_hours": round(max(0.0, total - el), 2)}
        active = [v for k, v in out.items() if v["status"] != "MET"]
        out["worst"] = "BREACHED" if any(v["status"] == "BREACHED" for v in active) else "AT_RISK" if any(v["status"] == "AT_RISK" for v in active) else "ON_TRACK"
        if case["state"] in TERMINAL:
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
        case["history"].append({"from": frm, "to": to, "by": actor_id, "scenario_time": self.clock, "at": now_utc(), "reason": reason})
        if to in TERMINAL:
            case["timings"]["decided_real"] = now_utc()
        if action:
            self.log(actor_id, action, "case", case["case_id"], case["case_id"], {"state": frm}, dict({"state": to}, **(after or {})), reason)
        self.save()
        self.publish({"type": "case", "case_id": case["case_id"], "state": to})

    def persona_of(self, actor_id):
        u = self.cfg.user(actor_id)
        return u["persona"] if u else None

    # ---- agent pipeline: RETRIEVING -> RECONCILING -> RECOMMENDING -> TRIAGE (or NO_ISSUE for a lookup) -------
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
            if case.get("lookup") and not recon["fired"]:
                self.transition(case, "NO_ISSUE", "agent.reconciliation", "No rule fired for this account at %s - no case opened" % self.clock,
                                {"fired": []}, action="LOOKUP_NO_ISSUE")
                return
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
                      "packet_seconds": round(real_hours_between(case["timings"]["new_real"], case["timings"].get("recommended_real") or now_utc()) * 3600, 3)})
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

    # ---- CSR account lookup ------------------------------------------------------------------------------
    def lookup(self, user, account_id):
        """Maya enters an account number: the agents retrieve and reconcile it at the current scenario time.
        Findings -> a case (LK-...) that goes to triage and the DRI; nothing fired -> NO_ISSUE, no case."""
        if user["persona"] != "CSR":
            self.deny(user, "look up an account (SoD-3: CSR intake)")
        account_id = (account_id or "").strip()
        v = self.view()
        if account_id not in v.accounts:
            self.log(user["user_id"], "LOOKUP_REQUESTED", "account", account_id or "-", None, None, {"found": False})
            raise AppError("Account %s was not found at scenario time %s" % (account_id or "(blank)", self.clock))
        # completed disconnects for the account: on its premises, plus unmatched OMS premises one character away
        prems = {p for sa in v.sas_by_account.get(account_id, []) for p in v.premises_by_sp.get(sa["ccb_sp_id"], [])}
        wos = [w for w in v.completed_disconnects() if w["oms_premise_id"] in prems or
               (w["oms_premise_id"] not in v.xw and any(c["premise"] in prems for c in near_matches(v, w["oms_premise_id"])))]
        if not wos:
            self.log(user["user_id"], "LOOKUP_NO_ISSUE", "account", account_id, None, None, {"disconnects": 0},
                     "No completed disconnect for this account at %s - no case opened" % self.clock)
            return {"status": "NO_DISCONNECT", "account_id": account_id, "customer_name": v.accounts[account_id]["customer_name"],
                    "message": "No completed disconnect for this account at scenario time %s - nothing to check, no case opened." % self.clock}
        wo = sorted(wos, key=lambda w: w["completed_at"])[-1]
        hit = next((c for c in self.state["cases"].values() if c["account_id"] == account_id and c["wo_id"] == wo["wo_id"] and c["state"] != "NO_ISSUE"), None)
        if hit:
            return {"status": "EXISTING", "case_id": hit["case_id"], "account_id": account_id, "customer_name": hit["customer_name"],
                    "message": "This disconnect is already handled in %s (%s)." % (hit["case_id"], hit["state"])}
        s = self.seed_for(v, wo, "REMEDIATE", origin_user=user["user_id"])
        bills = self.issued_credit_bills(v, s)
        s["type"] = "REMEDIATE" if bills else "ADVANCE"
        c = self.open_case(dict(s, bill_ids=bills, draft_bill_id=None), {"event_id": "LOOKUP", "type": "CSR_LOOKUP", "ref": account_id}, lookup=True)
        return {"status": "STARTED", "case_id": c["case_id"], "account_id": account_id, "customer_name": c["customer_name"],
                "message": "Agents are retrieving and reconciling %s (%s, disconnect %s)." % (account_id, c["customer_name"], wo["wo_id"])}

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
            raise AppError("A written reason is required for %s (SoD-4)." % what)

    def _a_confirm_match(self, user, case, p, reason):
        note = (p.get("note") or reason or "").strip() or "Evidence reviewed"
        case["match"].update(status="CONFIRMED", by=user["user_id"], at=now_utc(), note=note)
        self.log(user["user_id"], "MATCH_CONFIRMED", "premise", case["recorded_premise"], case["case_id"], {"premise": case["recorded_premise"]},
                 {"premise": case["suggested_premise"]}, note)
        case["reruns"] += 1
        self.transition(case, "RETRIEVING", user["user_id"], reason="ID match confirmed - re-running retrieval, reconciliation and recommendation once")
        self.spawn(self.pipeline(case["case_id"], rerun=True))

    def _a_reject_match(self, user, case, p, reason):
        reason = reason or (p.get("note") or "").strip()
        self.need_reason(reason, "rejecting an ID match")
        case["match"].update(status="REJECTED", by=user["user_id"], at=now_utc(), note=reason)
        self.log(user["user_id"], "MATCH_REJECTED", "premise", case["recorded_premise"], case["case_id"], None, {"premise": case["suggested_premise"]}, reason)

    def _a_confirm_triage(self, user, case, p, reason):
        case["triage_confirm"] = {"by": user["user_id"], "at": now_utc(), "note": (p.get("note") or reason or None)}
        case["triage"] = self.compute_triage(case)
        self.transition(case, "AWAITING_DRI", user["user_id"], reason or None, {"triage": case["triage"]["colour"]}, action="TRIAGE_CONFIRMED")

    def _a_decide(self, user, case, p, reason):
        """The single, final decision. APPROVED = approve the recommendation or choose another option;
        REJECTED = release as-is or cancel as a false positive. The case is sealed immediately."""
        rec = case.get("recommendation") or {}
        kind = p.get("kind") or "APPROVE_RECOMMENDED"
        if kind == "APPROVE_RECOMMENDED":
            if rec.get("option") == "FLAG":
                raise AppError("A Flag recommendation cannot be approved as an action - choose Prorate, Suppress or Release as-is.")
            option, outcome, action = rec.get("option"), "APPROVED", "DECISION_RECORDED"
        elif kind == "CHOOSE_OTHER":
            self.need_reason(reason, "choosing a non-recommended option")
            option = p.get("option")
            if option not in ("PRORATE", "SUPPRESS"):
                raise AppError("Choose Prorate or Suppress")
            if case["match"]["status"] in ("PENDING", "REJECTED"):
                raise AppError("The ID match is not confirmed - only Release as-is or cancel are possible")
            outcome, action = "APPROVED", "OVERRIDE_RECORDED"
        elif kind == "RELEASE_AS_IS":
            self.need_reason(reason, "releasing as-is (override)")
            option, outcome, action = "RELEASE_AS_IS", "REJECTED", "OVERRIDE_RECORDED"
        elif kind == "CANCEL_FALSE_POSITIVE":
            self.need_reason(reason, "cancelling as a false positive")
            option, outcome, action = None, "REJECTED", "CASE_CANCELLED"
        else:
            raise AppError("Unknown decision %s" % kind)
        label = rec.get("label") if kind == "APPROVE_RECOMMENDED" else OPTION_LABEL.get(option, "Cancelled as false positive")
        case["decision"] = {"kind": kind, "option": option, "label": label, "outcome": outcome, "by": user["user_id"], "at": now_utc(),
                            "scenario_time": self.clock, "reason": reason or None, "recommended": rec.get("option"),
                            "override": kind != "APPROVE_RECOMMENDED", "amount": case["amount"] if outcome == "APPROVED" else 0.0}
        if case.get("triage") and case["triage"]["would_auto_approve"]:
            case["triage"]["agreed"] = kind == "APPROVE_RECOMMENDED"
        if outcome == "APPROVED":
            kinds = {"PREVENT": "prevented", "REMEDIATE": "remediated", "ADVANCE": "exposure_addressed"}
            if case["type"] in kinds:
                self.state.setdefault("approved", []).append({"case_id": case["case_id"], "kind": kinds[case["type"]], "amount": case["amount"],
                                                              "scenario_time": self.clock})
        self.transition(case, outcome, user["user_id"], reason or None,
                        {"option": option, "kind": kind, "label": label, "outcome": outcome, "amount": case["decision"]["amount"]}, action=action)
        h = self.case_hash(case)
        case["seal"] = dict(h, by=user["user_id"], at=now_utc())
        self.log(user["user_id"], "CASE_SEALED", "case", case["case_id"], case["case_id"], None,
                 {"case_hash": h["case_hash"], "audit_events": h["audit_events"], "facts": h["facts"], "outcome": outcome})
        self.knowledge.add_case({"id": case["case_id"], "closed": self.clock[:10], "segment": (case.get("customer_class") or "").title() or "-",
                                 "pattern": "%s case: %s" % (case["type"], ", ".join(case.get("rules_fired") or [])), "cause": rec.get("probable_cause", "-"),
                                 "resolution": "%s - %s" % (outcome.title(), label or "-"), "amount": case["amount"], "manual_days": None, "source": "decided"})

    def case_hash(self, case, upto_event=None):
        evs = []
        for e in self.engine.audit.events:
            if upto_event and e["event_id"] == upto_event:
                break
            if e.get("case_id") == case["case_id"] and e.get("scenario_id") == self.sc_id and e.get("dataset_id") == self.ds.id:
                evs.append(e["hash"])
        facts = [f["hash"] for f in self.evidence.for_case(case["case_id"])]
        return {"case_hash": sha256_text("".join(evs + facts)), "audit_events": len(evs), "facts": len(facts), "outputs": 0}

    def deny(self, user, what, case_id=None):
        self.log(user["user_id"], "PERMISSION_DENIED", "case" if case_id else "api", case_id or what, case_id, None, {"attempted": what})
        raise PermissionDenied("%s may not %s" % (user["persona"], what))

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
                    rec = case.get("recommendation") or {}
                    if rec.get("option") == "FLAG":
                        return self.act(self.cfg.user("u.dri"), case["case_id"], "DECIDE", {"kind": "RELEASE_AS_IS", "reason": "Autopilot: ID match not confirmed"}, autopilot=True)
                    return self.act(self.cfg.user("u.dri"), case["case_id"], "DECIDE", {"kind": "APPROVE_RECOMMENDED"}, autopilot=True)
            except AppError:
                continue
        return None
