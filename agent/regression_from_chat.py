"""
regression_from_chat.py — every chat bug becomes a permanent test.

Cases live in tests/regression_cases.yaml. Each case asserts PLANNER behaviour
(routing + time + entity), so the suite runs in milliseconds with no LLM calls:

  - id: acme-group-schedule
    input: 上个礼拜的 Acme Group 的时间表
    expected:
      tool: roster
      function: get_roster_summary
      time_range: last_week          # canonical label, resolved against TODAY at run time
      entity: site:Acme Group       # must appear in plan.resolved_terms or args
      args_contains: {}              # exact key/value fragments that must be in args
    origin: manual | trace:<id>

Usage:
  python regression_from_chat.py run                 # run all cases (exit 1 on failure)
  python regression_from_chat.py promote <trace_id> [--tool T --function F --time-range L --entity E]
        # promote a logged conversation into a case; overrides describe the CORRECT behaviour
  python regression_from_chat.py list
"""

import argparse
import sys
import time as _time
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parent
CASES_PATH = AGENT_DIR / "tests" / "regression_cases.yaml"


def load_cases():
    if not CASES_PATH.exists():
        return []
    data = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8")) or {}
    return data.get("cases", [])


def save_cases(cases):
    CASES_PATH.parent.mkdir(exist_ok=True)
    CASES_PATH.write_text(
        "# Regression cases promoted from real chat (regression_from_chat.py).\n"
        "# Every bug found in conversation gets a case here so it can never silently return.\n"
        + yaml.safe_dump({"cases": cases}, allow_unicode=True, sort_keys=False, width=110),
        encoding="utf-8")


# ---------------------------------------------------------------- run
def expected_dates(label):
    from time_entity_parser import parse_time
    phrase = {"last_week": "last week", "this_week": "this week", "next_week": "next week",
              "yesterday": "yesterday", "today": "today", "tomorrow": "tomorrow",
              "last_month": "last month", "this_month": "this month"}[label]
    f = parse_time(phrase)
    f.pop("_time_phrase", None)
    return f


def run_case(planner, case):
    plan = planner.plan(case["input"])
    exp = case.get("expected", {})
    errors = []

    if exp.get("tool") and getattr(plan, "tool", None) != exp["tool"]:
        errors.append(f"tool: want {exp['tool']}, got {getattr(plan, 'tool', None)!r}")
    if exp.get("function") and getattr(plan, "function", None) != exp["function"]:
        errors.append(f"function: want {exp['function']}, got {getattr(plan, 'function', None)!r}")
    if exp.get("domain") and exp["domain"].lower() not in str(getattr(plan, "domain", "")).lower():
        errors.append(f"domain: want {exp['domain']}, got {getattr(plan, 'domain', None)!r}")

    args = getattr(plan, "args", {}) or {}
    if exp.get("time_range"):
        want = expected_dates(exp["time_range"])
        for k, v in want.items():
            if args.get(k) != v and getattr(plan, "resolved_terms", {}).get(k) != v:
                errors.append(f"time_range {exp['time_range']}: want {k}={v}, args have {args.get(k)!r}")
    if exp.get("entity"):
        seen = str(getattr(plan, "resolved_terms", {})) + str(args)
        if exp["entity"].split(":", 1)[-1].lower() not in seen.lower():
            errors.append(f"entity '{exp['entity']}' not resolved (terms={getattr(plan, 'resolved_terms', {})})")
    for k, v in (exp.get("args_contains") or {}).items():
        if str(v).lower() not in str(args.get(k, "")).lower():
            errors.append(f"args[{k}]: want contains {v!r}, got {args.get(k)!r}")
    if exp.get("not_clarification") and getattr(plan, "needs_clarification", False):
        errors.append("planner asked for clarification but a route was expected")
    return errors


def cmd_run():
    from planner_v2 import PlannerV2
    planner = PlannerV2()
    cases = load_cases()
    if not cases:
        print("No regression cases yet — promote one with: python regression_from_chat.py promote <trace_id>")
        return 0
    failed = 0
    for case in cases:
        errors = run_case(planner, case)
        if errors:
            failed += 1
            print(f"  ✗ {case['id']}  {case['input'][:50]!r}")
            for e in errors:
                print(f"      {e}")
        else:
            print(f"  ✓ {case['id']}")
    print(f"\n{len(cases) - failed}/{len(cases)} regression cases passed")
    return 1 if failed else 0


# ---------------------------------------------------------------- promote
def cmd_promote(trace_id, overrides):
    from conversation_trace_logger import TraceLogger
    from time_entity_parser import parse_time, time_range_label, resolve_entity

    tr = TraceLogger().find(trace_id)
    if not tr:
        print(f"trace '{trace_id}' not found in conversation_traces.jsonl")
        return 1

    q = tr["question"]
    expected = {}
    expected["tool"] = overrides.tool or tr.get("selected_tool") or None
    expected["function"] = overrides.function or tr.get("function") or None
    label = overrides.time_range or time_range_label(tr.get("args") or {}) \
        or time_range_label(parse_time(q, relative_only=True))
    if label:
        expected["time_range"] = label
    ent_str = overrides.entity
    if not ent_str:
        ent = resolve_entity(q)
        if ent:
            ent_str = f"{ent['type']}:{ent['value']}"
    if ent_str:
        expected["entity"] = ent_str
    expected = {k: v for k, v in expected.items() if v}
    expected["not_clarification"] = True

    cases = load_cases()
    cid = overrides.id or f"trace-{tr['trace_id']}"
    if any(c["id"] == cid for c in cases):
        print(f"case '{cid}' already exists")
        return 1
    cases.append({"id": cid, "input": q, "expected": expected,
                  "origin": f"trace:{tr['trace_id']}",
                  "added": _time.strftime("%Y-%m-%d")})
    save_cases(cases)
    print(f"✓ promoted trace {tr['trace_id']} -> case '{cid}'")
    print(f"  expected: {expected}")
    print("  NOTE: edit tests/regression_cases.yaml if the CORRECT behaviour differs from what was logged.")
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("list")
    pr = sub.add_parser("promote")
    pr.add_argument("trace_id")
    pr.add_argument("--id")
    pr.add_argument("--tool")
    pr.add_argument("--function")
    pr.add_argument("--time-range", dest="time_range",
                    choices=["last_week", "this_week", "next_week", "yesterday", "today",
                             "tomorrow", "last_month", "this_month"])
    pr.add_argument("--entity", help="e.g. 'site:Acme Group'")
    args = ap.parse_args()

    if args.cmd == "promote":
        sys.exit(cmd_promote(args.trace_id, args))
    elif args.cmd == "list":
        for c in load_cases():
            print(f"{c['id']:32} {c['input'][:46]!r} -> {c['expected'].get('tool')}.{c['expected'].get('function')}")
    else:
        sys.exit(cmd_run())


if __name__ == "__main__":
    main()
