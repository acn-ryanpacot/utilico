"""Event bus and timeline (prompt §7).

Events come from the CSV timestamps (DISCONNECT_COMPLETED, BILL_READY, DISPUTE_OPENED) plus follow-ups the
Orchestrator schedules (DISCONNECT_FOLLOWUP_DUE = disconnect + followup_hours). Each scenario has a scenario clock;
the runner advances it (default one scenario-day per real second), processes due events in order, waits while
agents work, and - with "Pause for humans" on - freezes while any case waits for a person.
"""
import asyncio

from .datasource import F_BILL, F_CRM, F_WO
from .util import ts_add

SCENARIOS = {
    "PREVENTION_REPLAY": {"label": "Prevention replay (pre-bill checks + disconnect follow-up)", "start": "2026-08-01 00:00",
                          "end": "2026-11-02 23:59", "active": ["DISCONNECT_COMPLETED", "BILL_READY", "DISCONNECT_FOLLOWUP_DUE"]},
    "DISPUTE_REPLAY": {"label": "Dispute replay (as it actually happened)", "start": "2026-11-02 00:00", "end": "2026-11-10 23:59",
                       "active": ["DISPUTE_OPENED"]},
    "BACKSCAN_2026-11-10": {"label": "Back-scan remediation (as of 2026-11-10)", "start": "2026-11-10 23:59", "end": "2026-11-10 23:59",
                            "active": []},
}
PRESETS = {
    "prevention_start": {"label": "Prevention: start", "scenario": "PREVENTION_REPLAY", "reset": True, "target": None},
    "prevention_nov2": {"label": "Prevention: jump to 2 Nov bill run", "scenario": "PREVENTION_REPLAY", "target": "2026-11-02 01:00"},
    "dispute_harbourline": {"label": "Dispute: Harbourline dispute (4 Nov 09:40)", "scenario": "DISPUTE_REPLAY", "target": "2026-11-04 09:40"},
    "dispute_regulatory": {"label": "Dispute: regulatory complaint (9 Nov 15:30)", "scenario": "DISPUTE_REPLAY", "target": "2026-11-09 15:30"},
}
ORDER = {"BILL_READY": 0, "DISCONNECT_COMPLETED": 1, "DISCONNECT_FOLLOWUP_DUE": 2, "DISPUTE_OPENED": 3}


def build_events(ds, sc_id, rules_cfg):
    sc = SCENARIOS[sc_id]
    lo, hi = sc["start"], sc["end"]
    out = []
    if "DISCONNECT_COMPLETED" in sc["active"]:
        for w in ds.table(F_WO):
            if w["wo_type"] == "DISCONNECT" and w["status"] == "COMPLETED" and w.get("completed_at") and lo <= w["completed_at"] <= hi:
                out.append({"type": "DISCONNECT_COMPLETED", "at": w["completed_at"], "ref": w["wo_id"],
                            "label": "%s %s %s" % (w["wo_id"], w["wo_subtype"], w["oms_premise_id"])})
    if "BILL_READY" in sc["active"]:
        for r in sorted({b["bill_run_at"] for b in ds.table(F_BILL) if b.get("bill_run_at")}):
            if lo <= r <= hi:
                out.append({"type": "BILL_READY", "at": r, "ref": r, "label": "Bill run %s" % r})
    if "DISPUTE_OPENED" in sc["active"]:
        for c in ds.table(F_CRM):
            if c["category"] in rules_cfg["dispute_categories"] and lo <= c["opened_at"] <= hi:
                out.append({"type": "DISPUTE_OPENED", "at": c["opened_at"], "ref": c["case_id"], "label": "%s %s" % (c["case_id"], c["category"])})
    out.sort(key=lambda e: (e["at"], ORDER[e["type"]], e["ref"]))
    for i, e in enumerate(out, 1):
        e.update(event_id="EVT-%03d" % i, status="PENDING")
    return out


