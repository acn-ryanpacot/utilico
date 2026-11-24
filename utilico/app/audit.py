"""Global append-only audit log with a SHA-256 hash chain (prompt §14). There is no edit or delete path.

hash = SHA-256(prev_hash + canonical JSON of the event without its hash). Case seals are verified by the engine,
which recomputes case_hash from the case's audit events, evidence facts and output files.
"""
import json
import os
import threading
import uuid

from .util import GENESIS_HASH, canon, now_local, now_utc, sha256_text

FIELDS = ["event_id", "timestamp_utc", "timestamp_local", "scenario_time", "dataset_id", "scenario_id", "actor_type", "actor_id",
          "actor_name", "actor_persona", "action", "object_type", "object_id", "case_id", "before", "after", "reason", "agent_mode",
          "autopilot", "prev_hash", "hash"]


def event_hash(prev_hash, ev):
    body = {k: v for k, v in ev.items() if k != "hash"}
    return sha256_text(prev_hash + canon(body))


class AuditLog:
    def __init__(self, path=None):
        self.path = path
        self.events = []
        self.lock = threading.Lock()
        self.listeners = []
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self.events.append(json.loads(line))

    @property
    def last_hash(self):
        return self.events[-1]["hash"] if self.events else GENESIS_HASH

    def append(self, **kw):
        with self.lock:
            ev = {k: kw.get(k) for k in FIELDS if k not in ("event_id", "timestamp_utc", "timestamp_local", "prev_hash", "hash")}
            ev = dict({"event_id": "EV-" + uuid.uuid4().hex[:12], "timestamp_utc": now_utc(), "timestamp_local": now_local()}, **ev)
            ev["autopilot"] = bool(kw.get("autopilot"))
            ev["agent_mode"] = kw.get("agent_mode") or "MOCK"
            ev["prev_hash"] = self.last_hash
            ev["hash"] = event_hash(ev["prev_hash"], ev)
            self.events.append(ev)
            if self.path:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    def verify_chain(self):
        """Recompute every link (from the file when there is one, so on-disk tampering is detected)."""
        evs = self.events
        if self.path and os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                evs = []
                for i, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        evs.append(json.loads(line))
                    except ValueError:
                        return {"ok": False, "events": len(evs), "first_broken": {"line": i, "event_id": None, "reason": "line is not valid JSON"}}
        prev = GENESIS_HASH
        for i, ev in enumerate(evs, 1):
            if ev.get("prev_hash") != prev:
                return {"ok": False, "events": len(evs), "first_broken": {"line": i, "event_id": ev.get("event_id"), "reason": "prev_hash does not match the previous event"}}
            if event_hash(prev, ev) != ev.get("hash"):
                return {"ok": False, "events": len(evs), "first_broken": {"line": i, "event_id": ev.get("event_id"), "reason": "event content does not match its hash"}}
            prev = ev["hash"]
        return {"ok": True, "events": len(evs), "first_broken": None}

    def query(self, f, limit=500):
        q = (f.get("q") or "").lower()

        def keep(e):
            for k, field in (("user", "actor_id"), ("action", "action"), ("case", "case_id"), ("scenario", "scenario_id"), ("dataset", "dataset_id")):
                if f.get(k) and e.get(field) != f[k]:
                    return False
            if f.get("persona") and e.get("actor_persona") != f["persona"] and e.get("actor_type") != f["persona"]:
                return False
            if f.get("from") and (e.get("timestamp_local") or "")[:10] < f["from"]:
                return False
            if f.get("to") and (e.get("timestamp_local") or "")[:10] > f["to"]:
                return False
            if f.get("case_ids") is not None and e.get("case_id") not in f["case_ids"]:
                return False
            if q and q not in json.dumps(e, ensure_ascii=False, default=str).lower():
                return False
            return True
        out = [e for e in list(self.events) if keep(e)]
        return {"total": len(out), "events": out[::-1][:limit]}

    def purge(self):
        """Command line only (--purge-log)."""
        with self.lock:
            self.events = []
            if self.path and os.path.exists(self.path):
                os.remove(self.path)
