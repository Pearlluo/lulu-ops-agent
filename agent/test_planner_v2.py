"""Planner V2 + tool analytics demo. Run: python test_planner_v2.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lulu_agent import LuluAgent

agent = LuluAgent()

META_QUESTIONS = [
    "Analyse our workforce risk.",                 # meta: 4-step composition
    "Give me the monthly workforce report.",       # meta: 6-step composition
    "Is TESTSITE ready for deployment?",           # meta: site readiness
]

FLOW = [
    # ordinary -> follow-up (same tool) -> correction (phrase) to demo analytics signals
    ("Which workers have expired tickets?", "default"),
    ("How many certificates are expired?", "default"),          # same tool -> follow-up signal on prev
    ("no, I meant expiring in the next 14 days", "default"),    # correction signal on prev
    ("Which suppliers provide the most workers?", "default"),
    ("Which workers are deployable right now?", "default"),
    ("Which workers are deployable right now?", "default"),
    ("Show current inventory levels.", "default"),
]

print("=" * 78)
print("PART 1 — META PLANS (composition: Planner, not Tool Selector)")
for q in META_QUESTIONS:
    r = agent.ask(q)
    print(f"\n>>> {q}")
    print(f"  meta tool : {r.function}   domain: {r.domain}")
    print("  plan      :")
    for s in r.plan_steps:
        print(f"    - {s}")
    print("  step runs :")
    for s in r.step_results:
        print(f"    [{'OK' if s['ok'] else 'XX'}] {s['tool']}.{s['function']} -> {s['rows']} rows")
    print(f"  ANSWER    : {r.answer}")
    print(f"  confidence: {r.confidence}")

print("\n" + "=" * 78)
print("PART 2 — ordinary flow (generates analytics signals)")
for q, role in FLOW:
    r = agent.ask(q, user_role=role)
    print(f"  {r.tool + '.' + r.function if r.tool else '(clarify)':45s} <- {q[:48]}")

print("\n" + "=" * 78)
print("PART 3 — TOOL ANALYTICS (which tools earn their keep)")
agent.usage_report(last_n=100)
