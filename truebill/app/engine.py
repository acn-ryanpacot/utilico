"""Engine and per-(dataset, scenario) Session. Everything runs on one asyncio loop, so state changes are not
interleaved; agents are asyncio tasks (the five retrieval lanes run concurrently via asyncio.gather)."""
import asyncio
import os
import shutil

from .agents import policies  # noqa: F401  (registers the mock policies)
from .agents.llm import make_llm_client
from .agents.tools import describe_args
from .audit import AuditLog
from .config import Config
from .events import PRESETS, SCENARIOS, TimelineMixin, build_events
from .evidence import EvidenceStore
from .knowledge import KnowledgeStore
from .pit import View
from .prebill import PrebillMixin
from .sandbox import Sandbox
from .util import STATE_DIR, AppError, NotFound, now_utc, read_json, sha256_text, write_json_atomic
from .workflow import CaseMixin

GUIDE = [
    ("Jump to 4 Nov 09:40. The Harbourline dispute (CASE-300276) arrives and the Orchestrator opens a REMEDIATE case.", "jump", "2026-11-04 09:40"),
    ("Agent activity streams: five retrieval lanes in parallel, then reconciliation. The packet is ready with R1-R4 all fired.", "agents", None),
    ("Maya (CSR) confirms triage. Daniel (DRI) approves Prorate - the recommended option.", "act", ["TRIAGE", "AWAITING_DRI"]),
    ("Aisha (Finance) approves the $1,436.50 reversal of revenue already posted to the GL.", "act", ["PENDING_FINANCE"]),
    ("Daniel approves the plan. The Execution agent runs each approved step and its check; the verifier passes.", "act", ["PLAN_APPROVAL"]),
    ("The four outputs appear (Jira BILL-NEW-1147) and the consistency check passes.", "agents", None),
    ("Daniel approves the outputs. Maya sends the customer summary. Daniel closes and seals the case.", "act", ["OUTPUT_APPROVAL", "READY_FOR_CSR", "RESOLVED"]),
    ("Jump to 9 Nov 15:30. The regulatory complaint opens a linked REGULATORY_RESPONSE case; R4 is now High (period 2026-10 closed on 6 Nov).", "jump", "2026-11-09 15:30"),
    ("The regulator response is drafted from the sealed packet; Daniel approves the regulatory content and the case is sealed.", "act", ["TRIAGE", "AWAITING_DRI", "OUTPUT_APPROVAL", "READY_FOR_CSR", "RESOLVED"]),
    ("The KPI tiles update: detection, citations, consistency, packet time and the Day-30 gate.", "done", None),
]


def new_state(ds_id, sc_id, events):
    return {"dataset_id": ds_id, "scenario_id": sc_id, "created_at": now_utc(), "clock": SCENARIOS[sc_id]["start"], "events": events,
            "timeline": {"playing": False, "speed": 1.0, "pause_for_humans": True, "paused_reason": None, "target": None},
            "toggles": {"followup": True, "stale_mdm": False, "inject_failure": False, "autopilot": False},
            "cases": {}, "case_seq": 0, "bills": {}, "unissued": [], "runs": [], "prevented": [], "backscan_sent": [], "banner": None,
            "guide": None, "last_event_real": None}


