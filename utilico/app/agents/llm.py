"""LLM abstraction (prompt §11).

LLMClient.run_turn(system_prompt, messages, tools) -> {"tool_calls": [{"id", "name", "input"}]} | {"final": <json>}

MockLLMClient is the implementation used: a deterministic, scripted policy per agent that reads the tool results
in the conversation and decides the next tool call or the final JSON. Record text is data - the only text it
inspects is the listed CRM signal-phrase matching. ClaudeLLMClient is the stub for the real model.
"""

POLICIES = {}       # agent id -> policy(messages, tools, brief) -> {"tool_calls": [...]} | {"final": {...}}


def policy(agent_id):
    def deco(fn):
        POLICIES[agent_id] = fn
        return fn
    return deco


class LLMClient:
    mode = "abstract"

    def run_turn(self, system_prompt, messages, tools):
        raise NotImplementedError


class MockLLMClient(LLMClient):
    mode = "mock"

    def run_turn(self, system_prompt, messages, tools):
        agent_id = system_prompt.split("\n", 1)[0].split("AGENT:", 1)[1].strip().split(" ")[0]
        fn = POLICIES.get(agent_id)
        if fn is None:
            raise RuntimeError("no mock policy for %s" % agent_id)
        brief = messages[0]["content"] if messages else {}
        return fn(messages, {t["name"] for t in tools}, brief)


class ClaudeLLMClient(LLMClient):
    """Stub. A real implementation would call the Anthropic Messages API with tool use:
    client.messages.create(model=..., system=system_prompt, messages=messages, tools=[{"name", "description",
    "input_schema"}...]) and map `tool_use` content blocks to {"tool_calls": [...]} and a final text/JSON block to
    {"final": ...}. Tool execution, guardrails, point-in-time reads, validation and logging stay in the server,
    so swapping it in only needs this class plus config/agent.json {"mode": "claude"}. Not used: the POC is offline.
    """
    mode = "claude"

    def __init__(self, *a, **kw):
        raise NotImplementedError("ClaudeLLMClient is a stub - the POC runs the deterministic MockLLMClient offline")


def make_llm_client(cfg):
    mode = cfg.agent().get("mode", "mock")
    if mode == "mock":
        return MockLLMClient()
    if mode == "claude":
        return ClaudeLLMClient()
    raise ValueError("unknown agent mode %r" % mode)


def tool_results(messages):
    """[(tool_name, input, result)] in order, from the conversation."""
    out, pending = [], {}
    for m in messages:
        if m["role"] == "assistant":
            for c in m.get("tool_calls", []):
                pending[c["id"]] = c
        elif m["role"] == "tool":
            c = pending.get(m["tool_call_id"], {})
            out.append((c.get("name"), c.get("input"), m["content"]))
    return out


def call(name, n, **inp):
    return {"id": "call_%s_%d" % (name, n), "name": name, "input": inp}
