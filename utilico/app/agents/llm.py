"""LLM abstraction (prompt §11).

LLMClient.run_turn(system_prompt, messages, tools) -> {"tool_calls": [{"id", "name", "input"}]} | {"final": <json>}

MockLLMClient is a deterministic scripted policy. OpenAICompatibleLLMClient calls a configured Chat Completions API.
"""
import json
import math
import os
import re
from urllib.parse import urlsplit

import httpx

from .. import guard
from ..util import ROOT
from .tools import GuardrailError

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


def model_settings(path):
    settings = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            for number, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, sep, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    raise ValueError("Invalid .env setting on line %d" % number)
                if value.startswith(("'", '"')):
                    if len(value) < 2 or value[-1] != value[0]:
                        raise ValueError("Unclosed .env value on line %d" % number)
                    value = value[1:-1]
                else:
                    value = value.split(" #", 1)[0].rstrip()
                if key.startswith("MODEL_"):
                    settings[key] = value
    for key in ("MODEL_API_BASE_URL", "MODEL_NAME", "MODEL_API_KEY", "MODEL_TIMEOUT_SECONDS"):
        if key in os.environ:
            settings[key] = os.environ[key]
    return settings


class OpenAICompatibleLLMClient(LLMClient):
    mode = "openai-compatible"

    def __init__(self, settings):
        required = ("MODEL_API_BASE_URL", "MODEL_NAME", "MODEL_API_KEY")
        missing = [k for k in required if not settings.get(k, "").strip()]
        if missing:
            raise ValueError("Missing model setting(s): %s" % ", ".join(missing))
        base = settings["MODEL_API_BASE_URL"].strip().rstrip("/")
        url = urlsplit(base)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or
                url.query or url.fragment or url.path.endswith("/chat/completions")):
            raise ValueError("MODEL_API_BASE_URL must be an HTTP(S) base URL without credentials, query, or /chat/completions")
        if url.scheme != "https" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("MODEL_API_BASE_URL must use HTTPS except for localhost")
        self.endpoint = base + "/chat/completions"
        self.name = settings["MODEL_NAME"].strip()
        self.key = settings["MODEL_API_KEY"].strip()
        try:
            self.timeout = float(settings.get("MODEL_TIMEOUT_SECONDS", "30"))
        except ValueError:
            raise ValueError("MODEL_TIMEOUT_SECONDS must be a positive number") from None
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("MODEL_TIMEOUT_SECONDS must be a positive number")

    @staticmethod
    def _content(value):
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    def run_turn(self, system_prompt, messages, tools):
        converted = [{"role": "system", "content": system_prompt}]
        for message in messages:
            role = message["role"]
            if role == "assistant" and not message.get("tool_calls"):
                # an earlier final answer the server rejected (sent back once for repair)
                converted.append({"role": "assistant", "content": self._content(message.get("content") or {})})
            elif role == "assistant":
                converted.append({"role": "assistant", "content": None, "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": self._content(c.get("input") or {})}}
                    for c in message.get("tool_calls", [])]})
            elif role == "tool":
                converted.append({"role": "tool", "tool_call_id": message["tool_call_id"],
                                  "content": self._content(message["content"])})
            else:
                converted.append({"role": role, "content": self._content(message["content"])})
        payload = {"model": self.name, "messages": converted, "response_format": {"type": "json_object"}}
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
            payload["tool_choice"] = "auto"
        try:
            with guard.model_connection():
                response = httpx.post(self.endpoint, json=payload, headers={"Authorization": "Bearer " + self.key},
                                      timeout=self.timeout, follow_redirects=False)
            if response.status_code != 200:
                raise GuardrailError("Model API returned HTTP %d" % response.status_code)
            result = response.json()
        except httpx.TimeoutException:
            raise GuardrailError("Model API request timed out") from None
        except httpx.RequestError:
            raise GuardrailError("Model API connection failed") from None
        except ValueError:
            raise GuardrailError("Model API returned invalid JSON") from None
        if not isinstance(result, dict) or not isinstance(result.get("choices"), list) or not result["choices"]:
            raise GuardrailError("Model API response has no choices")
        choice = result["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise GuardrailError("Model API response has no message")
        msg = choice["message"]
        if msg.get("tool_calls"):
            allowed = {t["name"] for t in tools}
            mapped = []
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict) or not isinstance(tc.get("function"), dict):
                    raise GuardrailError("Model returned an invalid tool call")
                fn = tc["function"]
                if not isinstance(tc.get("id"), str) or not tc["id"] or fn.get("name") not in allowed:
                    raise GuardrailError("Model returned an invalid or unavailable tool call")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (TypeError, ValueError):
                    raise GuardrailError("Model returned invalid tool arguments") from None
                if not isinstance(args, dict):
                    raise GuardrailError("Model tool arguments must be a JSON object")
                mapped.append({"id": tc["id"], "name": fn["name"], "input": args})
            return {"tool_calls": mapped}
        try:
            final = json.loads(msg.get("content") or "")
        except (TypeError, ValueError):
            raise GuardrailError("Model final response must be valid JSON") from None
        if not isinstance(final, dict):
            raise GuardrailError("Model final response must be a JSON object")
        return {"final": final}


def make_llm_client(cfg):
    mode = cfg.agent().get("mode", "mock")
    if mode == "mock":
        return MockLLMClient()
    if mode == "claude":
        return ClaudeLLMClient()
    if mode == "openai-compatible":
        return OpenAICompatibleLLMClient(model_settings(os.path.join(ROOT, ".env")))
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
