"""FastAPI app: REST API + Server-Sent Events + the static Alpine.js UI (prompt §15)."""
import asyncio
import csv
import io
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import backscan as BS
from . import guard
from .auth import current_user, require
from .config import Config
from .datasource import F_READ, KEY_FIELD, SOURCE_FILES, SYSTEM_LABEL, SYSTEM_OF_FILE, Registry
from .engine import Engine
from .kpi import kpis
from .knowledge import KnowledgeBase
from .pit import View
from .selftest import SelfTest
from .util import DEFAULT_DATA_DIR, STATE_DIR, STATIC_DIR, AppError, NotFound, PermissionDenied

AUDIT_COLS = ["event_id", "timestamp_utc", "timestamp_local", "scenario_time", "dataset_id", "scenario_id", "actor_type", "actor_id", "actor_name",
              "actor_persona", "action", "object_type", "object_id", "case_id", "before", "after", "reason", "agent_mode", "autopilot", "prev_hash", "hash"]


class Body(BaseModel):
    model_config = {"extra": "allow"}


class AppState:
    def __init__(self, data_dir, state_root):
        self.cfg = Config()
        self.data_dir = data_dir
        self.registry = Registry(data_dir)
        self.kb = KnowledgeBase(self.cfg)
        self.engine = Engine(self.registry, self.kb, self.cfg, state_root=state_root)
        self.source_hashes = {d["id"]: self.registry.get(d["id"]).source_hashes() for d in self.registry.list()}
        if self.kb.mode != "chromadb":
            self.engine.log(None, None, None, "sys.cli", "AGENT_TOOL_CALL", "knowledge", "fallback", after={"mode": "fallback", "error": self.kb.error},
                            reason="Knowledge store: fallback mode")
        guard.install(lambda host, port: self.engine.log(None, None, None, "sys.guard", "OFFLINE_GUARD_BLOCKED", "socket", "%s:%s" % (host, port),
                                                         after={"host": host, "port": port}, reason="Outbound connection blocked (OFFLINE_GUARD=1)"))


