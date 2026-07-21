"""Lulu's clock — ALWAYS Australia/Perth, regardless of where the code runs.

Locally the admin's laptop is already on Perth time, but cloud containers run UTC
(8h behind) — without pinning, 'today' would be wrong before 8am Perth.
Every date-aware component imports from here; never call datetime.now() directly.
"""

import datetime as _dt
from zoneinfo import ZoneInfo

PERTH = ZoneInfo("Australia/Perth")


def perth_now() -> _dt.datetime:
    return _dt.datetime.now(PERTH)


def perth_today() -> _dt.date:
    return perth_now().date()


def today_context() -> str:
    """The standard date line injected into every LLM conversation."""
    now = perth_now()
    return (f"[Context] Today is {now:%A}, {now:%Y-%m-%d}, current time {now:%H:%M} "
            "(Australia/Perth, AWST). Resolve all relative dates "
            "(last week/上个礼拜, tomorrow/明天, this month/本月) from this.")
