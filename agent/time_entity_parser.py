"""
time_entity_parser.py — bilingual (EN/中文) relative-time parsing + business-entity resolution.

One place for "人话 -> 结构化过滤" so the planner, traces and regression tests all share
the same rules. Three capabilities:

  parse_time(question)    -> {date_from, date_to} / {month} / {period} + which phrase matched
  resolve_entity(question)-> {type: site|project|client|supplier, value} matched against the
                             ACTUAL vocabulary in Gold (loaded through the safety chain)
  detect_intent(question) -> 'roster' (时间表/排班/schedule/班表) vs 'timesheet' (timesheet/工时表/考勤)

Today is always pinned Australia/Perth (lulu_time.perth_today).
"""

import datetime as dt
import re

from lulu_time import perth_today

# ---------------------------------------------------------------- intents
ROSTER_PHRASES = ["roster", "rostered", "schedule", "排班", "时间表", "班表", "上班安排"]
TIMESHEET_PHRASES = ["timesheet", "工时表", "考勤", "实际工时"]


def detect_intent(question):
    """'时间表/排班/schedule/roster' -> roster; 'timesheet/工时表/考勤' -> timesheet; else None."""
    q = question.lower()
    if any(p in q for p in TIMESHEET_PHRASES):
        return "timesheet"
    if any(p in q for p in ROSTER_PHRASES):
        return "roster"
    return None


# ---------------------------------------------------------------- time
_WEEK_LAST = ["last week", "上个礼拜", "上礼拜", "上周", "上星期", "上個禮拜", "上週"]
_WEEK_THIS = ["this week", "这周", "本周", "这个礼拜", "这星期", "本週"]
_WEEK_NEXT = ["next week", "下周", "下个礼拜", "下星期", "下週"]
_DAY_YESTERDAY = ["yesterday", "昨天"]
_DAY_TOMORROW = ["tomorrow", "明天"]
_DAY_TODAY = ["today", "今天", "今日"]
_MONTH_LAST = ["last month", "上个月", "上月"]
_MONTH_THIS = ["this month", "这个月", "本月"]


def _week(monday):
    return monday.isoformat(), (monday + dt.timedelta(days=6)).isoformat()


def parse_time(question, today=None, relative_only=False):
    """Return {} or a dict with date_from/date_to | month | period, plus _time_phrase.
    relative_only=True skips explicit-date fallbacks (planner handles those itself)."""
    q = question.lower()
    today = today or perth_today()
    f = {}

    def hit(phrases):
        for p in phrases:
            if p in q:
                return p
        return None

    p = hit(_WEEK_LAST)
    if p:
        f["date_from"], f["date_to"] = _week(today - dt.timedelta(days=today.weekday() + 7))
    elif hit(_WEEK_THIS):
        p = hit(_WEEK_THIS)
        f["date_from"], f["date_to"] = _week(today - dt.timedelta(days=today.weekday()))
    elif hit(_WEEK_NEXT):
        p = hit(_WEEK_NEXT)
        f["date_from"], f["date_to"] = _week(today + dt.timedelta(days=7 - today.weekday()))
    elif hit(_DAY_YESTERDAY):
        p = hit(_DAY_YESTERDAY)
        f["date_from"] = f["date_to"] = (today - dt.timedelta(days=1)).isoformat()
    elif hit(_DAY_TOMORROW):
        p = hit(_DAY_TOMORROW)
        f["date_from"] = f["date_to"] = (today + dt.timedelta(days=1)).isoformat()
    elif hit(_DAY_TODAY):
        p = hit(_DAY_TODAY)
        f["date_from"] = f["date_to"] = today.isoformat()
    elif hit(_MONTH_LAST):
        p = hit(_MONTH_LAST)
        f["month"] = (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    elif hit(_MONTH_THIS):
        p = hit(_MONTH_THIS)
        f["month"] = today.strftime("%Y-%m")

    if f:
        f["_time_phrase"] = p
        return f
    if relative_only:
        return {}

    # explicit dates: YYYY-MM-DD range, YYYY-MM, YYYY, 'in <monthname> YYYY'
    m = re.search(r"\b(20\d\d-\d\d-\d\d)\s*(?:to|至|到|-|~)\s*(20\d\d-\d\d-\d\d)\b", question)
    if m:
        return {"date_from": m.group(1), "date_to": m.group(2), "_time_phrase": m.group(0)}
    m = re.search(r"in (january|february|march|april|may|june|july|august|september|october|november|december)\s+(20\d\d)",
                  question, re.I)
    if m:
        mon = ["january", "february", "march", "april", "may", "june", "july", "august",
               "september", "october", "november", "december"].index(m.group(1).lower()) + 1
        return {"period": f"{m.group(2)}-{mon:02d}", "_time_phrase": m.group(0)}
    m = re.search(r"\b(20\d\d)-(\d\d)\b", question)
    if m:
        return {"month": m.group(0), "_time_phrase": m.group(0)}
    if re.search(r"\b(20\d\d)\b", question):
        y = re.search(r"\b(20\d\d)\b", question).group(1)
        return {"year": y, "_time_phrase": y}
    return {}


def time_range_label(f):
    """Reverse map for regression tests: args -> canonical label like 'last_week'."""
    today = perth_today()
    checks = {
        "last_week": _week(today - dt.timedelta(days=today.weekday() + 7)),
        "this_week": _week(today - dt.timedelta(days=today.weekday())),
        "next_week": _week(today + dt.timedelta(days=7 - today.weekday())),
        "yesterday": ((today - dt.timedelta(days=1)).isoformat(),) * 2,
        "today": (today.isoformat(),) * 2,
        "tomorrow": ((today + dt.timedelta(days=1)).isoformat(),) * 2,
    }
    pair = (f.get("date_from"), f.get("date_to"))
    for label, expect in checks.items():
        if pair == expect:
            return label
    if f.get("month") == (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m"):
        return "last_month"
    if f.get("month") == today.strftime("%Y-%m"):
        return "this_month"
    return None


# ---------------------------------------------------------------- entities
def resolve_entity(question):
    """{'type','value'} | None — delegates to the Search Layer (entity_resolver):
    normalised match ('Acmegroup'=='Acme Group'), aliases ('MG'), fuzzy typo tolerance,
    person names ('Carter' -> 'JOHN CARTER'). Never guesses below the auto threshold."""
    from entity_resolver import resolve_in_question
    hit = resolve_in_question(question)
    return {"type": hit["type"], "value": hit["value"]} if hit else None


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for q in ["上个礼拜的 Acme Group 的时间表", "last week timesheet", "tomorrow",
              "in april 2026", "2024-05", "who can go to NWM"]:
        print(f"{q!r:42} time={parse_time(q)} intent={detect_intent(q)} entity={resolve_entity(q)}")
