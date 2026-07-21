"""Memory Agent test — the admin's exact scenarios: teach Lulu once, it stays smart.
Run: python test_memory_agent.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path
MEM = Path(__file__).parent / "memory"
for f in ("company_memory.yaml", "conversation_memory.yaml"):     # clean slate for the demo
    p = MEM / f
    if p.exists():
        p.unlink()

from lulu_agent import LuluAgent

agent = LuluAgent()
results = []
def check(n, desc, cond):
    results.append(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {n}. {desc}")

def show(tag, r):
    print(f"\n--- {tag} ---")
    print(f"  Q: {r.question}")
    print(f"  route: {r.domain} -> {r.tool}.{r.function}")
    print(f"  A: {r.answer[:200]}")
    if r.memory_used:
        print(f"  memory used: {r.memory_used}")

print("== Scenario 1: teach NWM's rule once ==")
r1 = agent.ask("NWM要求: VOC, WAH, Driver Licence")
show("teach", r1)
check(1, "statement classified as site_rule and persisted", r1.function == "site_rule" and "NWM" in r1.answer.upper())

print("\n== Scenario 2: ask later — Lulu applies the rule WITHOUT re-explaining ==")
r2 = agent.ask("谁明天可以去NWM？")
show("apply", r2)
check(2, "memory-driven staffing (rule recalled, certs checked against Gold)",
      r2.function == "site_staffing_by_rule" and "voc" in str(r2.args).lower())
check(3, "answer grounded in Gold with validated SQL", r2.validator_ok and r2.tables == ["training_compliance"])

print("\n== Scenario 3: flag a supplier in passing ==")
r3 = agent.ask("WL Casual 经常出问题")
show("flag", r3)
check(4, "supplier flag remembered", r3.function == "supplier_flag" and "WL Casual" in r3.answer)

print("\n== Scenario 4: later risk question cites the memory + verifies with data ==")
r4 = agent.ask("Which supplier has the biggest compliance risk?")
show("verify", r4)
check(5, "answer enriched with remembered flag", any("WL Casual" in m for m in r4.memory_used))
check(6, "current data still verified (not just memory)", "WL - Casual" in r4.answer or "WL Casual" in r4.answer)

print("\n== Scenario 5: Ironclad rule (the admin's second example) ==")
agent.ask("Ironclad requires Confined Space, Gas Test")
r5 = agent.ask("Who is ready to go to Ironclad?")
show("ironclad", r5)
check(7, "Ironclad rule learned & applied", r5.function == "site_staffing_by_rule" and "confined" in str(r5.args).lower())

print("\n== Scenario 6: definitions + chit-chat hygiene ==")
r6 = agent.ask("active worker 的定义是 roster in the last 90 days")
check(8, "definition captured", r6.function == "definition")
r7 = agent.ask("谢谢")
check(9, "chit-chat NOT stored as knowledge", r7.function != "fact" and "已记" not in r7.answer)

print("\n== Scenario 7: memory survives a restart (new agent instance) ==")
agent2 = LuluAgent()
r8 = agent2.ask("who can go to NWM?")
check(10, "fresh instance still knows NWM's rule (persisted YAML)",
      r8.function == "site_staffing_by_rule")

print("\n== Scenario 8: conversation memory builds a profile ==")
prof = agent2.memory.user_profile("admin")
print(f"  profile: {prof}")
check(11, "focus topics tracked across questions", prof["questions_asked"] >= 3)

print(f"\n== {sum(results)}/{len(results)} checks passed ==")
import yaml
print("\n--- company_memory.yaml (what Lulu now knows) ---")
print((MEM / "company_memory.yaml").read_text(encoding="utf-8")[:900])
