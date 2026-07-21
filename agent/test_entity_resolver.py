"""Search Layer (实体归一) tests. Run: python test_entity_resolver.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from entity_resolver import resolve, suggest, resolve_in_question
from lulu_agent import LuluAgent

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {label}")
    else:
        fail += 1
        print(f"  ✗ {label}  {detail}")


print("— resolve(): normalisation / alias / fuzzy / person —")
r = resolve("Acmegroup")
check("'Acmegroup' == 'Acme Group' (normalised)", r["status"] == "exact"
      and r["match"]["value"] == "Acme Group", str(r))
r = resolve("MG")
check("'MG' alias -> Acme Group", r["match"] and r["match"]["value"] == "Acme Group", str(r))
r = resolve("Acme Grup")
check("'Acme Grup' fuzzy >= 90 auto-match", r["status"] == "fuzzy"
      and r["match"]["value"] == "Acme Group", str(r))
r = resolve("Carter")
check("'Carter' -> person JOHN CARTER", r["match"] and r["match"]["type"] == "person"
      and "CARTER" in r["match"]["value"], str(r))
r = resolve("Transport and Hire")
check("'and' == '&' normalisation", r["match"] and r["match"]["value"] == "Transport & Hire", str(r))
r = resolve("Marloogroop")
check("heavy typo -> candidates, never auto-guess", r["match"] is None and r["candidates"], str(r))
r = resolve("zzzzqqqq")
check("garbage -> none", r["status"] == "none" and not r["candidates"], str(r))

print("— resolve_in_question() —")
h = resolve_in_question("上个礼拜 site=Acmegroup, 所有人的 timesheet")
check("site=X pattern resolves", h and h["value"] == "Acme Group", str(h))
h = resolve_in_question("上个礼拜 site=Marloogroop 的timesheet")
check("unresolvable explicit filter keeps RAW (never drop the filter)",
      h and h["value"] == "Marloogroop" and h["score"] == 0, str(h))
h = resolve_in_question("MG 上周的timesheet")
check("short abbreviation via alias map", h and h["value"] == "Acme Group", str(h))

print("— end-to-end (deterministic agent) —")
agent = LuluAgent()
r = agent.ask("上个礼拜 site=Acmegroup, 所有人的 timesheet")
check("THE screenshot bug: Acmegroup timesheet returns rows",
      r.row_count > 0 and r.args.get("site") == "Acme Group", f"rows={r.row_count} args={r.args}")
r = agent.ask("Carter上周的timesheet")
check("person filter end-to-end", r.row_count > 0 and "CARTER" in str(r.args), str(r.args))
r = agent.ask("上个礼拜 site=Marloogroop 的timesheet")
check("0-row rescue offers candidates instead of dead end",
      "Acme Group" in r.answer and ("可能是" in r.answer or "最接近" in r.answer), r.answer[-120:])
r = agent.ask("上个礼拜 site=Acme Grup 的timesheet")
check("mild typo auto-corrected", r.row_count > 0 and r.args.get("site") == "Acme Group",
      f"rows={r.row_count} args={r.args}")

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