class Session(TimelineMixin, PrebillMixin, CaseMixin):
    def __init__(self, engine, ds, sc_id, folder=None):
        self.engine, self.ds, self.sc_id = engine, ds, sc_id
        self.cfg, self.llm = engine.cfg, engine.llm
        self.folder = folder
        self.fast = engine.fast
        self.tasks = set()
        self._runner = None
        self._ap_task = None
        self._guide_task = None
        self._autopilot_flag = False
        self.knowledge = KnowledgeStore(engine.kb, os.path.join(folder, "knowledge") if folder else None)
        self._load()

    # ---- persistence ------------------------------------------------------------------------------------
    def _load(self):
        st = read_json(os.path.join(self.folder, "state.json")) if self.folder else None
        if not st:
            st = new_state(self.ds.id, self.sc_id, build_events(self.ds, self.sc_id, self.cfg.rules()))
        st["timeline"]["playing"] = False          # never resume playing after a restart
        st["toggles"]["autopilot"] = False
        self.state = st
        self.sandbox = Sandbox(os.path.join(self.folder, "sandbox") if self.folder else None)
        self.evidence = EvidenceStore(os.path.join(self.folder, "evidence.jsonl") if self.folder else None)

    def save(self):
        if self.folder:
            write_json_atomic(os.path.join(self.folder, "state.json"), self.state)

    def write_output_file(self, case_id, name, text):
        if not self.folder:
            return None
        p = os.path.join(self.folder, "outputs", case_id, name + ".md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return p

    def reset(self, user_id, reason="Demo state reset", keep_guide=False):
        for t in list(self.tasks):
            t.cancel()
        self.tasks = set()
        if self._runner:
            self._runner.cancel()
        self._runner = None
        for t in (self._ap_task,) + (() if keep_guide else (self._guide_task,)):
            if t:
                t.cancel()
        self._ap_task = None
        if self.folder:
            for sub in ("sandbox", "outputs"):
                shutil.rmtree(os.path.join(self.folder, sub), ignore_errors=True)
            for f in ("state.json", "evidence.jsonl"):
                try:
                    os.remove(os.path.join(self.folder, f))
                except OSError:
                    pass
        self.knowledge.reset()
        self.state = new_state(self.ds.id, self.sc_id, build_events(self.ds, self.sc_id, self.cfg.rules()))
        self.sandbox = Sandbox(os.path.join(self.folder, "sandbox") if self.folder else None)
        self.evidence = EvidenceStore(os.path.join(self.folder, "evidence.jsonl") if self.folder else None)
        self.log(user_id, "DEMO_RESET", "scenario", self.sc_id, reason=reason + "; audit log kept")
        self.save()
        self.publish({"type": "session"})

    # ---- clock, views ----------------------------------------------------------------------------------
    @property
    def clock(self):
        return self.state["clock"]

    @clock.setter
    def clock(self, v):
        self.state["clock"] = v

    def real_now(self):
        return now_utc()

    def view(self, sandbox=True, drafts=None, T=None):
        return View(self.ds, T or self.clock, self.sandbox.overlays if sandbox else None,
                    set(drafts) if drafts is not None else set(self.state["unissued"]))

    # ---- logging / streaming ---------------------------------------------------------------------------
    def log(self, actor_id, action, object_type, object_id, case_id=None, before=None, after=None, reason=None):
        return self.engine.log(self.ds.id, self.sc_id, self.clock, actor_id, action, object_type, object_id, case_id, before, after, reason,
                               autopilot=self._autopilot_flag)

    def log_tool(self, case, agent_id, lane, name, args, ok, summary):
        entry = {"n": len(case["activity"]) + 1, "agent": agent_id, "lane": lane, "tool": name, "args": describe_args(args), "ok": ok,
                 "summary": summary, "at": now_utc(), "scenario_time": self.clock}
        case["activity"].append(entry)
        self.log(agent_id, "AGENT_TOOL_CALL", "tool", name, case["case_id"], None,
                 {"lane": lane, "args": args if name != "ADD_CRM_NOTE" else {k: v for k, v in args.items() if k != "text"}, "ok": ok, "result": summary})
        self.publish({"type": "activity", "case_id": case["case_id"], "entry": entry})

    def publish(self, msg):
        self.engine.publish(dict(msg, ds=self.ds.id, sc=self.sc_id))

    def publish_timeline(self):
        self.publish({"type": "timeline", "clock": self.clock, "playing": self.state["timeline"]["playing"],
                      "paused_reason": self.state["timeline"].get("paused_reason")})

    # ---- tasks ------------------------------------------------------------------------------------------
    def spawn(self, coro, track=True):
        t = asyncio.get_event_loop().create_task(coro)
        if track:
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t):
        if not t.cancelled() and t.exception():
            import traceback
            traceback.print_exception(t.exception())
            self.engine.errors.append(repr(t.exception()))
        self.kick_autopilot()

    def idle(self):
        return not self.tasks

    async def wait_idle(self):
        while self.tasks:
            await asyncio.sleep(0 if self.fast else 0.02)

    async def latency(self):
        ms = self.cfg.agent().get("step_latency_ms", 250)
        await asyncio.sleep(0 if self.fast else ms / 1000.0)

    # ---- toggles / autopilot -----------------------------------------------------------------------------
    def set_toggle(self, user_id, name, value):
        if name not in self.state["toggles"] and name != "pause_for_humans":
            raise AppError("Unknown toggle %s" % name)
        if name == "pause_for_humans":
            self.state["timeline"]["pause_for_humans"] = bool(value)
        else:
            self.state["toggles"][name] = bool(value)
        if name == "stale_mdm":
            self.log(user_id, "STALE_FEED_TOGGLED", "connector", "MDM", after={"stale": bool(value), "lag_minutes": self.freshness()["lags"]["MDM"]})
            if not value and self.state.get("banner", {}) and (self.state["banner"] or {}).get("kind") == "stale":
                self.state["banner"] = {"kind": "info", "text": "MDM feed fresh again - step or play the timeline to process the blocked bill run."}
        elif name == "inject_failure":
            if value:
                self.log(user_id, "FAILURE_INJECTED", "demo", "next_step", after={"armed": True})
        elif name == "autopilot" and value:
            self.kick_autopilot()
        self.save()
        self.publish({"type": "session"})

    def kick_autopilot(self):
        if self.state["toggles"].get("autopilot") and (self._ap_task is None or self._ap_task.done()):
            try:
                self._ap_task = asyncio.get_event_loop().create_task(self._autopilot_loop())
            except RuntimeError:
                pass

    async def _autopilot_loop(self):
        while self.state["toggles"].get("autopilot"):
            done = self.autopilot_step()
            if done is None:
                if self.idle():
                    return
                await asyncio.sleep(0 if self.fast else 0.05)
                continue
            await asyncio.sleep(0 if self.fast else 0.35)

    async def run_to_end(self, autopilot=True):
        """Self-test: play the whole scenario with the autopilot until nothing is left to do."""
        self.state["toggles"]["autopilot"] = autopilot
        self.state["timeline"]["playing"] = True
        self.state["timeline"]["speed"] = 1.0
        for _ in range(100000):
            if self.state["timeline"]["playing"]:
                self.ensure_runner()
            await asyncio.sleep(0)
            if autopilot:
                self.autopilot_step()
            if not self.state["timeline"]["playing"] and self.idle() and (not autopilot or self.autopilot_step() is None) and self.idle():
                if not self.pending_events() or self.state["timeline"].get("paused_reason") == "stale":
                    break
                self.state["timeline"]["playing"] = True
        await self.wait_idle()

    # ---- guided demo --------------------------------------------------------------------------------------
    def start_guide(self, user_id):
        if self.sc_id != "DISPUTE_REPLAY":
            raise AppError("The guided demo runs on DISPUTE_REPLAY (Dataset A)")
        if self._guide_task and not self._guide_task.done():
            raise AppError("The guided demo is already running")
        self._guide_task = asyncio.get_event_loop().create_task(self._guide(user_id))

    async def _guide(self, user_id):
        dwell = 0 if self.fast else 4.0
        self.reset(user_id, "Guided demo started", keep_guide=True)
        self.state["guide"] = {"step": 0, "lines": [g[0] for g in GUIDE], "running": True}
        for i, (line, kind, arg) in enumerate(GUIDE):
            self.state["guide"]["step"] = i + 1
            self.publish({"type": "session"})
            if kind == "jump":
                self.state["timeline"]["pause_for_humans"] = True
                self.play(user_id, target=arg)
                await asyncio.sleep(0.1 if not self.fast else 0)
                while self.state["timeline"]["playing"]:
                    await asyncio.sleep(0.05 if not self.fast else 0)
                await self.wait_idle()
            elif kind == "agents":
                await self.wait_idle()
            elif kind == "act":
                for _ in range(60):
                    await self.wait_idle()
                    open_ = [c for c in self.state["cases"].values() if c["state"] in arg]
                    if not open_:
                        break
                    self._autopilot_flag = True
                    self.autopilot_step()
                    self._autopilot_flag = False
                    await asyncio.sleep(0.6 if not self.fast else 0)
                await self.wait_idle()
            await asyncio.sleep(dwell)
        self.state["guide"]["running"] = False
        self.save()
        self.publish({"type": "session"})


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

    def session(self, ds_id, sc_id):
        if sc_id not in SCENARIOS:
            raise NotFound("Unknown scenario %s" % sc_id)
        key = (ds_id, sc_id)
        if key not in self.sessions:
            ds = self.registry.get(ds_id)
            folder = os.path.join(self.state_root, ds_id, sc_id) if self.state_root else None
            self.sessions[key] = Session(self, ds, sc_id, folder)
        return self.sessions[key]

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
                                 case_id=case_id, before=before, after=after, reason=reason, agent_mode=self.llm.mode.upper(),
                                 autopilot=autopilot and typ == "USER")

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
                continue
            case = s.state["cases"].get(e["case_id"])
            if not case or not case.get("seal") or case["seal"]["case_hash"] != (e.get("after") or {}).get("case_hash"):
                continue          # superseded by a reset - the sealed event stays in the chain
            h = s.case_hash(case, upto_event=e["event_id"])
            files_ok = all(sha256_text(open(f["path"], encoding="utf-8").read()) == f["sha256"] if f.get("path") and os.path.exists(f["path"]) else True
                           for f in case["outputs"]["files"].values())
            ok = h["case_hash"] == case["seal"]["case_hash"] and files_ok
            seals.append({"case_id": case["case_id"], "dataset_id": e["dataset_id"], "scenario_id": e["scenario_id"], "ok": ok})
            if not ok and broken is None:
                broken = {"case_id": case["case_id"], "reason": "case_hash does not match the sealed audit events, facts and outputs"}
        return {"ok": chain["ok"] and broken is None, "chain": chain, "seals": seals, "first_broken_seal": broken}
