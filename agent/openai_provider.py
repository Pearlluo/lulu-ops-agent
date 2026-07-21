"""OpenAI-compatible provider — canonical conversation -> /chat/completions.

Covers OpenAI, and (via base_url) any OpenAI-compatible server: DeepSeek, ollama, vllm, etc.
Canonical JSON-Schema tool defs are wrapped into the `function` tool format; tool results
become role="tool" messages. Temperature IS applied here (these APIs support it).
"""

import json

import requests

from llm_provider import LLMProvider, LLMResponse, ToolCall

TIMEOUT = 120


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model, config, name=None):
        super().__init__(model, config)
        if name:
            self.name = name
        self.base_url = (config or {}).get("base_url", "https://api.openai.com/v1").rstrip("/")

    # ---------- canonical -> openai wire ----------
    @staticmethod
    def _to_wire(system, messages):
        wire = [{"role": "system", "content": system}]
        for m in messages:
            role = m["role"]
            if role == "user":
                wire.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                msg = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{
                        "id": tc["id"], "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                    } for tc in m["tool_calls"]]
                wire.append(msg)
            elif role == "tool_results":
                for r in m["results"]:
                    wire.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        return wire

    @staticmethod
    def _tools_to_wire(tools):
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}} for t in tools]

    def chat(self, system, messages, tools=None, max_tokens=4096, temperature=None) -> LLMResponse:
        body = {"model": self.model,
                "messages": self._to_wire(system, messages),
                "max_tokens": max_tokens}
        if tools:
            body["tools"] = self._tools_to_wire(tools)
        if temperature is not None:
            body["temperature"] = temperature

        def _post(b):
            return requests.post(f"{self.base_url}/chat/completions",
                                 headers={"Authorization": f"Bearer {self.api_key}",
                                          "Content-Type": "application/json"},
                                 json=b, timeout=TIMEOUT)

        resp = _post(body)
        # newer OpenAI models (gpt-5*/o*) want max_completion_tokens and fixed temperature —
        # absorb those parameter-shape differences here, never in business logic
        for _ in range(2):
            if resp.status_code != 400:
                break
            err = resp.text
            if "max_tokens" in err and "max_completion_tokens" in err and "max_tokens" in body:
                body["max_completion_tokens"] = body.pop("max_tokens")
            elif "temperature" in err and "temperature" in body:
                body.pop("temperature")
            else:
                break
            resp = _post(body)
        if resp.status_code >= 400:
            raise RuntimeError(f"{self.name} HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]

        calls = []
        for tc in (msg.get("tool_calls") or []):
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc["id"], tc["function"]["name"], args))

        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            stop="tool_use" if choice.get("finish_reason") == "tool_calls" else "end",
            raw=msg,
            usage={"in": (data.get("usage") or {}).get("prompt_tokens", 0),
                   "out": (data.get("usage") or {}).get("completion_tokens", 0)},
        )