def create_app(data_dir=None, state_root=STATE_DIR):
    app = FastAPI(title="AI-Utilico", docs_url=None, redoc_url=None, openapi_url=None)
    st = AppState(data_dir or os.environ.get("UTILICO_DATA_DIR") or DEFAULT_DATA_DIR, state_root)
    app.state.tb = st
    cfg, eng = st.cfg, st.engine

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.middleware("http")
    async def security(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return PlainTextResponse("Forbidden host", status_code=403)
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def ctx(request):
        return eng.session(request.headers.get("x-dataset-id") or "dataset_A_with_errors")

    # ---- static -------------------------------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    # ---- session / context ----------------------------------------------------------------------------
    @app.get("/api/users")
    async def users():
        return {"users": cfg.users(), "agent": {"label": "Agent: %s (scripted)" % cfg.agent()["mode"], "mode": cfg.agent()["mode"]},
                "knowledge": {"mode": st.kb.mode, "error": st.kb.error}}

    @app.post("/api/session/sign_in")
    async def sign_in(request: Request, body: Body):
        b = body.model_dump()
        u = cfg.user(b.get("user_id"))
        if not u:
            raise AppError("Unknown user")
        prev = b.get("previous_user_id")
        if prev and prev != u["user_id"]:
            eng.log(None, None, None, u["user_id"], "SWITCH_USER", "session", u["user_id"], {"user_id": prev}, {"user_id": u["user_id"]})
        else:
            eng.log(None, None, None, u["user_id"], "SIGN_IN", "session", u["user_id"])
        return {"user": u}

    @app.get("/api/me")
    async def me(request: Request):
        u = current_user(cfg, request)
        return {"user": u, "permissions": {k: v.get(u["persona"], "-") for k, v in cfg.screens().items()},
                "screens": {k: v["label"] for k, v in cfg.screens().items()}}

    @app.get("/api/datasets")
    async def datasets(request: Request):
        current_user(cfg, request)
        from .engine import DATA_START, EXTRACT_END
        return {"datasets": st.registry.list(), "data_dir": st.data_dir, "data_start": DATA_START, "extract_end": EXTRACT_END}

    @app.post("/api/context")
    async def context(request: Request, body: Body):
        u = current_user(cfg, request)
        b = body.model_dump()
        s = ctx(request)
        s.log(u["user_id"], "DATASET_SWITCH", "dataset", s.ds.id, before={"dataset_id": b.get("previous")}, after={"dataset_id": s.ds.id})
        return {"ok": True}

    def mywork(u, s):
        items = []
        p = u["persona"]
        for c in s.state["cases"].values():
            na = None
            if p == "CSR" and c["state"] == "TRIAGE":
                na = "Confirm the ID match" if c["match"]["status"] == "PENDING" else "Confirm triage"
            elif p == "DRI" and c["state"] == "AWAITING_DRI":
                na = "Decide - approve or reject (final)"
            if na:
                sv = s.sla_view(c)
                items.append({"case_id": c["case_id"], "type": c["type"], "customer_name": c["customer_name"], "account_id": c["account_id"],
                              "amount": c["amount"], "priority": c["priority"], "triage": (c.get("triage") or {}).get("colour"), "state": c["state"],
                              "next_action": na, "sla": sv.get("resolve"), "sla_first": sv.get("first_action"), "sla_worst": sv["worst"], "returned": c["returned"]})
        if p == "CSR":
            n = sum(1 for r in s.backscan_candidates() if r["eligible"] and not r["sent"])
            if n:
                items.append({"case_id": None, "type": "BACKSCAN", "next_action": "Send %d eligible back-scan finding(s) to the workflow" % n, "priority": None})
        order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3, None: 9}
        items.sort(key=lambda i: (order.get(i.get("priority")), (i.get("sla") or {}).get("due") or "9"))
        return items

    def case_row(s, c):
        rec = c.get("recommendation") or {}
        sv = s.sla_view(c)
        return {k: c.get(k) for k in ("case_id", "jira_id", "type", "state", "returned", "account_id", "customer_name", "amount", "priority", "wo_id",
                                      "opened_at", "as_of", "lookup", "rules_fired", "flags", "linked_case_id")} | {
            "code": s.ds.case_codes.get(c["account_id"]), "triage": (c.get("triage") or {}).get("colour"), "option": rec.get("option"),
            "label": rec.get("label"), "cause": rec.get("probable_cause"), "confidence": rec.get("confidence"), "sla_worst": sv["worst"],
            "sla": sv.get("resolve"), "match": c["match"]["status"], "agent_error": c.get("agent_error"), "waiting_on": cfg.workflow()["human_states"].get(c["state"])}

    @app.get("/api/state")
    async def state(request: Request):
        u = current_user(cfg, request)
        s = ctx(request)
        return {"mywork": len(mywork(u, s)), "knowledge": {"mode": st.kb.mode, "error": st.kb.error}, "extract_end": s.state["clock"],
                "dataset": s.ds.id, "case_codes": s.ds.case_codes, "busy": not s.idle(), "errors": eng.errors[-3:]}

    # ---- dashboard, my work, cases --------------------------------------------------------------------
    @app.get("/api/dashboard")
    async def dashboard(request: Request, feed: str = "all"):
        u = require(st, request, "dashboard")
        s = ctx(request)
        f = {"dataset": s.ds.id, "scenario": s.sc_id}
        if feed == "mine":
            f["user"] = u["user_id"]
        elif feed == "mycases":
            f["case_ids"] = sorted({e["case_id"] for e in eng.audit.events if e.get("actor_id") == u["user_id"] and e.get("case_id")})
        return {"kpis": kpis(s), "feed": eng.audit.query(f, 25)["events"], "mywork": mywork(u, s)[:5],
                "cases": [case_row(s, c) for c in s.state["cases"].values() if c["state"] != "NO_ISSUE"]}

    @app.get("/api/mywork")
    async def my_work(request: Request):
        u = require(st, request, "mywork")
        return {"items": mywork(u, ctx(request))}

    @app.get("/api/cases")
    async def cases(request: Request):
        require(st, request, "cases")
        s = ctx(request)
        return {"cases": [case_row(s, c) for c in sorted(s.state["cases"].values(), key=lambda c: c["case_id"]) if c["state"] != "NO_ISSUE"]}

    # ---- CSR account lookup ------------------------------------------------------------------------------
    @app.post("/api/lookup")
    async def lookup(request: Request, body: Body):
        u = require(st, request, "lookup", ("A",), "look up an account")
        b = body.model_dump()
        return ctx(request).lookup(u, b.get("account_id"), b.get("as_of"))

    @app.get("/api/lookup")
    async def lookups(request: Request):
        require(st, request, "lookup", ("A",), "open Account lookup")
        s = ctx(request)
        return {"extract_end": s.state["clock"], "items": [case_row(s, c) | {"reconciled": bool(c.get("reconciliation")), "facts": len(s.evidence.for_case(c["case_id"]))}
                                            for c in sorted(s.state["cases"].values(), key=lambda c: c["case_id"], reverse=True) if c.get("lookup")]}

    @app.get("/api/cases/{case_id}")
    async def case_detail(case_id: str, request: Request, view: int = 0):
        u = require(st, request, "case")
        s = ctx(request)
        c = s.state["cases"].get(case_id)
        if not c:
            raise NotFound("Unknown case")
        if view:
            s.log(u["user_id"], "CASE_VIEWED", "case", case_id, case_id)
        out = {k: v for k, v in c.items() if k != "packet_snapshot"}
        out["code"] = s.ds.case_codes.get(c["account_id"])
        out["allowed_actions"] = s.allowed_actions(u, c)
        out["sla_view"] = s.sla_view(c)
        out["facts"] = s.evidence.for_case(case_id)
        out["audit_trail"] = eng.audit.query({"case": case_id, "dataset": s.ds.id, "scenario": s.sc_id}, 500)["events"]
        link = s.state["cases"].get(c.get("linked_case_id")) if c.get("linked_case_id") else None
        out["linked_case"] = case_row(s, link) | {"seal": link.get("seal")} if link else None
        return out

    @app.post("/api/cases/{case_id}/action")
    async def case_action(case_id: str, request: Request, body: Body):
        u = require(st, request, "case", ("A",), "act on cases")
        b = body.model_dump()
        s = ctx(request)
        c = s.act(u, case_id, b.get("action"), b)
        return {"case": case_row(s, c)}

    # ---- back-scan, daily check, explorer, knowledge ---------------------------------------------------------
    @app.get("/api/backscan")
    async def backscan(request: Request):
        require(st, request, "backscan")
        s = ctx(request)
        v = View(s.ds, s.state["clock"])
        det = BS.detect(v, v.Tdate)
        cand = s.backscan_candidates()
        by_wo = {r["wo_id"]: r for r in cand}
        return {"as_of": v.Tdate, "findings": [dict(BS.finding_json(f), workflow=by_wo.get(f["wo"]["wo_id"])) for f in det["back"]["findings"]],
                "unmatched": [{"wo_id": u["wo"]["wo_id"], "premise": u["wo"]["oms_premise_id"], "suggestion": u["suggestion"], "exposure": float(u["exposure"] or 0),
                               "account_id": u["account_id"], "subtype": u["wo"]["wo_subtype"], "disconnect_at": u["wo"]["completed_at"],
                               "workflow": by_wo.get(u["wo"]["wo_id"])} for u in det["back"]["unmatched"]],
                "total_credit": float(det["back"]["total_credit"]), "scanned": det["back"]["scanned"], "matched": det["back"]["matched"],
                "scan": [det["scan_start"], det["scan_end"]], "can_send": True, "case_codes": s.ds.case_codes}

    @app.post("/api/backscan/send")
    async def backscan_send(request: Request, body: Body):
        u = require(st, request, "backscan", ("A",), "send back-scan findings to the workflow")
        return {"cases": ctx(request).send_backscan(u, body.model_dump().get("wo_ids") or None)}

    @app.get("/api/daily")
    async def daily(request: Request, as_of: str = None):
        require(st, request, "daily")
        s = ctx(request)
        as_of = as_of or "2026-11-02"
        v = View(s.ds, as_of + " 23:59")
        det = BS.detect(v, as_of)
        rows = []
        for h in det["daily"]["holds"]:
            rows.append({"account_id": h["account_id"], "customer_name": (h["res"]["account"] or {}).get("customer_name"), "sa_id": h["sa_id"],
                         "wo_id": h["wo"]["wo_id"], "subtype": h["wo"]["wo_subtype"], "disconnect_at": h["wo"]["completed_at"],
                         "msg": {k: (h["msg"] or {}).get(k) for k in ("msg_id", "status", "processed_at")}, "todos": [t["todo_id"] for t in h["todos"]]})
        return {"as_of": as_of, "holds": rows, "unmatched": [{"wo_id": u["wo"]["wo_id"], "premise": u["wo"]["oms_premise_id"], "suggestion": u["suggestion"]} for u in det["daily"]["unmatched"]],
                "excluded_nonpay": [x["wo"]["wo_id"] for x in det["daily"]["excluded_nonpay"]], "case_codes": s.ds.case_codes}

    @app.get("/api/explorer")
    async def explorer(request: Request, file: str = "oms_work_orders.csv", q: str = "", page: int = 0):
        require(st, request, "explorer")
        s = ctx(request)
        files = list(SOURCE_FILES)
        if file not in files:
            raise NotFound("Unknown file")
        v = View(s.ds, None)      # the extracts exactly as delivered (read-only; nothing is ever written back)
        if file == F_READ:
            rows = [r for m in v.reads_by_meter.values() for r in m.values()]
            rows.sort(key=lambda r: r["read_id"])
        else:
            rows = list(v.t.get(file, []))
        headers = s.ds.headers(file) or sorted({k for r in rows for k in r if not k.startswith("_")})
        terms = [t for t in q.lower().split() if t]
        if terms:
            rows = [r for r in rows if all(t in " ".join(str(x) for x in r.values()).lower() for t in terms)]
        n = len(rows)
        page_rows = rows[page * 100:(page + 1) * 100]
        return {"file": file, "files": [{"file": f, "system": SYSTEM_LABEL.get(SYSTEM_OF_FILE.get(f), "Sandbox")} for f in files],
                "headers": headers, "rows": [{"cells": [r.get(h, "") for h in headers]} for r in page_rows],
                "total": n, "page": page, "pages": max(1, (n + 99) // 100), "key": KEY_FIELD.get(file)}

    @app.get("/api/knowledge")
    async def knowledge(request: Request, q: str = "", collection: str = "case_history"):
        require(st, request, "knowledge")
        s = ctx(request)
        if collection not in ("rules", "thresholds", "case_history"):
            raise NotFound("Unknown collection")
        return {"mode": s.knowledge.mode, "results": s.knowledge.search(q or "billed after disconnection", collection, 5)}

    # ---- audit ---------------------------------------------------------------------------------------------
    def audit_filters(request):
        qp = request.query_params
        return {k: qp.get(k) for k in ("user", "persona", "action", "case", "scenario", "dataset", "from", "to", "q") if qp.get(k)}

    @app.get("/api/audit")
    async def audit(request: Request):
        require(st, request, "audit")
        res = eng.audit.query(audit_filters(request), 500)
        res["actions"] = sorted({e["action"] for e in eng.audit.events})
        return res

    @app.get("/api/audit/export")
    async def audit_export(request: Request):
        u = require(st, request, "audit", what="export the audit log")
        evs = eng.audit.query(audit_filters(request), 10 ** 9)["events"][::-1]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(AUDIT_COLS)
        for e in evs:
            w.writerow([json.dumps(e.get(c), ensure_ascii=False) if isinstance(e.get(c), (dict, list)) else e.get(c) for c in AUDIT_COLS])
        eng.log(request.headers.get("x-dataset-id"), "WORKSPACE", None, u["user_id"], "AUDIT_EXPORTED", "audit_log", "csv",
                after={"events": len(evs), "filters": audit_filters(request)})
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="audit_log.csv"'})

    @app.post("/api/audit/verify")
    async def audit_verify(request: Request):
        require(st, request, "audit", what="verify integrity")
        return eng.verify_integrity()

    # ---- self-test -------------------------------------------------------------------------------------------
    @app.get("/api/selftest")
    async def selftest(request: Request):
        require(st, request, "selftest")
        return await asyncio.to_thread(lambda: SelfTest(st.registry, st.kb, st.source_hashes).run())

    # ---- SSE --------------------------------------------------------------------------------------------------
    @app.get("/api/stream")
    async def stream(request: Request):
        current_user(cfg, request)
        ds, sc = request.headers.get("x-dataset-id"), "WORKSPACE"
        q = asyncio.Queue(maxsize=2000)
        eng.subscribers.add(q)

        async def gen():
            try:
                yield "retry: 2000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if msg.get("ds") not in (None, ds) or msg.get("sc") not in (None, sc):
                        continue
                    yield "event: %s\ndata: %s\n\n" % (msg["type"], json.dumps(msg, ensure_ascii=False, default=str))
            finally:
                eng.subscribers.discard(q)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    return app
