"""Anthropic provider — canonical conversation -> Claude Messages API (official SDK).

Notes per current API surface:
- adaptive thinking on (recommended for 4.6+); temperature is NOT sent (removed on Opus 4.7+/4.8 — would 400).
- canonical tool defs are already JSON-Schema shaped, which matches Anthropic's native format.
- `_raw` round-trip: we echo our own native content blocks (incl. thinking signatures) when
  continuing a tool loop, so multi-turn tool use stays valid.
"""

from llm_provider import LLMProvider, LLMResponse, ToolCall


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model, config):
        super().__init__(model, config)
        self._client = None

    def _cli(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    # ---------- canonical -> anthropic wire ----------
    @staticmethod
    def _to_wire(messages):
        wire = []
        for m in messages:
            role = m["role"]
            if role == "user":
                wire.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                raw = (m.get("_raw") or {}).get("anthropic")
                if raw is not None:
                    wire.append({"role": "assistant", "content": raw})
                else:
                    blocks = []
                    if m.get("content"):
                        blocks.append({"type": "text", "text": m["content"]})
                    for tc in m.get("tool_calls", []):
                        blocks.append({"type": "tool_use", "id": tc["id"],
                                       "name": tc["name"], "input": tc["args"]})
                    wire.append({"role": "assistant", "content": blocks or m.get("content", "")})
            elif role == "tool_results":
                wire.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"],
                     "content": r["content"], "is_error": bool(r.get("is_error"))}
                    for r in m["results"]]})
        return wire

    def chat(self, system, messages, tools=None, max_tokens=4096, temperature=None) -> LLMResponse:
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=self._to_wire(messages),
        )
        if tools:
            kwargs["tools"] = tools          # canonical == anthropic native
        # temperature intentionally ignored: removed on Opus 4.7+/4.8 (400 if sent)
        resp = self._cli().messages.create(**kwargs)

        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        return LLMResponse(
            text=text,
            tool_calls=calls,
            stop="tool_use" if resp.stop_reason == "tool_use" else "end",
            raw=[b.model_dump() for b in resp.content],   # for _raw echo (preserves thinking sigs)
            usage={"in": resp.usage.input_tokens, "out": resp.usage.output_tokens,
                   "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0)},
        )