class TimelineMixin:
    """Scenario clock and runner for one Session."""

    TICK = 0.1

    def pending_events(self):
        return sorted([e for e in self.state["events"] if e["status"] in ("PENDING", "BLOCKED")], key=lambda e: (e["at"], ORDER[e["type"]], e["event_id"]))

    def schedule(self, typ, at, ref, label):
        n = len(self.state["events"]) + 1
        ev = {"type": typ, "at": at, "ref": ref, "label": label, "event_id": "EVT-%03d" % n, "status": "PENDING", "scheduled": True}
        self.state["events"].append(ev)
        return ev

    def timeline_view(self):
        tl = self.state["timeline"]
        sc = SCENARIOS[self.sc_id]
        return {"clock": self.clock, "start": sc["start"], "end": sc["end"], "playing": tl["playing"], "speed": tl["speed"],
                "pause_for_humans": tl["pause_for_humans"], "paused_reason": tl.get("paused_reason"), "target": tl.get("target"),
                "events": [{k: e.get(k) for k in ("event_id", "type", "at", "ref", "label", "status", "scheduled", "case_id")} for e in
                           sorted(self.state["events"], key=lambda e: (e["at"], ORDER[e["type"]]))],
                "toggles": self.state["toggles"], "banner": self.state.get("banner"), "guide": self.state.get("guide")}

    def waiting_for_humans(self):
        hs = self.cfg.workflow()["human_states"]
        return [c for c in self.state["cases"].values() if c["state"] in hs]

    # ---- controls (logged) --------------------------------------------------------------------------
    def play(self, user_id, target=None, speed=None):
        tl = self.state["timeline"]
        if speed:
            tl["speed"] = speed
        tl["target"] = target
        if not tl["playing"]:
            tl["playing"] = True
            self.log(user_id, "TIMELINE_PLAY", "timeline", self.sc_id, after={"clock": self.clock, "target": target, "speed": tl["speed"]})
        self.save()
        self.ensure_runner()

    def pause(self, user_id, reason=None):
        tl = self.state["timeline"]
        if tl["playing"]:
            tl["playing"] = False
            tl["target"] = None
            self.log(user_id, "TIMELINE_PAUSE", "timeline", self.sc_id, after={"clock": self.clock}, reason=reason)
        self.save()
        self.publish_timeline()

    async def step(self, user_id):
        """Advance the clock to the next event and process it."""
        await self.wait_idle()
        ev = next(iter(self.pending_events()), None)
        if not ev:
            return None
        self.log(user_id, "TIMELINE_STEP", "timeline", self.sc_id, before={"clock": self.clock}, after={"clock": max(self.clock, ev["at"]), "event": ev["event_id"]})
        self.clock = max(self.clock, ev["at"])
        await self.process_event(ev)
        self.save()
        self.publish_timeline()
        return ev

    def ensure_runner(self):
        if self._runner is None or self._runner.done():
            self._runner = self.spawn(self._run_loop(), track=False)

    async def _run_loop(self):
        tl = self.state["timeline"]
        end = SCENARIOS[self.sc_id]["end"]
        try:
            while tl["playing"]:
                if not self.idle():
                    await asyncio.sleep(0.02 if not self.fast else 0)
                    continue
                pend = next(iter(self.pending_events()), None)
                if tl.get("target") and self.clock >= tl["target"] and not (pend and pend["at"] <= self.clock):
                    tl["playing"], tl["paused_reason"], tl["target"] = False, "target", None
                    self.save()
                    break
                if tl["pause_for_humans"] and self.waiting_for_humans():
                    if tl.get("paused_reason") != "humans":
                        tl["paused_reason"] = "humans"
                        self.publish_timeline()
                    await asyncio.sleep(0.05 if not self.fast else 0)
                    continue
                if tl.get("paused_reason") == "humans":
                    tl["paused_reason"] = None
                ev = next(iter(self.pending_events()), None)
                if ev and ev["status"] == "BLOCKED" and self.state["toggles"]["stale_mdm"]:
                    tl["playing"], tl["paused_reason"] = False, "stale"
                    self.save()
                    self.publish_timeline()
                    break
                goal = min(ev["at"] if ev else end, tl.get("target") or end)
                if self.fast or tl.get("target"):
                    new = goal
                else:
                    new = min(goal, ts_add(self.clock, minutes=max(1, round(24 * 60 * float(tl["speed"]) * self.TICK))))
                if new > self.clock:
                    self.clock = new
                if ev and self.clock >= ev["at"]:
                    await self.process_event(ev)
                    self.save()
                nxt = next(iter(self.pending_events()), None)
                due_now = nxt is not None and nxt["at"] <= self.clock
                if (not nxt and self.clock >= end) or (tl.get("target") and self.clock >= tl["target"] and not due_now):
                    tl["playing"] = False
                    tl["paused_reason"] = "end" if not tl.get("target") else "target"
                    tl["target"] = None
                    self.save()
                self.publish_timeline()
                await asyncio.sleep(0 if (self.fast or tl.get("target")) else self.TICK)
        finally:
            self.publish_timeline()
