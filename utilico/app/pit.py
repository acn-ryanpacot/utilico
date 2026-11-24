"""Point-in-time view (prompt §6). All gate, agent and rule reads go through View.

Given scenario time T it hides anything that did not exist yet, then layers the scenario's sandbox overlays on top:
row overlays (replace or add a row) and read rules (zero + CORRECTED for reads as they become visible).
"""
import threading

from .datasource import (F_ACC, F_BILL, F_CRM, F_GL, F_KEY, F_MEMO, F_MSG, F_MTR, F_NOTE, F_PER, F_READ, F_SA, F_TAR,
                         F_TODO, F_WO, F_XW, KEY_FIELD)
from .util import D, add_days

STOP_SUBTYPES = ("STOP_SERVICE", "MOVE_OUT")
_CACHE, _LOCK = {}, threading.Lock()


def _point_in_time(ds, T):
    Td = T[:10] if T else None
    pit = T is not None
    g = ds.table
    t = {F_ACC: [a for a in g(F_ACC) if not pit or not a.get("created_date") or a["created_date"] <= Td],
         F_TAR: list(g(F_TAR)), F_XW: list(g(F_XW)), F_KEY: list(g(F_KEY)), F_MEMO: [], F_NOTE: []}
    sas = []
    for s in g(F_SA):
        if pit and s["start_date"] > Td:
            continue
        if pit and s.get("stop_processed_at") and s["stop_processed_at"] > T:
            s = dict(s, sa_status="ACTIVE", stop_date="", stop_processed_at="")       # stop processed after T -> ACTIVE
        elif pit and s["sa_status"] == "STOPPED" and not s.get("stop_processed_at") and s.get("stop_date", "") > Td:
            s = dict(s, sa_status="ACTIVE", stop_date="")
        sas.append(s)
    t[F_SA] = sas
    t[F_BILL] = [dict(b) for b in g(F_BILL) if not pit or b["bill_run_at"] <= T]
    t[F_TODO] = [x for x in g(F_TODO) if not pit or x["created_at"] <= T]
    mtrs = []
    for m in g(F_MTR):
        if pit and m.get("connection_state_updated_at") and m["connection_state_updated_at"] > T:
            m = dict(m, connection_state="UNKNOWN", connection_state_updated_at="")
        mtrs.append(m)
    t[F_MTR] = mtrs
    t[F_READ] = [r for r in g(F_READ) if not pit or r["read_date"] < Td]
    wos = []
    for w in g(F_WO):
        if pit and w.get("created_at") and w["created_at"] > T:
            continue
        if pit and w["status"] == "COMPLETED" and w.get("completed_at") and w["completed_at"] > T:
            w = dict(w, status="OPEN", completed_at="")                                # open orders by created_at
        wos.append(w)
    t[F_WO] = wos
    crm = []
    for c in g(F_CRM):
        if pit and c["opened_at"] > T:
            continue
        if pit and c.get("closed_at") and c["closed_at"] > T:
            c = dict(c, status="OPEN", closed_at="")
        crm.append(c)
    t[F_CRM] = crm
    t[F_GL] = [x for x in g(F_GL) if not pit or x["posting_date"] <= Td]
    pers = []
    for p in g(F_PER):
        if pit and p.get("closed_at") and p["closed_at"] > T:
            p = dict(p, status="OPEN", closed_at="")
        pers.append(p)
    t[F_PER] = pers
    msgs = []
    for m in g(F_MSG):
        if pit and m["received_at"] > T:
            continue
        if pit and (not m.get("processed_at") or m["processed_at"] > T):
            m = dict(m, status="UNPROCESSED", processed_at="", error_message="", retry_count="0")
        msgs.append(m)
    t[F_MSG] = msgs
    # bill status: DISPUTED only once a BILLING_DISPUTE case exists at T (otherwise the bill was simply FINAL)
    disputed_accounts = {c["account_id"] for c in crm if c["category"] == "BILLING_DISPUTE"}
    sa_acc = {s["sa_id"]: s["account_id"] for s in g(F_SA)}
    for b in t[F_BILL]:
        if b["bill_status"] == "DISPUTED" and sa_acc.get(b["sa_id"]) not in disputed_accounts:
            b["bill_status"] = "FINAL"
    return t


