import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agents.llm import OpenAICompatibleLLMClient, model_settings
from app.agents.models import RetrievalOut
from app.agents.runner import run_agent, validate_live_args, validate_live_output
from app.agents.tools import GuardrailError, specs


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
    with pytest.raises(GuardrailError, match="current case"):
        validate_live_args(case, "get_crm_cases", {"account_id": "A2"}, schema)
    schema = specs(["calculate"])[0]["input_schema"]
    with pytest.raises(GuardrailError, match="case draft bill"):
        validate_live_args(case, "calculate", {"kind": "draft", "bill_id": "B2"}, schema)


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
