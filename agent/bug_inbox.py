"""
bug_inbox.py — classify failed / low-confidence conversations into actionable bug categories.

Reads logs/conversation_traces.jsonl (TraceLogger) and sorts every suspicious trace into:

    semantic_gap              planner couldn't map the phrase to any tool (clarification)
    time_parse_error          question has a time phrase but no date filter reached the tool
    entity_resolution_error   question names a known site/project/client/supplier but no filter applied
    tool_routing_error        user said the tool/answer was wrong (correction_flag / feedback)
    business_definition_gap   business term resolved to nothing (no resolved_terms, generic route)
    data_quality_issue        tool ran fine but returned 0 rows
    answer_quality_issue      low confidence / validator blocked / error text in the answer

Usage:
    python bug_inbox.py              # classify all traces, print inbox, write logs/bug_inbox.json
    python bug_inbox.py --last 50    # only the most recent 50 traces
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from conversation_trace_logger import TraceLogger
from time_entity_parser import parse_time, resolve_entity

AGENT_DIR = Path(__file__).resolve().parent
INBOX_PATH = AGENT_DIR / "logs" / "bug_inbox.json"

CATEGORIES = ["semantic_gap", "time_parse_error", "entity_resolution_error",
              "tool_routing_error", "business_definition_gap",
              "data_quality_issue", "answer_quality_issue"]


def classify(trace):
    """Return (category, reason) or None when the trace looks healthy."""
    q = trace.get("question", "")
    args = trace.get("args") or {}
    answer = trace.get("answer", "") or ""

    # explicit human signal beats every heuristic
    if trace.get("correction_flag"):
        return "tool_routing_error", f"user flagged correction: {trace.get('user_feedback')}"

    if trace.get("needs_clarification"):
        return "semantic_gap", "planner could not map the question to any tool"

    if answer.startswith(("Tool error", "LLM error", "Internal routing error")):
        return "answer_quality_issue", answer[:120]

    if trace.get("validator_errors"):
        return "answer_quality_issue", f"validator blocked: {trace['validator_errors'][:2]}"

    # time phrase present but no date/period arg made it into the tool call
    t = parse_time(q, relative_only=True)
    if t and not any(k in str(args) for k in ("date_from", "date_to", "month", "period", "days")):
        return "time_parse_error", f"phrase '{t.get('_time_phrase')}' parsed but no date filter in args {args}"

    # known business entity in the question but no filter applied
    ent = resolve_entity(q)
    if ent and trace.get("engine") == "deterministic":
        ent_in_args = ent["value"].lower() in json.dumps(args, ensure_ascii=False).lower()
        ent_recorded = str(trace.get("resolved_terms", {})).lower().find(ent["value"].lower()) >= 0
        if not ent_in_args and not ent_recorded:
            return "entity_resolution_error", f"'{ent['value']}' ({ent['type']}) recognised but unused"

    if trace.get("result_rows", 0) == 0 and trace.get("selected_tool"):
        return "data_quality_issue", "tool ran but returned 0 rows — data gap or over-tight filter"

    if trace.get("confidence") == "Low":
        return "answer_quality_issue", "low-confidence answer"

    # business words with no semantic-layer hit and a generic/empty route
    if trace.get("engine") == "deterministic" and not trace.get("resolved_terms") \
            and not trace.get("selected_tool"):
        return "business_definition_gap", "no semantic term resolved and no tool selected"

    return None


def build_inbox(last_n=None):
    traces = TraceLogger().read(last_n)
    inbox = defaultdict(list)
    for tr in traces:
        hit = classify(tr)
        if hit:
            cat, reason = hit
            inbox[cat].append({
                "trace_id": tr["trace_id"], "ts": tr.get("ts"), "engine": tr.get("engine"),
                "question": tr.get("question"), "tool": tr.get("selected_tool"),
                "function": tr.get("function"), "args": tr.get("args"),
                "confidence": tr.get("confidence"), "reason": reason,
            })
    return {"total_traces": len(traces),
            "flagged": sum(len(v) for v in inbox.values()),
            "by_category": {c: inbox.get(c, []) for c in CATEGORIES if inbox.get(c)}}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=None)
    args = ap.parse_args()

    report = build_inbox(args.last)
    INBOX_PATH.parent.mkdir(exist_ok=True)
    INBOX_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                          encoding="utf-8")

    print(f"BUG INBOX — {report['flagged']} flagged / {report['total_traces']} traces "
          f"(saved {INBOX_PATH.name})")
    for cat, items in report["by_category"].items():
        print(f"\n[{cat}] {len(items)}")
        for it in items[:8]:
            print(f"  {it['trace_id']}  {it['question'][:46]!r}")
            print(f"      -> {it['tool']}.{it['function']}  | {it['reason'][:90]}")
    if not report["by_category"]:
        print("  (empty — no suspicious conversations)")
    print("\nPromote any of these into a regression test:")
    print("  python regression_from_chat.py promote <trace_id> --tool <t> --function <f>")


if __name__ == "__main__":
    main()
