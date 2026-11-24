import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agents.llm import MockLLMClient, OpenAICompatibleLLMClient, model_settings
from app.agents.models import RetrievalOut
from app.agents.runner import fill_server_fields, run_agent, validate_live_args, validate_live_output
from app.agents.tools import ArgumentRejected, GuardrailError, specs


def client():
    return OpenAICompatibleLLMClient({
        "MODEL_API_BASE_URL": "https://example.test/v1",
        "MODEL_NAME": "test-model", "MODEL_API_KEY": "test-key", "MODEL_TIMEOUT_SECONDS": "10",
    })


def test_env_settings_preserve_process_override(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('MODEL_NAME="file-model"\nMODEL_API_KEY=file-key\n', encoding="utf-8")
    monkeypatch.setenv("MODEL_NAME", "process-model")
    settings = model_settings(p)
    assert settings["MODEL_NAME"] == "process-model"
    assert settings["MODEL_API_KEY"] == "file-key"


def test_chat_completion_maps_calls_and_history(monkeypatch):
    received = []

    def fake_post(url, **kwargs):
        received.append((url, kwargs))
        if len(received) == 1:
            data = {"choices": [{"message": {"tool_calls": [{"id": "c1", "type": "function",
                    "function": {"name": "get_meter", "arguments": '{"meter_id":"M1"}'}}]}}]}
        else:
            data = {"choices": [{"message": {"content": '{"lane":"MDM","fact_ids":[],"records":0}'}}]}
        return httpx.Response(200, json=data)

    monkeypatch.setattr(httpx, "post", fake_post)
    model = client()
    tools = specs(["get_meter"])
    messages = [{"role": "user", "content": {"lane": "MDM"}}]
    reply = model.run_turn("prompt", messages, tools)
    assert reply == {"tool_calls": [{"id": "c1", "name": "get_meter", "input": {"meter_id": "M1"}}]}
    messages.extend([{"role": "assistant", "tool_calls": reply["tool_calls"]},
                     {"role": "tool", "tool_call_id": "c1", "content": {"fact_id": "F1"}}])
    assert model.run_turn("prompt", messages, tools)["final"]["lane"] == "MDM"
    url, request = received[-1]
    assert url == "https://example.test/v1/chat/completions"
    assert request["json"]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": '{"fact_id": "F1"}'}
    assert request["json"]["tools"][0]["function"]["parameters"]["required"] == ["meter_id"]


def test_model_errors_are_explicit_and_do_not_include_response(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401, text="sensitive provider response"))
    with pytest.raises(GuardrailError, match="HTTP 401") as exc:
        client().run_turn("prompt", [], [])
    assert "sensitive provider response" not in str(exc.value)


def test_unlimited_tool_calls_and_explicit_limit(monkeypatch):
    from app.agents import runner

    class LLM:
        mode = "mock"

        def __init__(self):
            self.count = 0

        def run_turn(self, system, messages, tools):
            self.count += 1
            if self.count <= 20:
                return {"tool_calls": [{"id": str(self.count), "name": "get_case_packet", "input": {}}]}
            return {"final": {"lane": "OMS", "fact_ids": [], "records": 0}}

    monkeypatch.setitem(runner.READ, "get_case_packet", lambda ctx, args: ({}, "ok"))
    cfg = SimpleNamespace(agent=lambda: {"max_tool_calls": None})
    session = SimpleNamespace(cfg=cfg, llm=LLM(), evidence=SimpleNamespace(by_id={}),
                              latency=lambda: asyncio.sleep(0), log_tool=lambda *args: None)
    out = asyncio.run(run_agent(session, {}, "agent.retrieval", {}, ["get_case_packet"], RetrievalOut))
    assert out["lane"] == "OMS"
    assert session.llm.count == 21
    session.cfg = SimpleNamespace(agent=lambda: {"max_tool_calls": 2})
    session.llm = LLM()
    with pytest.raises(GuardrailError, match="exceeded 2 tool calls"):
        asyncio.run(run_agent(session, {}, "agent.retrieval", {}, ["get_case_packet"], RetrievalOut))


