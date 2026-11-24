"""Pre-bill check and bill-run simulator (sys.prebill), disconnect follow-ups, disputes and back-scan intake
(prompt §6 freshness, §10)."""
from . import backscan as BS
from .datasource import F_BILL
from .pit import STOP_SUBTYPES, View, disconnect_window, resolve_premise
from .rules import near_matches, run_rules
from .util import D, f2, ts_add


class PrebillMixin:
    # ---- dispatch ----------------------------------------------------------------------------------
    async def process_event(self, ev):
        if ev["type"] == "BILL_READY" and self.state["toggles"]["stale_mdm"]:
            return self.block_stale(ev)
        self.log("agent.orchestrator", "EVENT_RECEIVED", "event", ev["event_id"], after={"type": ev["type"], "ref": ev["ref"], "at": ev["at"]})
        self.state["last_event_real"] = self.real_now()
        ev["status"] = "DONE"
        if ev["type"] == "BILL_READY":
            self.state["banner"] = None
            await self.bill_run(ev)
        elif ev["type"] == "DISCONNECT_COMPLETED":
            self.on_disconnect(ev)
        elif ev["type"] == "DISCONNECT_FOLLOWUP_DUE":
            self.on_followup(ev)
        elif ev["type"] == "DISPUTE_OPENED":
            self.on_dispute(ev)
        self.save()
        self.publish({"type": "session"})

    def block_stale(self, ev):
        fr = self.freshness()
        if ev["status"] != "BLOCKED":
            ev["status"] = "BLOCKED"
            self.state["banner"] = {"kind": "stale", "text": "Bill run %s blocked: MDM feed is %d min behind (max %d). No drafts were evaluated." % (
                ev["at"], fr["lags"]["MDM"], fr["max_lag_minutes"])}
            self.log("sys.prebill", "BILL_RUN_BLOCKED_STALE_DATA", "bill_run", ev["ref"], after={"lags": fr["lags"], "max_lag_minutes": fr["max_lag_minutes"]})
        self.save()
        self.publish({"type": "session"})

    def freshness(self):
        fcfg = self.cfg.freshness()
        lags = {c: 0 for c in fcfg["connectors"]}
        if self.state["toggles"]["stale_mdm"]:
            lags["MDM"] = fcfg["stale_demo_minutes"]
        return {"lags": lags, "max_lag_minutes": fcfg["max_lag_minutes"], "ok": all(v <= fcfg["max_lag_minutes"] for v in lags.values())}

    # ---- seeds ---------------------------------------------------------------------------------------
    def seed_for(self, view, wo, ctype, **kw):
        """Resolve a disconnect to account / SA / meter (recorded premise, or PB3 near match pending confirmation)."""
        dd = wo["completed_at"][:10]
        res = resolve_premise(view, wo["oms_premise_id"], dd)
        match, suggested = "NONE", None
        if not res:
            cands = near_matches(view, wo["oms_premise_id"])
            if not cands:
                return None
            suggested = cands[0]["premise"]
            res = resolve_premise(view, suggested, dd)
            if not res:
                return None
            match = "PENDING"
        win = disconnect_window(view, wo, wo["oms_premise_id"])
        return dict({"type": ctype, "wo_id": wo["wo_id"], "subtype": wo["wo_subtype"], "recorded_premise": wo["oms_premise_id"],
                     "suggested_premise": suggested, "match_status": match, "res": res, "window": win}, **kw)

    def open_case_for(self, key_account, wo_id):
        """The case for this account and root-cause disconnect - open or already decided (one case per disconnect)."""
        for c in self.state["cases"].values():
            if c["account_id"] == key_account and c["wo_id"] == wo_id and c["state"] != "NO_ISSUE":
                return c
        return None

    # ---- BILL_READY ----------------------------------------------------------------------------------
    async def bill_run(self, ev):
        """Pre-bill checks on every draft of the run. Nothing is corrected automatically any more (the process ends
        at the DRI's decision), so a draft whose disconnect already has a case - open or decided - is held and
        attached to that case instead of opening a second one."""
        run_ts = ev["ref"]
        drafts = sorted([b for b in self.ds.table(F_BILL) if b["bill_run_at"] == run_ts], key=lambda b: b["bill_id"])
        ids = {b["bill_id"] for b in drafts}
        self.log("sys.prebill", "BILL_RUN_STARTED", "bill_run", run_ts, after={"drafts": len(drafts)})
        unissued = set(self.state["unissued"]) | ids
        counts = {"held": 0, "passed": 0, "held_on_existing_case": 0}
        v = self.view(drafts=unissued)
        unmatched = [w for w in v.completed_disconnects() if w["oms_premise_id"] not in v.xw]
        new_cases = []
        for b in drafts:
            sa = v.sas.get(b["sa_id"])
            if not sa:
                continue
            ctxs = []
            for prem in v.premises_by_sp.get(sa["ccb_sp_id"], []):
                for wo in v.wos_by_premise.get(prem, []):
                    if wo["wo_type"] == "DISCONNECT" and wo["status"] == "COMPLETED" and wo.get("completed_at") and wo["completed_at"][:10] >= sa["start_date"]:
                        s = self.seed_for(v, wo, "PREVENT")
                        if s:
                            ctxs.append(s)
                for wo in unmatched:
                    if any(c["premise"] == prem for c in near_matches(v, wo["oms_premise_id"])) and b["period_end"] > wo["completed_at"][:10]:
                        s = self.seed_for(v, wo, "PREVENT")
                        if s:
                            ctxs.append(s)
            outcome, results = "PASSED", []
            for s in ctxs:
                res = run_rules(v, self.rule_ctx_from_seed(s, [b["bill_id"]]), self.cfg.rules())
                f = [r for r in ("R1", "R2", "R3", "R4", "PB1", "PB2", "PB3") if res[r]["fired"]]
                blocking = [r for r in f if r in self.cfg.rules()["blocking_rules"]]
                results.append((s, f, blocking))
                if blocking:
                    outcome = "HELD"
            rules = sorted({r for _, f, _ in results for r in f})
            entry = {"run_ts": run_ts, "before": f2(D(b["total_amount"])), "outcome": outcome, "rules": rules, "account_id": sa["account_id"],
                     "status": "HELD" if outcome == "HELD" else "PASSED"}
            self.state["bills"][b["bill_id"]] = entry
            if outcome == "HELD":
                counts["held"] += 1
                self.state["unissued"].append(b["bill_id"])
                self.log("sys.prebill", "BILL_HELD", "bill", b["bill_id"], None, None, {"rules": rules, "total": entry["before"]})
                for s, f, blocking in results:
                    if not blocking:
                        continue
                    hit = self.open_case_for(s["res"]["sa"]["account_id"], s["wo_id"])
                    if hit:
                        self.attach_bill(hit, b["bill_id"])
                        entry["case_id"], entry["attached"] = hit["case_id"], True
                        counts["held_on_existing_case"] += 1
                        continue
                    c = self.open_case(dict(s, bill_ids=[b["bill_id"]], draft_bill_id=b["bill_id"], run_ts=run_ts), ev)
                    entry["case_id"] = c["case_id"]
                    new_cases.append(c)
            else:
                counts["passed"] += 1
                self.log("sys.prebill", "BILL_PASSED", "bill", b["bill_id"], None, None, {"rules": rules, "total": entry["before"]})
                for s, f, blocking in results:
                    if not f or not ({"PB1", "PB2"} & set(f)):
                        continue
                    if self.open_case_for(s["res"]["sa"]["account_id"], s["wo_id"]):
                        continue
                    c = self.open_case(dict(s, type="ADVANCE", bill_ids=[], draft_bill_id=None, run_ts=run_ts,
                                            non_blocking_period_end=b["period_end"], passed_bill_id=b["bill_id"]), ev)
                    entry["case_id"] = c["case_id"]
                    new_cases.append(c)
        self.state["runs"].append({"run_ts": run_ts, "status": "COMPLETED", "counts": counts, "at_real": self.real_now()})
        self.log("sys.prebill", "BILL_RUN_COMPLETED", "bill_run", run_ts, after=dict(counts, cases=[c["case_id"] for c in new_cases]))
        ev["case_id"] = ", ".join(c["case_id"] for c in new_cases) or None

    def credit_prevented(self, case_id, bill_id, amount, how):
        self.state["prevented"].append({"case_id": case_id, "bill_id": bill_id, "amount": f2(amount), "how": how, "scenario_time": self.clock})

    def attach_bill(self, case, bill_id):
        """A decided (sealed) case stays read-only: the held bill is recorded next to it, not inside its packet."""
        decided = case["state"] in ("APPROVED", "REJECTED")
        target = case.setdefault("attached_bills", []) if decided else case["bill_ids"]
        if bill_id not in target:
            target.append(bill_id)
            self.log("agent.orchestrator", "EVENT_RECEIVED", "bill", bill_id, None, after={"attached_to": case["case_id"], "case_state": case["state"]},
                     reason="One case per account and root-cause disconnect: held bill attached to %s %s" % (
                         "decided case" if decided else "open case", case["case_id"]))

    # ---- DISCONNECT_COMPLETED / FOLLOWUP_DUE ---------------------------------------------------------
    def on_disconnect(self, ev):
        v = self.view()
        wo = v.wos.get(ev["ref"])
        hours = self.cfg.rules()["followup_hours"]
        if wo and self.state["toggles"]["followup"] and wo["wo_subtype"] in STOP_SUBTYPES:
            due = ts_add(wo["completed_at"], hours=hours)
            from .events import SCENARIOS
            if due <= SCENARIOS[self.sc_id]["end"]:
                self.schedule("DISCONNECT_FOLLOWUP_DUE", due, wo["wo_id"], "Follow-up %s (+%dh)" % (wo["wo_id"], hours))
                self.log("agent.orchestrator", "EVENT_RECEIVED", "work_order", wo["wo_id"], after={"followup_due": due},
                         reason="Disconnect follow-up scheduled (S3 preview)")

    def on_followup(self, ev):
        if not self.state["toggles"]["followup"]:
            ev["status"] = "IGNORED"
            return
        v = self.view()
        wo = v.wos.get(ev["ref"])
        if not wo:
            return
        s = self.seed_for(v, wo, "ADVANCE")
        if not s:
            return
        if self.open_case_for(s["res"]["sa"]["account_id"], wo["wo_id"]):
            return
        if s["match_status"] == "PENDING":
            c = self.open_case(dict(s, bill_ids=[], draft_bill_id=None), ev)
        elif s["res"]["sa"]["sa_status"] == "ACTIVE":
            c = self.open_case(dict(s, bill_ids=[], draft_bill_id=None), ev)
        else:
            return
        ev["case_id"] = c["case_id"]

    # ---- DISPUTE_OPENED --------------------------------------------------------------------------------
    def on_dispute(self, ev):
        v = self.view()
        crm = v.crm.get(ev["ref"])
        if not crm:
            return
        acc = crm["account_id"]
        open_cases = [c for c in self.state["cases"].values() if c["account_id"] == acc and c["state"] not in ("APPROVED", "REJECTED", "NO_ISSUE")]
        if open_cases:
            c = open_cases[0]
            c["flags"]["dispute"] = True
            if crm["category"] == "REGULATORY_COMPLAINT":
                c["flags"]["regulatory"] = True
            c["dispute_case_ids"].append(crm["case_id"])
            self.log("agent.orchestrator", "EVENT_RECEIVED", "crm_case", crm["case_id"], c["case_id"], after={"attached_to": c["case_id"], "dispute_flag": True})
            ev["case_id"] = c["case_id"]
            return
        sealed = [c for c in self.state["cases"].values() if c["account_id"] == acc and c["state"] in ("APPROVED", "REJECTED")]
        if crm["category"] == "REGULATORY_COMPLAINT" and sealed:
            link = sorted(sealed, key=lambda c: c["case_id"])[-1]
            seed = {"type": "REGULATORY_RESPONSE", "wo_id": link["wo_id"], "subtype": link["subtype"], "recorded_premise": link["recorded_premise"],
                    "suggested_premise": link.get("suggested_premise"), "match_status": "NONE" if not link.get("suggested_premise") else "CONFIRMED",
                    "res": resolve_premise(v, link["premise"], link["window"]["disconnect_date"]), "window": link["window"],
                    "bill_ids": list(link["bill_ids"]), "draft_bill_id": None, "linked_case_id": link["case_id"], "dispute_case_ids": [crm["case_id"]]}
            c = self.open_case(seed, ev)
            ev["case_id"] = c["case_id"]
            return
        # REMEDIATE for the disputed bill(s): the account's latest completed disconnect before the dispute
        wos = []
        for sa in v.sas_by_account.get(acc, []):
            for prem in v.premises_by_sp.get(sa["ccb_sp_id"], []):
                wos += [w for w in v.wos_by_premise.get(prem, []) if w["wo_type"] == "DISCONNECT" and w["status"] == "COMPLETED" and w.get("completed_at")]
        if not wos:
            return
        wo = sorted(wos, key=lambda w: w["completed_at"])[-1]
        s = self.seed_for(v, wo, "REMEDIATE", dispute_case_ids=[crm["case_id"]])
        bills = self.issued_credit_bills(v, s)
        if not bills:
            return
        c = self.open_case(dict(s, bill_ids=bills, draft_bill_id=None), ev)
        ev["case_id"] = c["case_id"]

    def issued_credit_bills(self, v, s):
        from .calculator import issued_credit
        res = s["res"]
        cr = issued_credit(View(self.ds, self.clock, None, set(self.state["unissued"])), s["subtype"], res["sp_sas"], s["window"], res["meter_id"])
        return cr["bill_ids"]

    # ---- BACKSCAN intake (CSR sends findings into the workflow) -------------------------------------------
    def backscan_candidates(self):
        v = self.view(sandbox=False)
        sent = set(self.state["backscan_sent"])
        rows = BS.backscan_rows(v)
        for r in rows:
            r["sent"] = r["wo_id"] in sent
            r["case_id"] = next((c["case_id"] for c in self.state["cases"].values() if c["wo_id"] == r["wo_id"]), None)
        return rows

    def send_backscan(self, user, wo_ids=None):
        from .events import SCENARIOS
        if self.sc_id != "BACKSCAN_2026-11-10":
            raise_error("Switch to the BACKSCAN_2026-11-10 scenario to send findings to the workflow.")
        rows = {r["wo_id"]: r for r in self.backscan_candidates()}
        wanted = wo_ids or [w for w, r in rows.items() if r["eligible"] and not r["sent"]]
        for w in wanted:
            r = rows.get(w)
            if not r:
                raise_error("No back-scan row for %s" % w)
            if not r["eligible"]:
                raise_error("%s cannot be sent: %s" % (w, r["reason"]))
        v = self.view()
        created = []
        ev = {"event_id": "BACKSCAN", "type": "BACKSCAN_SENT", "at": self.clock}
        for w in wanted:
            if w in self.state["backscan_sent"]:
                continue
            wo = v.wos[w]
            s = self.seed_for(v, wo, "REMEDIATE")
            if not s:
                continue
            c = self.open_case(dict(s, bill_ids=list(rows[w]["bill_ids"]), draft_bill_id=None, origin_user=user["user_id"]), ev)
            self.state["backscan_sent"].append(w)
            created.append(c["case_id"])
        self.log(user["user_id"], "EVENT_RECEIVED", "backscan", self.sc_id, after={"sent": wanted, "cases": created}, reason="Back-scan findings sent to the workflow")
        self.save()
        return created


def raise_error(msg):
    from .util import AppError
    raise AppError(msg)
