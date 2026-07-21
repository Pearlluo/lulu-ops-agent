"""Search-escalation chain tests (0 rows is never a final answer). Run: python test_search_escalation.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
from pathlib import Path

from lulu_agent import LuluAgent
from query_tool import QueryTool

agent = LuluAgent()
ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {label}")
    else:
        fail += 1
        print(f"  ✗ {label}  {detail}")


print("— steps 2+3: resolve + retry (the Acmegroup regression) —")
r = agent.ask("上个礼拜 site=Acmegroup, 所有人的 timesheet")
check("Acmegroup resolves and returns rows", r.row_count > 0
      and r.args.get("site") == "Acme Group", f"rows={r.row_count}")

print("— step 1+4+6: diagnostic answer when truly 0 rows —")
r = agent.ask("2027-01-01 to 2027-01-07 site=Acme Group 的timesheet")
check("0 rows -> NOT a bare 'no records'", "no matching records" not in r.answer.lower()
      and "0 条记录" in r.answer, r.answer[:80])
check("says WHAT was searched", "get_weekly_timesheet" in r.answer and "Acme Group" in r.answer)
check("gives a REASON (date coverage)", "日期范围在数据覆盖之外" in r.answer, r.answer[:200])
check("probes related Gold tables (entity exists)", "site_assignment" in r.answer, r.answer)
check("escalation steps recorded in plan trace",
      any("search escalation" in s for s in r.plan_steps), str(r.plan_steps[:2]))
check("default role gets NO RAW section", "RAW" not in r.answer)

print("— step 5: RAW layer is Admin_IT-gated —")
r2 = agent.ask("2027-01-01 to 2027-01-07 site=Acme Group 的timesheet", user_role="Admin_IT")
check("Admin_IT gets RAW/UNVALIDATED section", "RAW/UNVALIDATED" in r2.answer, r2.answer[-150:])
check("RAW section carries the debug-only disclaimer", "不构成业务结论" in r2.answer)

qt = QueryTool()
rd = qt.raw_debug_lookup("Acme", user_role="default")
check("raw_debug_lookup REFUSES non-admin", rd["allowed"] is False and not rd["hits"])
rd = qt.raw_debug_lookup("Acme", user_role="HR_Manager")
check("raw_debug_lookup refuses HR_Manager too", rd["allowed"] is False)
rd = qt.raw_debug_lookup("Acme", user_role="Admin_IT")
check("Admin_IT raw lookup finds silver-flat hits", rd["allowed"] and len(rd["hits"]) > 0)

log = Path("logs/raw_debug_access.jsonl")
check("every RAW access is audit-logged", log.exists()
      and "Acme" in [json.loads(l)["term"] for l in open(log, encoding="utf-8")][-1])

print("— guard: successful queries never escalate —")
r3 = agent.ask("上个礼拜的timesheet")
check("normal query untouched by escalation", r3.row_count > 0
      and not any("escalation" in s for s in r3.plan_steps))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
