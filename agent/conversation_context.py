"""
conversation_context.py — multi-turn follow-up resolution for the deterministic engine.

"上个礼拜acme的timesheet" → "那这周呢?" should NOT be a clarification: the second turn
inherits the previous tool and only swaps the filters that changed (time / entity / person).

The deterministic planner stays single-question; this module only kicks in when the
planner fails (clarification) AND there is history to lean on — zero risk to existing routes.
The LLM gateway gets context the native way (previous turns in the messages array).
"""

import re

from time_entity_parser import parse_time, resolve_entity

# follow-up phrasing signals (EN + 中文)
_FOLLOWUP_PAT = re.compile(
    r"(那|呢|换成|改成|那么|还有|这个|那个|他|她|这周|上周|下周|这个月|上个月"
    r"|what about|how about|and (for|in|at)\b|same (for|but)|instead)", re.I)

# which person-argument each function expects (for "What about John?" style swaps)
PERSON_ARG = {
    "search_employee": "name", "get_employee_profile": "name",
    "check_worker_compliance": "worker_name", "get_worker_hours": "worker_name",
    "get_weekly_timesheet": "worker_name", "get_worker_licences": None,  # takes worker_id only
}
PERSON_KEYS = ("worker_id", "worker_name", "name")
DATE_KEYS = ("date_from", "date_to", "month", "period", "year")
ENTITY_ARG = {"site": "site", "project": "project", "client": "client"}


def is_followup(question, history):
    """Cheap signal: there IS a previous tool turn and the question reads like a delta."""
    if not history:
        return False
    q = question.strip()
    return bool(_FOLLOWUP_PAT.search(q)) or len(q) <= 12


def _extract_person(question):
    """Single capitalised name ('John', 'CARTER') — the planner itself only catches
    ALL-CAPS first+last pairs, so follow-ups often carry just one token."""
    stop = {"What", "About", "And", "The", "How", "Who", "Show", "Same", "Last", "This",
            "Next", "Week", "Month", "Today", "Tomorrow", "Acme", "Group"}
    # NOT \b: Chinese chars count as \w in Python, so 'CARTER呢' would never match
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Z][A-Za-z]{2,})(?![A-Za-z0-9])", question):
        if m.group(1) not in stop:
            return m.group(1)
    return None


def merge_followup(question, history):
    """Try to answer a clarification-bound question by deltaing the last tool turn.

    history: [{question, tool, function, args}, ...] (most recent last)
    Returns {tool, function, args, notes} or None when no safe merge exists.
    """
    last = next((h for h in reversed(history) if h.get("tool") and h.get("function")
                 and h["tool"] not in ("memory", "meta")), None)
    if not last:
        return None

    args = dict(last.get("args") or {})
    notes, changed = [], False

    # 1. new time window replaces the old one entirely — keeping the SHAPE the previous
    #    call used (a month answer to a date_from/date_to tool becomes a month range)
    t = parse_time(question)
    t.pop("_time_phrase", None)
    if t:
        had_range = "date_from" in args or "date_to" in args
        for k in DATE_KEYS:
            args.pop(k, None)
        if had_range and ("month" in t or "period" in t):
            ym = t.get("month") or t.get("period")
            t = {"date_from": f"{ym}-01", "date_to": f"{ym}-31"}
        args.update(t)
        notes.append(f"time → {t}")
        changed = True

    # 2. new entity (site/project/client) replaces the matching filter
    ent = resolve_entity(question)
    if ent and ent["type"] in ENTITY_ARG:
        key = ENTITY_ARG[ent["type"]]
        if args.get(key) != ent["value"]:
            args[key] = ent["value"]
            notes.append(f"{key} → {ent['value']}")
            changed = True

    # 3. new person replaces the previous person filter
    person = _extract_person(question)
    if person:
        pkey = PERSON_ARG.get(last["function"], "worker_name")
        if pkey:
            for k in PERSON_KEYS:
                args.pop(k, None)
            args[pkey] = person
            notes.append(f"{pkey} → {person}")
            changed = True

    if not changed:
        return None
    return {"tool": last["tool"], "function": last["function"], "args": args,
            "notes": notes, "inherited_from": last["question"]}


def history_to_messages(history, max_turns=30):
    """Previous turns as alternating user/assistant messages for the LLM gateway.
    Each past answer carries a structured [tool: ...] tag so follow-ups inherit the
    previous person/filters by reading a label instead of guessing from prose."""
    msgs = []
    for h in history[-max_turns:]:
        msgs.append({"role": "user", "content": h["question"]})
        a = str(h.get("answer", ""))[:2000]
        if h.get("tool") and h.get("function"):
            a += (f"\n[tool: {h['tool']}.{h['function']}"
                  + (f", args: {str(h['args'])[:200]}" if h.get("args") else "") + "]")
        msgs.append({"role": "assistant", "content": a})
    return msgs
