"""Multi-turn follow-up resolution tests (deterministic engine). Run: python test_followup.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import datetime as dt
from lulu_time import perth_today
from lulu_agent import LuluAgent

TODAY = perth_today()
LW_MON = (TODAY - dt.timedelta(days=TODAY.weekday() + 7)).isoformat()
TW_MON = (TODAY - dt.timedelta(days=TODAY.weekday())).isoformat()
TOMORROW = (TODAY + dt.timedelta(days=1)).isoformat()
LM = (TODAY.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")

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


def run_conversation(turns):
    """turns: [(question, check_fn)]; threads history like the chat UI does."""
    hist = []
    for q, check_fn in turns:
        r = agent.ask(q, history=hist, conversation_id="test_followup")
        hist.append({"question": r.question, "answer": r.answer,
                     "tool": r.tool, "function": r.function, "args": r.args})
        check_fn(r)


print("— conversation 1: timesheet time/person hopping —")
run_conversation([
    ("上个礼拜acme的timesheet", lambda r: check(
        "T1 timesheet last week", r.function == "get_weekly_timesheet"
        and r.args.get("date_from") == LW_MON, str(r.args))),
    ("那这周呢?", lambda r: check(
        "T2 '那这周呢' inherits tool, swaps week", r.function == "get_weekly_timesheet"
        and r.args.get("date_from") == TW_MON, f"{r.tool}.{r.function} {r.args}")),
    ("换成上个月", lambda r: check(
        "T3 month converts to date range for this tool", r.function == "get_weekly_timesheet"
        and r.args.get("date_from") == LM + "-01" and r.args.get("date_to") == LM + "-31",
        str(r.args))),
    ("CARTER呢?", lambda r: check(
        "T4 person swap keeps the time window", r.function == "get_weekly_timesheet"
        and r.args.get("worker_name") == "CARTER" and r.args.get("date_from") == LM + "-01",
        str(r.args))),
    ("what about JONES?", lambda r: check(
        "T5 English person swap", r.args.get("worker_name") == "JONES", str(r.args))),
])

print("— conversation 2: roster follow-up —")
run_conversation([
    ("上周谁在排班?", lambda r: check(
        "R1 roster last week", r.function == "get_roster_summary"
        and r.args.get("date_from") == LW_MON, str(r.args))),
    ("那明天呢?", lambda r: check(
        "R2 '那明天呢' single-day swap", r.function == "get_roster_summary"
        and r.args.get("date_from") == TOMORROW == r.args.get("date_to"),
        f"{r.tool}.{r.function} {r.args}")),
])

print("— guards —")
r = agent.ask("What about John?")          # NO history -> must still clarify, not crash
check("no history -> clarification unchanged", r.answer.startswith("CLARIFICATION"), r.answer[:60])

hist = [{"question": "上周谁在排班?", "answer": "...", "tool": "roster",
         "function": "get_roster_summary", "args": {"date_from": LW_MON, "date_to": (dt.date.fromisoformat(LW_MON) + dt.timedelta(days=6)).isoformat()}}]
r = agent.ask("谁明天不能上岗?", history=hist)   # routable question must NOT be hijacked by history
check("routable question ignores history", r.function == "find_not_eligible_workers",
      f"{r.tool}.{r.function}")

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
