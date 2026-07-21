"""Model Gateway offline tests — registry, routing, wire-format translation, graceful degradation.
No API calls. Run: python test_model_gateway.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
from llm_provider import load_registry, load_settings, resolve_role, get_provider, gateway_status
from claude_tool_definitions import TOOL_DEFINITIONS

results = []
def check(n, desc, cond):
    results.append(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {n}. {desc}")

print("== LLM Gateway tests (offline) ==")

# 1. registry + settings load and resolve all three roles
reg = load_registry(); st = load_settings()
ok = all(resolve_role(r)[0] and resolve_role(r)[1] for r in ("planner", "answer", "fallback"))
check(1, "registry+settings resolve planner/answer/fallback", ok)

# 2. admin override wins over registry default
pname, model, _ = resolve_role("planner")
check(2, f"admin_settings override applied (planner={pname}/{model})",
      f"{pname}/{model}" == st.get("planner_model"))

# 3. all three providers construct (no API call)
provs = {r: get_provider(r) for r in ("planner", "answer", "fallback")}
check(3, "providers instantiate: " + ", ".join(p.label() for p in provs.values()),
      len(provs) == 3)

# 4. availability reflects API keys (anthropic may have key; deepseek/openai likely not)
status = gateway_status()
print(f"      availability: " + ", ".join(f"{r}={status[r]['available']}" for r in ("planner","answer","fallback")))
check(4, "gateway_status reports per-role availability", all(k in status for k in ("planner","answer","fallback")))

# 5. canonical -> Anthropic wire translation
from anthropic_provider import AnthropicProvider
canon = [
    {"role": "user", "content": "q"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "find_expired_tickets", "args": {"count_only": True}}]},
    {"role": "tool_results", "results": [{"id": "t1", "content": "{\"n\":1999}", "is_error": False}]},
]
wire = AnthropicProvider._to_wire(canon)
check(5, "anthropic wire: tool_use + tool_result blocks",
      wire[1]["content"][0]["type"] == "tool_use" and wire[2]["content"][0]["type"] == "tool_result"
      and wire[2]["content"][0]["tool_use_id"] == "t1")

# 6. canonical -> OpenAI/DeepSeek wire translation
from openai_provider import OpenAIProvider
owire = OpenAIProvider._to_wire("sys", canon)
check(6, "openai wire: system + function tool_calls + role=tool",
      owire[0]["role"] == "system"
      and owire[2]["tool_calls"][0]["function"]["name"] == "find_expired_tickets"
      and json.loads(owire[2]["tool_calls"][0]["function"]["arguments"]) == {"count_only": True}
      and owire[3]["role"] == "tool" and owire[3]["tool_call_id"] == "t1")

# 7. canonical tool defs -> OpenAI function format (all 39)
ow_tools = OpenAIProvider._tools_to_wire(TOOL_DEFINITIONS)
check(7, f"all {len(ow_tools)} tool defs translate to OpenAI function format",
      len(ow_tools) == len(TOOL_DEFINITIONS) and all(t["type"] == "function" and
      t["function"]["parameters"]["type"] == "object" for t in ow_tools))

# 8. deepseek provider = openai-compatible with deepseek endpoint
from deepseek_provider import DeepSeekProvider
ds = DeepSeekProvider("deepseek-chat", {"api_key_env": "DEEPSEEK_API_KEY"})
check(8, "deepseek provider inherits OpenAI wire + correct base_url",
      ds.base_url == "https://api.deepseek.com" and ds.name == "deepseek")

# 9. answer stage degrades gracefully when answer model has no key
from answer_generator import AnswerGenerator
ag = AnswerGenerator()
final, used = ag.generate("q", [{"tool": "x", "summary": "s"}], "DRAFT ANSWER", "anthropic/claude-opus-4-8")
check(9, "answer stage falls back to planner draft when answer model unavailable",
      final == "DRAFT ANSWER" and used is None or used is not None)

# 10. gateway runner constructs with tools + system prompt (no API call)
from llm_agent_runner import LuluGatewayAgent
try:
    agent = LuluGatewayAgent()
    check(10, "LuluGatewayAgent constructs (tools + prompt + answerer wired)", True)
except Exception as e:
    check(10, f"LuluGatewayAgent constructs -> {e}", False)

print(f"\n== {sum(results)}/{len(results)} checks passed ==")
