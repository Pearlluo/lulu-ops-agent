"""
data_quality_sentinel.py — the roster-window bug should have been caught by a machine, not eyes.

Run after the nightly pipeline (or any local rebuild):
    python data_quality_sentinel.py            # exit 0 OK / 1 FAIL (warn does not fail)
    python data_quality_sentinel.py --strict   # warnings also fail

Checks
  1. Critical Gold tables exist and are non-empty
     (employee_profile, training_compliance, roster_summary, timesheet_summary,
      project_job_summary + weekly_timesheet, project_bridge)
  2. roster_summary max date >= today + 60 days   (the bug Admin caught by eye)
  3. Row counts must not drop > 30% vs the previous report
  4. Null-rate ceilings on key columns
  5. KPI swing alarms (> 30% day-over-day): expired certs, active workers, supplier count

Output: logs/data_quality_report.json (latest) + logs/data_quality_history.jsonl (append).
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import duckdb

AGENT_DIR = Path(__file__).resolve().parent
GOLD = AGENT_DIR.parent / "gold"
# report lives in data/agent/ (NOT logs/) so it bakes into the deployed image — logs/ is an Azure
# Files mount that shadows baked files; the cockpit + Ask-LuLu freshness need it present in the image.
REPORT_PATH = AGENT_DIR / "data_quality_report.json"
HISTORY_PATH = AGENT_DIR / "logs" / "data_quality_history.jsonl"

CRITICAL_TABLES = ["employee_profile", "training_compliance", "roster_summary",
                   "timesheet_summary", "project_job_summary",
                   "weekly_timesheet", "project_bridge"]

# column -> max allowed null fraction
NULL_CEILINGS = {
    ("employee_profile", "first_name"): 0.05,
    ("training_compliance", "is_expired"): 0.01,
    ("roster_summary", "roster_date"): 0.02,
    ("weekly_timesheet", "work_date"): 0.0,
    ("project_bridge", "client_code"): 0.10,
}

ROSTER_HORIZON_DAYS = 60
DROP_THRESHOLD = 0.30
KPI_SWING_THRESHOLD = 0.30


def _q(con, sql):
    return con.execute(sql).fetchone()[0]


def run_checks(today=None):
    today = today or dt.date.today()
    con = duckdb.connect()
    checks, kpis, counts = [], {}, {}

    def add(name, ok, detail, level="FAIL"):
        checks.append({"check": name, "ok": bool(ok), "detail": detail,
                       "level": "OK" if ok else level})

    # 1. tables exist + non-empty, collect row counts
    for t in CRITICAL_TABLES:
        p = GOLD / f"{t}.parquet"
        if not p.exists():
            add(f"{t}: exists", False, f"{p.name} missing from gold/")
            continue
        n = _q(con, f"SELECT COUNT(*) FROM '{p}'")
        counts[t] = n
        add(f"{t}: non-empty", n > 0, f"{n:,} rows")

    # 2. roster horizon (the bug caught by eye on 2026-06-11)
    if "roster_summary" in counts:
        mx = _q(con, f"SELECT MAX(roster_date) FROM '{GOLD / 'roster_summary.parquet'}'")
        horizon = today + dt.timedelta(days=ROSTER_HORIZON_DAYS)
        ok = mx is not None and str(mx) >= horizon.isoformat()
        add("roster_summary: future horizon", ok,
            f"max roster_date = {mx}, required >= {horizon} (today + {ROSTER_HORIZON_DAYS}d)")

    # 4. null-rate ceilings
    for (t, col), ceiling in NULL_CEILINGS.items():
        p = GOLD / f"{t}.parquet"
        if not p.exists() or counts.get(t, 0) == 0:
            continue
        nulls = _q(con, f"SELECT COUNT(*) FROM '{p}' WHERE {col} IS NULL")
        frac = nulls / counts[t]
        add(f"{t}.{col}: null rate", frac <= ceiling,
            f"{frac:.1%} null (ceiling {ceiling:.0%})", level="WARN")

    # 5. KPIs
    try:
        kpis["expired_certs"] = _q(con,
            f"SELECT COUNT(*) FROM '{GOLD / 'training_compliance.parquet'}' WHERE is_expired = true")
        kpis["active_workers"] = _q(con,
            f"SELECT COUNT(*) FROM '{GOLD / 'employee_profile.parquet'}' WHERE is_active = true")
        kpis["suppliers"] = _q(con, f"SELECT COUNT(DISTINCT supplier_name) FROM '{GOLD / 'supplier_summary.parquet'}'")
        kpis["last_week_actual_hours"] = float(_q(con,
            f"SELECT COALESCE(SUM(actual_hours),0) FROM '{GOLD / 'weekly_timesheet.parquet'}' "
            f"WHERE work_date >= '{(today - dt.timedelta(days=today.weekday() + 7)).isoformat()}'"))
    except Exception as ex:
        add("kpi collection", False, f"{type(ex).__name__}: {ex}", level="WARN")

    # 3 + 5: compare with previous report
    prev = None
    if REPORT_PATH.exists():
        try:
            prev = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            prev = None
    if prev:
        for t, n in counts.items():
            pn = (prev.get("row_counts") or {}).get(t)
            if pn and pn > 0:
                drop = (pn - n) / pn
                add(f"{t}: row-count stability", drop <= DROP_THRESHOLD,
                    f"{pn:,} -> {n:,} ({-drop:+.1%})", level="WARN")
        for k, v in kpis.items():
            pv = (prev.get("kpis") or {}).get(k)
            if pv:
                swing = abs(v - pv) / max(abs(pv), 1)
                add(f"kpi {k}: swing", swing <= KPI_SWING_THRESHOLD,
                    f"{pv:,} -> {v:,} ({swing:+.1%})", level="WARN")
    else:
        add("baseline", True, "first run — counts/KPIs recorded as baseline")

    # 6. folder-link health (from check_links.py -> data/agent/link_health.json)
    try:
        lh_path = AGENT_DIR / "link_health.json"
        if lh_path.exists():
            for tbl, v in json.loads(lh_path.read_text(encoding="utf-8")).items():
                if not isinstance(v, dict) or "missing" not in v:
                    continue
                miss, dead = v.get("missing", 0), v.get("broken", 0)
                add(f"{tbl}: folder links", dead == 0 and miss == 0,
                    f"{miss} missing, {dead} dead (of {v.get('checked', '?')} checked)", level="WARN")
    except Exception as ex:
        add("folder-link health", False, f"{type(ex).__name__}: {ex}", level="WARN")

    fails = [c for c in checks if not c["ok"] and c["level"] == "FAIL"]
    warns = [c for c in checks if not c["ok"] and c["level"] == "WARN"]
    status = "FAIL" if fails else ("WARN" if warns else "OK")

    report = {"ts": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
              "status": status, "fails": len(fails), "warns": len(warns),
              "checks": checks, "row_counts": counts, "kpis": kpis}
    return report


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="warnings also fail")
    args = ap.parse_args()

    report = run_checks()
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                           encoding="utf-8")
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({k: report[k] for k in ("ts", "status", "fails", "warns",
                                                   "row_counts", "kpis")},
                           ensure_ascii=False, default=str) + "\n")

    icon = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌"}[report["status"]]
    print(f"{icon} DATA QUALITY: {report['status']}  ({report['fails']} fail, {report['warns']} warn)"
          f"  -> {REPORT_PATH.name}")
    for c in report["checks"]:
        mark = "✓" if c["ok"] else ("✗" if c["level"] == "FAIL" else "!")
        print(f"  {mark} {c['check']:42} {c['detail']}")
    sys.exit(1 if report["status"] == "FAIL" or (args.strict and report["status"] == "WARN") else 0)


if __name__ == "__main__":
    main()
