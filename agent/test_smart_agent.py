"""LuluAgent Smart V1 (tool-first) — realistic business test questions. Run: python test_smart_agent.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lulu_agent import LuluAgent

QUESTIONS = [
    ("Which workers have expired tickets?", "default"),
    ("How many certificates are expired?", "default"),
    ("Whose certificates are expiring soon?", "default"),
    ("哪些工人的证书快到期了？", "default"),
    ("Which workers cannot work right now due to expired certs?", "default"),
    ("Is worker 6 compliant for Working at Heights?", "default"),
    ("Total hours worked at each site in 2024", "default"),
    ("How many hours did worker 4 work in 2024-04?", "default"),
    ("List active projects.", "default"),
    ("How many jobs does Ironstone have?", "default"),
    ("Which active workers have no roster in the last 90 days?", "default"),
    ("Who was rostered in June 2026?", "default"),
    ("Which suppliers provide the most workers?", "default"),
    ("Are any rostered workers a compliance risk?", "default"),            # cross-domain intelligence
    ("Which workers are deployable right now?", "default"),                # cross-domain intelligence
    ("Give me a 360 view of worker 6.", "default"),                        # multi-query merge
    ("What is the expiry forecast for the next 6 months?", "default"),     # forecast
    ("Which supplier has the biggest compliance risk?", "default"),        # supplier risk
    ("What changed recently on employee records?", "default"),
    ("Show current inventory levels.", "default"),
    ("Which items are out of stock?", "default"),
    ("Summarise purchases by supplier.", "default"),
    ("What is the total spend per supplier?", "Finance"),
    ("What about John?", "default"),                                       # ambiguous -> clarify
]


def show(i, q, role, r):
    print(f"\n{'='*78}\nQ{i:02d} [{role}] {q}")
    print(f"  domain      : {r.domain or '(unresolved)'}")
    print(f"  tool        : {r.tool + '.' + r.function if r.tool else '(clarification)'}  args={r.args}")
    if r.plan_steps:
        print(f"  plan        : {' | '.join(r.plan_steps)}")
    if r.sql:
        s = " ".join(r.sql.split())
        print(f"  SQL         : {s[:160]}{'...' if len(s) > 160 else ''}")
    print(f"  validator   : {'PASS' if r.validator_ok else ('REJECTED: ' + '; '.join(r.validator_errors) if r.validator_errors else 'n/a')}")
    print(f"  answer      : {r.answer}")
    print(f"  confidence  : {r.confidence}")
    if r.caveats:
        print(f"  caveats     : {r.caveats[0]}{' (+more)' if len(r.caveats) > 1 else ''}")


if __name__ == "__main__":
    agent = LuluAgent()
    tally = {"High": 0, "Medium": 0, "Low": 0}
    for idx, (q, role) in enumerate(QUESTIONS, 1):
        resp = agent.ask(q, user_role=role)
        show(idx, q, role, resp)
        tally[resp.confidence] = tally.get(resp.confidence, 0) + 1
    print(f"\n{'='*78}\nSUMMARY: {len(QUESTIONS)} questions | confidence {tally}")