class View:
    """Read-only, point-in-time snapshot of one dataset (+ optional sandbox overlays).

    drafts: bill ids that are not issued in this scenario (held, or being evaluated by the current bill run) -
    their GL journals are not posted yet, so they are hidden from GL.
    """

    def __init__(self, ds, T=None, overlays=None, drafts=None):
        self.ds, self.T = ds, T
        self.Tdate = T[:10] if T else None
        key = (ds.id, ds.folder, T)
        with _LOCK:
            base = _CACHE.get(key)
            if base is None:
                if len(_CACHE) > 64:
                    _CACHE.clear()
                base = _CACHE[key] = _point_in_time(ds, T)
        self.t = {k: list(v) for k, v in base.items()}
        self.drafts = set(drafts or ())
        self.read_rules = []
        self.overlay_ids = {}
        for ov in overlays or []:
            self._apply(ov)
        self._index()

    def _apply(self, ov):
        if ov.get("kind") == "read_rule":
            self.read_rules.append(ov)
            return
        rows = self.t.setdefault(ov["file"], [])
        kf = ov["key_field"]
        pos_all = self.__dict__.setdefault("_pos", {})
        pos = pos_all.get(ov["file"])
        if pos is None:
            pos = pos_all[ov["file"]] = {r.get(kf): i for i, r in enumerate(rows)}
        i = pos.get(ov["key"])
        if i is not None:
            rows[i] = dict(rows[i], **ov["row"], _overlay=ov["overlay_id"])
        else:
            pos[ov["key"]] = len(rows)
            rows.append(dict(ov["row"], _overlay=ov["overlay_id"]))
        self.overlay_ids[(ov["file"], ov["key"])] = ov["overlay_id"]

    def _index(self):
        t = self.t
        self.accounts = {a["account_id"]: a for a in t[F_ACC]}
        self.tariffs = {x["tariff_code"]: x for x in t[F_TAR]}
        self.xw = {x["oms_premise_id"]: x for x in t[F_XW]}
        self.premises_by_sp = {}
        for x in t[F_XW]:
            self.premises_by_sp.setdefault(x["ccb_sp_id"], []).append(x["oms_premise_id"])
        self.sas = {s["sa_id"]: s for s in t[F_SA]}
        self.sas_by_sp, self.sas_by_account = {}, {}
        for s in t[F_SA]:
            self.sas_by_sp.setdefault(s["ccb_sp_id"], []).append(s)
            self.sas_by_account.setdefault(s["account_id"], []).append(s)
        t[F_BILL] = [dict(b, _state="DRAFT" if b["bill_id"] in self.drafts else "ISSUED") for b in t[F_BILL]]   # never mutate cached rows
        self.bills = {b["bill_id"]: b for b in t[F_BILL]}
        self.bills_by_sa = {}
        for b in t[F_BILL]:
            self.bills_by_sa.setdefault(b["sa_id"], []).append(b)
        self.meters = {m["mdm_meter_id"]: m for m in t[F_MTR]}
        self.reads_by_meter = {}
        for r in t[F_READ]:
            self.reads_by_meter.setdefault(r["mdm_meter_id"], {})[r["read_date"]] = r
        for rr in self.read_rules:                     # read rules apply to reads as they become visible
            reads = self.reads_by_meter.get(rr["meter"], {})
            for d, r in list(reads.items()):
                if d >= rr["from_date"] and (not rr.get("to_date") or d <= rr["to_date"]):
                    reads[d] = dict(r, kwh="0.0", read_quality="CORRECTED", vee_rule="SANDBOX_ZERO_POST_DISCONNECT",
                                    _overlay=rr["overlay_id"])
        self.wos = {w["wo_id"]: w for w in t[F_WO]}
        self.wos_by_premise = {}
        for w in t[F_WO]:
            self.wos_by_premise.setdefault(w["oms_premise_id"], []).append(w)
        self.msgs = {m["msg_id"]: m for m in t[F_MSG]}
        self.msgs_by_wo = {}
        for m in t[F_MSG]:
            self.msgs_by_wo.setdefault(m["wo_id"], []).append(m)
        self.todos = {x["todo_id"]: x for x in t[F_TODO]}
        self.crm = {c["case_id"]: c for c in t[F_CRM]}
        self.crm_by_account = {}
        for c in t[F_CRM]:
            self.crm_by_account.setdefault(c["account_id"], []).append(c)
        self.gl_by_ref = {}
        for x in t[F_GL]:
            if x["source_ref"] in self.drafts:
                continue
            self.gl_by_ref.setdefault(x["source_ref"], []).append(x)
        self.periods = {p["accounting_period"]: p for p in t[F_PER]}
        self.memos = {m["memo_id"]: m for m in t[F_MEMO]}
        self.notes = {m["note_id"]: m for m in t[F_NOTE]}

    # ---- lookups ------------------------------------------------------------------------------
    def record(self, record_id):
        """(file, row) for any record id, used to validate citations."""
        for f, kf in KEY_FIELD.items():
            if not kf:
                continue
            for r in self.t.get(f, []):
                if r.get(kf) == record_id:
                    return f, r
        if record_id.startswith("JE-"):
            for x in self.t[F_GL]:
                if x["je_id"] == record_id:
                    return F_GL, x
        for reads in self.reads_by_meter.values():
            for r in reads.values():
                if r["read_id"] == record_id:
                    return F_READ, r
        return None

    def latest_msg(self, wo_id):
        ms = sorted(self.msgs_by_wo.get(wo_id, []), key=lambda m: m["received_at"])
        return ms[-1] if ms else None

    def todos_for(self, wo_id, sa_id):
        return [x for x in self.t[F_TODO] if x["related_entity"] == wo_id or (sa_id and x["sa_id"] == sa_id and x["todo_type"] == "FA_UPLOAD_ERROR")]

    def open_todos(self, wo_id, sa_id):
        return [x for x in self.todos_for(wo_id, sa_id) if x["status"] == "OPEN"]

    def completed_disconnects(self):
        return sorted([w for w in self.t[F_WO] if w["wo_type"] == "DISCONNECT" and w["status"] == "COMPLETED" and w.get("completed_at")],
                      key=lambda w: w["completed_at"])

    def current_period(self):
        """The accounting period of the scenario date if OPEN, else the next OPEN one."""
        opens = sorted(p for p, r in self.periods.items() if r["status"] == "OPEN")
        if not self.Tdate:
            return opens[0] if opens else None
        cur = self.Tdate[:7]
        if self.periods.get(cur, {}).get("status") == "OPEN":
            return cur
        later = [p for p in opens if p >= cur]
        return later[0] if later else None

    def reads_until(self):
        return add_days(self.Tdate, -1) if self.Tdate else max((r["read_date"] for r in self.t[F_READ]), default=None)


