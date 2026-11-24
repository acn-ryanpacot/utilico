"""Engine and the per-dataset workspace Session. Everything runs on one asyncio loop, so state changes are not
interleaved; agents are asyncio tasks (the five retrieval lanes run concurrently via asyncio.gather).

There is no timeline. Each case has its own "as of" time (the CSR's lookup date, or the extract date for the
back-scan); every read of that case - agents, rules, money - goes through the point-in-time view at that time.
"""
import asyncio
import contextvars
import os
import shutil

from .agents import policies  # noqa: F401  (registers the mock policies)
from .agents.llm import make_llm_client
from .agents.tools import describe_args
from .audit import AuditLog
from .config import Config
from .evidence import EvidenceStore
from .intake import IntakeMixin
from .knowledge import KnowledgeStore
from .pit import View
from .sandbox import Sandbox
from .util import STATE_DIR, NotFound, now_utc, read_json, write_json_atomic
from .workflow import CaseMixin

STATE_VERSION = 4          # 4 = no timeline; one workspace per dataset; per-case "as of" time
WORKSPACE = "WORKSPACE"    # the single session id per dataset (audit scenario_id)
EXTRACT_END = "2026-11-10 23:59"
DATA_START = "2026-08-01"
CASE_T = contextvars.ContextVar("case_as_of", default=None)


def new_state(ds_id):
    return {"version": STATE_VERSION, "dataset_id": ds_id, "created_at": now_utc(), "clock": EXTRACT_END,
            "cases": {}, "case_seq": 0, "lookup_seq": 0, "approved": [], "backscan_sent": [], "unissued": []}


class Session(IntakeMixin, CaseMixin):
    def __init__(self, engine, ds, folder=None):
        self.engine, self.ds, self.sc_id = engine, ds, WORKSPACE
        self.cfg, self.llm = engine.cfg, engine.llm
        self.folder = folder
        self.fast = engine.fast
        self.tasks = set()
        self._autopilot_flag = False
        self.knowledge = KnowledgeStore(engine.kb, os.path.join(folder, "knowledge") if folder else None)
        self._load()

    # ---- persistence ------------------------------------------------------------------------------------
    def _load(self):
        st = read_json(os.path.join(self.folder, "state.json")) if self.folder else None
        if st and st.get("version") != STATE_VERSION and self.folder:
            for sub in ("sandbox", "outputs"):
                shutil.rmtree(os.path.join(self.folder, sub), ignore_errors=True)
            for f in ("state.json", "evidence.jsonl"):
                try:
                    os.remove(os.path.join(self.folder, f))
                except OSError:
                    pass
            st = None
        self.state = st or new_state(self.ds.id)
        self.sandbox = Sandbox(None)          # nothing is ever written: the process ends at the DRI's decision
        self.evidence = EvidenceStore(os.path.join(self.folder, "evidence.jsonl") if self.folder else None)

    def save(self):
        if self.folder:
            write_json_atomic(os.path.join(self.folder, "state.json"), self.state)

    def write_output_file(self, case_id, name, text):       # dormant (output drafting is switched off)
        return None

    # ---- point in time -----------------------------------------------------------------------------------
    @property
    def clock(self):
        """The current case's "as of" time while its agents / actions run, else the extract end."""
        return CASE_T.get() or self.state["clock"]

    def at(self, T):
        """Context manager: run code (and the tasks it spawns) as of time T."""
        session = self

        class _At:
            def __enter__(self_):
                self_.tok = CASE_T.set(T)
                return session

            def __exit__(self_, *a):
                CASE_T.reset(self_.tok)
        return _At()

    def real_now(self):
        return now_utc()

    def view(self, sandbox=True, drafts=None, T=None):
        return View(self.ds, T or self.clock, None, set(drafts) if drafts is not None else set(self.state.get("unissued", [])))

    # ---- logging / streaming ---------------------------------------------------------------------------
    def log(self, actor_id, action, object_type, object_id, case_id=None, before=None, after=None, reason=None):
        return self.engine.log(self.ds.id, self.sc_id, self.clock, actor_id, action, object_type, object_id, case_id, before, after, reason)

    def log_tool(self, case, agent_id, lane, name, args, ok, summary):
        entry = {"n": len(case["activity"]) + 1, "agent": agent_id, "lane": lane, "tool": name, "args": describe_args(args), "ok": ok,
                 "summary": summary, "at": now_utc(), "scenario_time": self.clock}
        case["activity"].append(entry)
        self.log(agent_id, "AGENT_TOOL_CALL", "tool", name, case["case_id"], None, {"lane": lane, "args": args, "ok": ok, "result": summary})
        self.publish({"type": "activity", "case_id": case["case_id"], "entry": entry})

    def publish(self, msg):
        self.engine.publish(dict(msg, ds=self.ds.id, sc=self.sc_id))

    # ---- tasks ------------------------------------------------------------------------------------------
    def spawn(self, coro):
        t = asyncio.get_event_loop().create_task(coro)        # copies the current context (incl. the case's as-of time)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t):
        if not t.cancelled() and t.exception():
            import traceback
            traceback.print_exception(t.exception())
            self.engine.errors.append(repr(t.exception()))

    def idle(self):
        return not self.tasks

    async def wait_idle(self):
        while self.tasks:
            await asyncio.sleep(0 if self.fast else 0.02)

    async def latency(self):
        ms = self.cfg.agent().get("step_latency_ms", 250)
        await asyncio.sleep(0 if self.fast else ms / 1000.0)


