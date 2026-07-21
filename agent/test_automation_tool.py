"""Smoke-test the automation (GitHub workflow estate) tool. Run: python test_automation_tool.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import build_tools
from planner_v2 import PlannerV2

tools = build_tools()
auto = tools["automation"]
ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {label}")
    else:
        fail += 1
        print(f"  ✗ {label}  {detail}")


print("— tool functions —")
r = auto.list_automations()
check("list_automations returns 14", r.ok and r.row_count == 14, f"got {r.row_count}")

r = auto.list_automations(category="AI")
check("list_automations(category='AI') filters", r.ok and 0 < r.row_count < 10, f"got {r.row_count}")

r = auto.get_automation_detail(name="timesheet automation")
check("get_automation_detail fuzzy name", r.ok and r.row_count == 1
      and r.data[0]["repo"] == "acme_weeklytimesheet_automation", r.summary)
check("detail includes GitHub workflow triggers",
      bool(r.data and r.data[0].get("github_workflows")
           and r.data[0]["github_workflows"][0].get("triggers")), str(r.data[0].get("github_workflows")))

r = auto.get_automation_detail(name="不存在的系统xyz")
check("detail no-match degrades gracefully", r.ok and r.row_count == 0 and "Known automations" in r.summary)

r = auto.find_automation(keyword="rates")
check("find_automation('rates') → rates updater", r.ok and
      any(d["repo"] == "AdminLuo-working-UpdateTimesheet-Projects-Rates-" for d in r.data), r.summary)

r = auto.find_automation(keyword="费率")
check("find_automation('费率') Chinese keyword", r.ok and r.row_count >= 1, r.summary)

r = auto.get_automation_runs(name="quote")
check("get_automation_runs(one) returns run rows", r.ok and r.row_count >= 1, r.summary)

r = auto.get_automation_runs()
check("get_automation_runs(all) health check 10 repos", r.ok and r.row_count >= 10, f"got {r.row_count}")

print("— planner routing —")
p = PlannerV2()
plan = p.plan("我们有哪些自动化系统?")
check("'哪些自动化系统' → list_automations", plan.function == "list_automations", f"got {plan.function}")

plan = p.plan("上周的timesheet automation 跑成功了吗?")
check("'automation 跑成功了吗' → get_automation_runs", plan.function == "get_automation_runs", f"got {plan.function}")

plan = p.plan("哪个系统管费率更新?")
check("'哪个系统管费率' → find_automation", plan.function == "find_automation", f"got {plan.function}")

plan = p.plan("which system handles gap hours?")
check("'which system handles gap hours' → find_automation",
      plan.function == "find_automation" and "gap" in plan.args.get("keyword", ""),
      f"got {plan.function} {plan.args}")

plan = p.plan("谁明天可以上岗?")  # regression: deployable workers must NOT hit automation
check("deployable-workers routing unaffected", plan.tool != "automation", f"got {plan.tool}.{plan.function}")

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