def sa_covering(sas, d):
    for s in sas:
        if s["start_date"] <= d and (not s.get("stop_date") or s["stop_date"] >= d):
            return s
    return None


def resolve_premise(view, premise, on_date):
    """OMS premise -> crosswalk -> service point (all SAs on it) + meter."""
    xw = view.xw.get(premise)
    if not xw:
        return None
    sp_sas = view.sas_by_sp.get(xw["ccb_sp_id"], [])
    if not sp_sas:
        return None
    sa = sa_covering(sp_sas, on_date) or sorted([s for s in sp_sas if s["start_date"] <= on_date] or sp_sas,
                                                key=lambda s: s["start_date"])[-1]
    return {"xw": xw, "sa": sa, "sp_sas": sp_sas, "account": view.accounts.get(sa["account_id"]),
            "meter": view.meters.get(xw["mdm_meter_id"]), "premise": premise, "meter_id": xw["mdm_meter_id"]}


def disconnect_window(view, wo, premise=None):
    """Disconnect day billable; window = day after .. day before the next COMPLETED RECONNECT (or open-ended)."""
    dd = wo["completed_at"][:10]
    recs = sorted([w for w in view.wos_by_premise.get(premise or wo["oms_premise_id"], [])
                   if w["wo_type"] == "RECONNECT" and w["status"] == "COMPLETED" and w.get("completed_at", "") > wo["completed_at"]],
                  key=lambda w: w["completed_at"])
    rec = recs[0] if recs else None
    return {"disconnect_date": dd, "start": add_days(dd, 1),
            "end": add_days(rec["completed_at"][:10], -1) if rec else None, "reconnect_wo_id": rec["wo_id"] if rec else None}


def kwh(r):
    return D(r["kwh"]) if r else D(0)