def test_live_recommendation_requires_calculated_amount():
    session = SimpleNamespace(evidence=SimpleNamespace(for_case=lambda case_id: [{"fact_id": "F1"}]))
    case = {"case_id": "C1"}
    brief = {"type": "PREVENT", "match_status": "CONFIRMED", "subtype": "STOP_SERVICE"}
    rules = {"cause": "C1", "cause_label": "Integration", "s_codes": ["S1"], "rule_set_version": "v1", "fired": ["PB1"]}
    out = {"fact_ids": ["F1"], "probable_cause": "C1", "cause_label": "Integration", "s_codes": ["S1"], "rule_set_version": "v1",
           "amount": 999, "amount_kind": "reduction", "amount_fact_id": "F1", "option": "PRORATE", "alternatives": [],
           "calculation": {"action": "REGENERATE", "before": 20, "after": 10}}
    results = [("run_rules", {}, rules), ("calculate", {"kind": "draft"},
               {"amount": 10, "fact_id": "F1", "action": "REGENERATE", "before": 20, "after": 10})]
    with pytest.raises(GuardrailError, match="does not match the calculator"):
        validate_live_output(session, case, "agent.recommendation", brief, out, results)
    out["amount"] = 10
    validate_live_output(session, case, "agent.recommendation", brief, out, results)


def test_live_tools_cannot_read_other_cases():
    case = {"account_id": "A1", "sa_id": "S1", "meter_id": "M1", "wo_id": "W1",
            "recorded_premise": "P1", "premise": "P1", "bill_ids": ["B1"], "type": "PREVENT", "draft_bill_id": "B1"}
    schema = specs(["get_crm_cases"])[0]["input_schema"]
    validate_live_args(case, "get_crm_cases", {"account_id": "A1"}, schema)
    with pytest.raises(ArgumentRejected, match="current case"):
        validate_live_args(case, "get_crm_cases", {"account_id": "A2"}, schema)
    schema = specs(["calculate"])[0]["input_schema"]
    with pytest.raises(ArgumentRejected, match="case draft bill"):
        validate_live_args(case, "calculate", {"kind": "draft", "bill_id": "B2"}, schema)
    # a work order on the case's own premise (e.g. the reconnect) is allowed; any other is refused
    schema = specs(["get_integration_messages"])[0]["input_schema"]
    validate_live_args(case, "get_integration_messages", {"wo_id": "W2"}, schema, allowed_wos={"W1", "W2"})
    with pytest.raises(ArgumentRejected, match="work order on the case premise"):
        validate_live_args(case, "get_integration_messages", {"wo_id": "W3"}, schema, allowed_wos={"W1", "W2"})


def test_search_knowledge_schema_lists_collections():
    schema = specs(["search_knowledge"])[0]["input_schema"]
    assert schema["properties"]["collection"]["enum"] == ["case_history", "rules", "thresholds"]
    case = {"account_id": "A1", "sa_id": "S1", "meter_id": "M1", "wo_id": "W1", "recorded_premise": "P1", "premise": "P1", "bill_ids": []}
    with pytest.raises(ArgumentRejected, match="use one of case_history, rules, thresholds"):
        validate_live_args(case, "search_knowledge", {"query": "q", "collection": "cases"}, schema)
    with pytest.raises(ArgumentRejected, match="between 1 and 5"):
        validate_live_args(case, "search_knowledge", {"query": "q", "k": 50}, schema)


