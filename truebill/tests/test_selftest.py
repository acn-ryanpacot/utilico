"""The in-app Self-test (§18, §19, guardrails, SoD, freshness, seals, offline) must pass for both datasets."""
from app.selftest import SelfTest


def test_selftest_all_pass(registry, kb):
    hashes = {d["id"]: registry.get(d["id"]).source_hashes() for d in registry.list()}
    r = SelfTest(registry, kb, hashes).run()
    failed = [(g["name"], x["check"], x["expected"], x["actual"]) for g in r["groups"] for x in g["rows"] if not x["pass"]]
    assert r["pass"], failed[:10]
    assert r["checks"] > 400