class Engine:
    def __init__(self, registry, kb, cfg=None, state_root=STATE_DIR, audit=None, fast=False):
        self.registry, self.kb = registry, kb
        self.cfg = cfg or Config()
        self.llm = make_llm_client(self.cfg)
        self.state_root = state_root            # None -> in-memory (self-test)
        self.audit = audit or AuditLog(os.path.join(state_root, "audit_log.jsonl") if state_root else None)
        self.fast = fast
        self.sessions = {}
        self.subscribers = set()
        self.errors = []
        self.audit.listeners.append(lambda ev: self.publish({"type": "audit", "event": ev, "ds": ev.get("dataset_id"), "sc": ev.get("scenario_id")}))

    def session(self, ds_id, sc_id=WORKSPACE):
        if sc_id != WORKSPACE:
            raise NotFound("Unknown workspace %s" % sc_id)
        if ds_id not in self.sessions:
            ds = self.registry.get(ds_id)
            folder = os.path.join(self.state_root, ds_id, WORKSPACE) if self.state_root else None
            self.sessions[ds_id] = Session(self, ds, folder)
        return self.sessions[ds_id]

    def actor(self, actor_id):
        u = self.cfg.user(actor_id)
        if u:
            return "USER", u["name"], u["persona"]
        a = self.cfg.actors().get(actor_id)
        if a:
            return a["type"], a["name"], None
        return "SYSTEM", actor_id, None

    def log(self, ds_id, sc_id, scenario_time, actor_id, action, object_type, object_id, case_id=None, before=None, after=None, reason=None, autopilot=False):
        typ, name, persona = self.actor(actor_id)
        return self.audit.append(scenario_time=scenario_time, dataset_id=ds_id, scenario_id=sc_id, actor_type=typ, actor_id=actor_id,
                                 actor_name=name, actor_persona=persona, action=action, object_type=object_type, object_id=object_id,
                                 case_id=case_id, before=before, after=after, reason=reason, agent_mode=self.llm.mode.upper(), autopilot=False)

    def publish(self, msg):
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def verify_integrity(self):
        """Hash chain + every case seal."""
        chain = self.audit.verify_chain()
        seals, broken = [], None
        for e in self.audit.events:
            if e["action"] != "CASE_SEALED":
                continue
            try:
                s = self.session(e["dataset_id"], e["scenario_id"])
            except Exception:
                continue          # a sealed case from an earlier app version - the event stays in the chain
            case = s.state["cases"].get(e["case_id"])
            if not case or not case.get("seal") or case["seal"]["case_hash"] != (e.get("after") or {}).get("case_hash"):
                continue
            h = s.case_hash(case, upto_event=e["event_id"])
            ok = h["case_hash"] == case["seal"]["case_hash"]
            seals.append({"case_id": case["case_id"], "dataset_id": e["dataset_id"], "scenario_id": e["scenario_id"], "ok": ok})
            if not ok and broken is None:
                broken = {"case_id": case["case_id"], "reason": "case_hash does not match the sealed audit events and facts"}
        return {"ok": chain["ok"] and broken is None, "chain": chain, "seals": seals, "first_broken_seal": broken}