def test_server_fills_fixed_fields_and_explanations_may_add_case_facts():
    session = SimpleNamespace(evidence=SimpleNamespace(for_case=lambda case_id: [{"fact_id": f} for f in ("F1", "F2", "F3")]))
    case = {"case_id": "C1"}
    brief = {"type": "REMEDIATE", "match_status": "NONE", "subtype": "STOP_SERVICE"}
    rules = {"cause": "C2", "cause_label": "Integration failure; To Do TD-1 open and unassigned", "cause_quote": "q", "s_codes": ["S1", "S2"],
             "rule_set_version": "v1", "fired": ["PB2"], "rules": {"PB2": {"fact_ids": ["F1"]}}}
    calc = {"amount": 10.5, "fact_id": "F3", "bill_ids": ["B1"], "lines": [{"bill_id": "B1"}], "credit": 10.5, "kind": "credit"}
    results = [("run_rules", {}, rules), ("calculate", {"kind": "credit"}, calc)]
    # reconciliation: PB2 cites its own fact plus a related case fact -> accepted
    rec = fill_server_fields("agent.reconciliation", brief, {"explanations": [{"rule": "PB2", "text": "t", "fact_ids": ["F1", "F2"]}]}, results)
    validate_live_output(session, case, "agent.reconciliation", brief, rec, results)
    # ... but citing none of its rule's facts is rejected, naming the facts it should cite
    bad = fill_server_fields("agent.reconciliation", brief, {"explanations": [{"rule": "PB2", "text": "t", "fact_ids": ["F2"]}]}, results)
    with pytest.raises(GuardrailError, match=r"PB2 explanation must cite at least one of its rule's facts \(F1\)"):
        validate_live_output(session, case, "agent.reconciliation", brief, bad, results)
    # recommendation: a paraphrased label / wrong S-codes / no amount are overwritten from the tools
    out = fill_server_fields("agent.recommendation", brief, {"cause_label": "integration failed", "s_codes": [], "option": "PRORATE",
                                                            "alternatives": [], "fact_ids": ["F1"]}, results)
    assert out["cause_label"] == rules["cause_label"] and out["s_codes"] == ["S1", "S2"]
    assert out["amount"] == 10.5 and out["amount_kind"] == "credit" and out["amount_fact_id"] == "F3"
    assert out["calculation"] == {"bill_ids": ["B1"], "lines": [{"bill_id": "B1"}], "credit": 10.5}
    assert out["fact_ids"] == ["F1", "F3"]
    validate_live_output(session, case, "agent.recommendation", brief, out, results)
    # a missing calculation tells the model which tool to call
    with pytest.raises(GuardrailError, match="call calculate with kind credit"):
        fill_server_fields("agent.recommendation", brief, {}, results[:1])


