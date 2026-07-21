"""LuluAgent x Claude tool-use — 20 real-question live test.
Run: python test_claude_tool_use.py            (needs ANTHROPIC_API_KEY)
     python test_claude_tool_use.py 3          (run only question #3)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from claude_agent_runner import ClaudeLuluAgent

QUESTIONS = [
    # (question, role)
    ("谁的证书快到期了？", "default"),                                          # expiring soon (中文)
    ("Which workers cannot be deployed tomorrow because of expired certs?", "default"),  # 明天谁不能上岗
    ("Which active workers have had no roster in the last 90 days?", "default"),         # roster gaps
    ("Which supplier has the highest compliance risk?", "default"),                       # supplier risk
    ("Give me a 360 view of worker 6.", "default"),                                       # 员工360
    ("Who logged the most hours overall?", "default"),                                    # 工时最多的人
    ("What are our active projects?", "default"),                                         # active projects
    ("Is TESTSITE ready for deployment?", "default"),                                     # site readiness
    ("Which PPE or inventory items are low in stock?", "default"),                        # 库存低
    ("What did we spend per supplier?", "default"),                                       # purchase 默认角色 -> 降级
    ("What did we spend per supplier?", "Finance"),                                       # purchase Finance -> 金额
    ("How many certificates are expired right now?", "default"),
    ("Is worker 6 compliant for Working at Heights?", "default"),
    ("Who is rostered in June 2026?", "default"),
    ("Which suppliers provide the most workers?", "default"),
    ("What changed recently on employee records?", "default"),
    ("What is the day shift rate for boilermakers?", "default"),                          # rate 默认角色 -> 拒绝/降级
    ("Show me the top 5 workers by mobilisation ranking.", "default"),                    # ranking 默认 -> role gate
    ("Find the worker CARTER and tell me their role.", "default"),                       # search -> profile chain
    ("What's the cert expiry forecast for the next 6 months?", "default"),
]


def show(i, q, role, r):
    print(f"\n{'='*80}\nQ{i:02d} [{role}] {q}")
    if r.tools_called:
        for t in r.tools_called:
            flag = " [BLOCKED]" if t.blocked else ""
            print(f"  tool       : {t.name}({', '.join(f'{k}={v}' for k, v in t.args.items())}){flag}")
            print(f"               -> {t.row_count} rows | {t.confidence} | {t.summary[:110]}")
    else:
        print("  tool       : (none — Claude answered/clarified directly)")
    print(f"  answer     : {r.final_answer[:400]}{'...' if len(r.final_answer) > 400 else ''}")
    print(f"  confidence : {r.confidence}")
    flags = []
    if r.clarification:
        flags.append("CLARIFICATION")
    if r.role_gate:
        flags.append("ROLE-GATE")
    if r.caveats:
        flags.append("DATA-CAVEAT")
    print(f"  flags      : {', '.join(flags) if flags else '-'}")
    if r.caveats:
        print(f"  caveats    : {r.caveats[0]}{' (+more)' if len(r.caveats) > 1 else ''}")
    print(f"  turns/time : {r.turns} turn(s), {r.duration_s:.1f}s")


if __name__ == "__main__":
    only = int(sys.argv[1]) if len(sys.argv) > 1 else None
    agent = ClaudeLuluAgent()
    tally = {"High": 0, "Medium": 0, "Low": 0}
    gates = clar = 0
    for idx, (q, role) in enumerate(QUESTIONS, 1):
        if only and idx != only:
            continue
        r = agent.ask(q, user_role=role)
        show(idx, q, role, r)
        tally[r.confidence] = tally.get(r.confidence, 0) + 1
        gates += r.role_gate
        clar += r.clarification
    print(f"\n{'='*80}\nSUMMARY: confidence {tally} | role-gates triggered: {gates} | clarifications: {clar}")
