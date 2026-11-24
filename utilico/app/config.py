"""config/*.json loader (re-read when a file changes on disk)."""
import json
import os

from .util import CONFIG_DIR


class Config:
    def __init__(self, folder=CONFIG_DIR, overrides=None):
        self.folder = folder
        self._cache = {}
        self.overrides = overrides or {}       # e.g. {"agent.json": {"step_latency_ms": 0}} for the self-test

    def get(self, name):
        path = os.path.join(self.folder, name)
        mtime = os.path.getmtime(path)
        hit = self._cache.get(name)
        if not hit or hit[0] != mtime:
            with open(path, "r", encoding="utf-8") as fh:
                self._cache[name] = (mtime, json.load(fh))
        data = self._cache[name][1]
        if name in self.overrides:
            data = dict(data, **self.overrides[name])
        return data

    def users(self):
        return self.get("users.json")["users"]

    def user(self, user_id):
        return next((u for u in self.users() if u["user_id"] == user_id), None)

    def actors(self):
        return self.get("users.json")["actors"]

    def screens(self):
        return self.get("users.json")["screens"]

    def level(self, persona, screen):
        return self.screens().get(screen, {}).get(persona, "-")

    def agent(self):
        return self.get("agent.json")

    def sla(self):
        return self.get("sla.json")

    def triage(self):
        return self.get("triage.json")

    def freshness(self):
        return self.get("freshness.json")

    def rules(self):
        return self.get(os.path.join("rules", "v1.0.json"))

    def workflow(self):
        return self.get("workflow.json")

    def seed_history(self):
        return self.get("seed_case_history.json")["cases"]

    def expected(self):
        return self.get("expected_results.json")