class SloppyLiveClient(MockLLMClient):
    """Replays the mistakes a real model made (UT-1001..1005): reads the reconnect work order and another customer's
    account, uses an unknown knowledge collection, cites extra related facts, paraphrases the cause label, leaves out
    server-filled fields and first picks the wrong option."""
    mode = "openai-compatible"

    @staticmethod
    def clean(messages):
        out = []
        for m in messages:
            if m["role"] == "tool" and m["tool_call_id"].startswith("sloppy"):
                continue
            if m["role"] == "assistant":
                tcs = [c for c in m.get("tool_calls") or [] if not c["id"].startswith("sloppy")]
                if not tcs:
                    continue
                m = dict(m, tool_calls=tcs)
            if m["role"] == "user" and out:
                continue
            out.append(m)
        return out

    def run_turn(self, system, messages, tools):
        agent = system.split("\n", 1)[0].split("AGENT:", 1)[1].split(" ")[0]
        brief = messages[0]["content"]
        made = {c["id"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls") or []}
        inject = []
        if agent == "agent.retrieval" and brief["lane"] == "OMS":
            if brief["window"].get("reconnect_wo_id"):
                inject.append(("sloppy_reconnect", "get_integration_messages", {"wo_id": brief["window"]["reconnect_wo_id"]}))
            inject.append(("sloppy_other_wo", "get_integration_messages", {"wo_id": "WO-999999"}))
        if agent == "agent.retrieval" and brief["lane"] == "CRM":
            inject.append(("sloppy_other_account", "get_crm_cases", {"account_id": "8800000000"}))
        if agent == "agent.recommendation":
            inject.append(("sloppy_collection", "search_knowledge", {"query": "similar", "collection": "cases", "k": 3}))
        for cid, name, args in inject:
            if cid not in made:
                return {"tool_calls": [{"id": cid, "name": name, "input": args}]}
        resp = super().run_turn(system, self.clean(messages), tools)
        if "final" not in resp:
            return resp
        final = dict(resp["final"])
        if agent == "agent.retrieval":
            return {"final": {}}
        if agent == "agent.reconciliation":
            every = sorted({f for e in final["explanations"] for f in e["fact_ids"]})
            exps = [dict(e, fact_ids=e["fact_ids"] + [f for f in every if f not in e["fact_ids"]][:1]) for e in final["explanations"]]
            return {"final": {"explanations": exps}}
        repaired = any(m["role"] == "user" for m in messages[1:])
        for key in ("probable_cause", "s_codes", "amount", "amount_kind", "amount_fact_id", "calculation", "rule_set_version"):
            final.pop(key, None)
        final["cause_label"] = "integration problem (paraphrased)"
        if not repaired:
            final["option"] = "SUPPRESS" if final["option"] != "SUPPRESS" else "PRORATE"
        return {"final": final}


def _engine(registry, kb, llm=None):
    from app.audit import AuditLog
    from app.config import Config
    from app.engine import Engine
    cfg = Config(overrides={"agent.json": {"mode": "mock", "step_latency_ms": 0, "max_tool_calls": None}})
    engine = Engine(registry, kb, cfg, state_root=None, audit=AuditLog(None), fast=True)
    if llm:
        engine.llm = llm
    return engine, cfg


def _backscan(engine, cfg):
    session = engine.session("dataset_A_with_errors")

    async def drive():
        session.send_backscan(cfg.user("u.csr"))
        await session.wait_idle()

    asyncio.run(drive())
    return session


def test_sloppy_live_model_reaches_triage_like_the_mock(registry, kb):
    base = _backscan(*_engine(registry, kb))
    engine, cfg = _engine(registry, kb, SloppyLiveClient())
    live = _backscan(engine, cfg)
    want = {c["case_id"]: (c["state"], c["amount"], c["recommendation"]["option"], c["recommendation"]["probable_cause"])
            for c in base.state["cases"].values()}
    got = {c["case_id"]: (c["state"], c["amount"], (c["recommendation"] or {}).get("option"), (c["recommendation"] or {}).get("probable_cause"))
           for c in live.state["cases"].values()}
    assert [c.get("agent_error") for c in live.state["cases"].values()] == [None] * len(want)
    assert got == want and len(want) == 5
    for c in live.state["cases"].values():
        assert c["recommendation"]["cause_label"] == base.state["cases"][c["case_id"]]["recommendation"]["cause_label"]
    events = engine.audit.events
    assert sum(1 for e in events if e["action"] == "AGENT_OUTPUT_REJECTED" and e["after"]["retry"]) == 5
    refused = [e for e in events if e["action"] == "AGENT_TOOL_CALL" and str((e.get("after") or {}).get("result", "")).startswith("refused")]
    assert refused and all(e["after"]["ok"] is False for e in refused)


class StubbornLiveClient(SloppyLiveClient):
    """Never calls calculate, so the recommendation is stopped even after its repair turn."""

    def run_turn(self, system, messages, tools):
        resp = super().run_turn(system, messages, tools)
        if any(c["name"] == "calculate" for c in resp.get("tool_calls") or []):
            return {"final": {"option": "PRORATE"}}
        return resp


def test_stopped_case_can_be_rerun_by_the_csr(registry, kb):
    engine, cfg = _engine(registry, kb, StubbornLiveClient())
    session = engine.session("dataset_A_with_errors")

    async def drive():
        session.send_backscan(cfg.user("u.csr"), ["WO-104277"])
        await session.wait_idle()
        case = next(iter(session.state["cases"].values()))
        assert case["state"] == "RECOMMENDING" and "missing a deterministic calculation" in case["agent_error"]
        assert session.allowed_actions(cfg.user("u.csr"), case) == ["RERUN_AGENTS"]
        assert session.allowed_actions(cfg.user("u.dri"), case) == []
        stop = [e for e in engine.audit.events if e["action"] == "AGENT_TOOL_CALL" and (e.get("after") or {}).get("guardrail")][-1]
        assert stop["after"]["state"] == "RECOMMENDING"
        engine.llm = session.llm = SloppyLiveClient()
        session.act(cfg.user("u.csr"), case["case_id"], "RERUN_AGENTS", {})
        await session.wait_idle()
        return case

    case = asyncio.run(drive())
    assert case["agent_error"] is None and case["state"] == "TRIAGE" and case["agent_retries"] == 1
    assert any(e["action"] == "AGENT_RERUN_REQUESTED" for e in engine.audit.events)


def test_live_pipeline_with_scripted_model_responses(registry, kb):
    from app.agents.llm import MockLLMClient
    from app.audit import AuditLog
    from app.config import Config
    from app.engine import Engine

    class ScriptedLiveClient(MockLLMClient):
        mode = "openai-compatible"

    cfg = Config(overrides={"agent.json": {"mode": "mock", "step_latency_ms": 0, "max_tool_calls": None}})
    engine = Engine(registry, kb, cfg, state_root=None, audit=AuditLog(None), fast=True)
    engine.llm = ScriptedLiveClient()
    session = engine.session("dataset_A_with_errors")

    async def drive():
        session.send_backscan(cfg.user("u.csr"), ["WO-104277"])
        await session.wait_idle()

    asyncio.run(drive())
    case = next(iter(session.state["cases"].values()))
    assert case["agent_error"] is None
    assert case["state"] == "TRIAGE"
    assert case["recommendation"]["amount"] == case["amount"]
