"""Side-by-side: deterministic planner vs LLM gateway on the same question.
Run:  python compare_engines.py "谁明天可以去NWM？"
      python compare_engines.py "Which supplier has the biggest compliance risk?" Finance
"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

q = sys.argv[1] if len(sys.argv) > 1 else "Which workers cannot work due to expired certs?"
role = sys.argv[2] if len(sys.argv) > 2 else "default"

print(f"Q: {q}   [role={role}]")
print("=" * 78)

# ---------- engine 1: deterministic (no LLM, free, instant) ----------
from lulu_agent import LuluAgent
t0 = time.time()
r = LuluAgent().ask(q, user_role=role)
print(f"\n[1] DETERMINISTIC planner   ({time.time()-t0:.1f}s, $0)")
print(f"    route : {r.domain} -> {r.tool}.{r.function} args={r.args}")
if r.memory_used:
    print(f"    memory: {r.memory_used}")
print(f"    answer: {r.answer}")
print(f"    conf  : {r.confidence}")

# ---------- engine 2: LLM gateway (planner, or fallback if planner has no key) ----------
from llm_provider import gateway_status
gs = gateway_status()
usable = gs.get("planner", {}).get("available") or gs.get("fallback", {}).get("available")
if not usable:
    print(f"\n[2] LLM gateway: SKIPPED — no API key for planner "
          f"({gs.get('planner', {}).get('provider')}/{gs.get('planner', {}).get('model')}) "
          f"or fallback ({gs.get('fallback', {}).get('provider')}/{gs.get('fallback', {}).get('model')}).")
    print(f"    To enable: add {gs.get('planner', {}).get('api_key_env')}=sk-... "
          "to Raw Data/API/credential/.env, then re-run this script.")
else:
    from llm_agent_runner import LuluGatewayAgent
    t0 = time.time()
    g = LuluGatewayAgent().ask(q, user_role=role)
    print(f"\n[2] LLM GATEWAY ({g.planner_model})   ({time.time()-t0:.1f}s)")
    for t in g.tools_called:
        print(f"    tool  : {t['name']}({t['args']}) -> {t['rows']} rows")
    if g.answer_model:
        print(f"    answer model: {g.answer_model}")
    print(f"    answer: {g.final_answer[:500]}")
    print(f"    conf  : {g.confidence}")
