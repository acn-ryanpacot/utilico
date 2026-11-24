"""Case intake without a timeline: the back-scan (as of the extract end, sent by the CSR) and the shared helpers used
by the CSR account lookup (as of the date Maya picks)."""
from . import backscan as BS
from .calculator import issued_credit
from .pit import disconnect_window, resolve_premise
from .rules import near_matches
from .util import AppError


class IntakeMixin:
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

    def open_case_for(self, account_id, wo_id):
        """The case for this account and root-cause disconnect - open or already decided (one case per disconnect)."""
        for c in self.state["cases"].values():
            if c["account_id"] == account_id and c["wo_id"] == wo_id and c["state"] != "NO_ISSUE":
                return c
        return None

    def issued_credit_bills(self, view, s):
        res = s["res"]
        return issued_credit(view, s["subtype"], res["sp_sas"], s["window"], res["meter_id"])["bill_ids"]

    # ---- back-scan (CSR sends findings into the workflow) -------------------------------------------------
    def backscan_candidates(self):
        with self.at(self.state["clock"]):
            rows = BS.backscan_rows(self.view())
        sent = set(self.state["backscan_sent"])
        for r in rows:
            r["sent"] = r["wo_id"] in sent
            r["case_id"] = next((c["case_id"] for c in self.state["cases"].values() if c["wo_id"] == r["wo_id"] and c["state"] != "NO_ISSUE"), None)
        return rows

    def send_backscan(self, user, wo_ids=None):
        if user["persona"] != "CSR":
            self.deny(user, "send back-scan findings to the workflow (SoD-3: CSR intake)")
        rows = {r["wo_id"]: r for r in self.backscan_candidates()}
        wanted = wo_ids or [w for w, r in rows.items() if r["eligible"] and not r["sent"]]
        for w in wanted:
            r = rows.get(w)
            if not r:
                raise AppError("No back-scan row for %s" % w)
            if not r["eligible"]:
                raise AppError("%s cannot be sent: %s" % (w, r["reason"]))
        created = []
        with self.at(self.state["clock"]):
            v = self.view()
            for w in wanted:
                if w in self.state["backscan_sent"] or rows[w]["case_id"]:
                    continue
                s = self.seed_for(v, v.wos[w], "REMEDIATE")
                if not s:
                    continue
                c = self.open_case(dict(s, bill_ids=list(rows[w]["bill_ids"]), draft_bill_id=None, origin_user=user["user_id"]),
                                   {"event_id": "BACKSCAN", "type": "BACKSCAN_SENT", "ref": w})
                self.state["backscan_sent"].append(w)
                created.append(c["case_id"])
            self.log(user["user_id"], "BACKSCAN_SENT_TO_WORKFLOW", "backscan", "extract", after={"sent": wanted, "cases": created},
                     reason="Back-scan findings sent to the workflow")
        self.save()
        return created
