"""Evidence store (prompt §14): state/<dataset>/<scenario>/evidence.jsonl, shared by all agents.

Each fact: fact_id, case_id, system, file, record_id, fields, value, as_of, retrieved_at, rule_set_version, agent, hash.
A record retrieved twice for the same case is the same fact (deduplicated by case + file + record id + kind).
"""
import json
import os

from .util import RULE_SET_VERSION, canon, now_utc, sha256_text


class EvidenceStore:
    def __init__(self, path=None):
        self.path = path
        self.facts = []
        self.by_id = {}
        self._key = {}
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        f = json.loads(line)
                        self._index(f)

    def _index(self, f):
        self.facts.append(f)
        self.by_id[f["fact_id"]] = f
        self._key[(f["case_id"], f["file"], f["record_id"], f.get("kind", "record"))] = f

    def next_id(self):
        return "F-%04d" % (len(self.facts) + 1)

    def find(self, case_id, file, record_id, kind="record"):
        return self._key.get((case_id, file, record_id, kind))

    def add(self, case_id, system, file, record_id, fields, value, as_of, agent, kind="record"):
        hit = self.find(case_id, file, record_id, kind)
        if hit:
            return hit, False
        f = {"fact_id": self.next_id(), "case_id": case_id, "system": system, "file": file, "record_id": record_id, "kind": kind,
             "fields": fields, "value": value, "as_of": as_of, "retrieved_at": now_utc(), "rule_set_version": RULE_SET_VERSION,
             "agent": agent}
        f["hash"] = sha256_text(canon({k: v for k, v in f.items() if k != "hash"}))
        self._index(f)
        if self.path:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(f, ensure_ascii=False, default=str) + "\n")
        return f, True

    def for_case(self, case_id):
        return [f for f in self.facts if f["case_id"] == case_id]

    def verify(self, fact):
        return sha256_text(canon({k: v for k, v in fact.items() if k != "hash"})) == fact.get("hash")
