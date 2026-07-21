"""pipeline_guard.py — anti-leak gate. Runs as the LAST pipeline step (needs Gold built).

Two jobs the pipeline was missing:
  #1 Quality   — critical Gold tables exist & non-empty; row counts didn't drop >30%.
  #2 Completeness — extract didn't silently lose data: any OPMS endpoint / SharePoint list
                    / Gold table that HAD data last run but is now 0-rows or errored = a
                    regression WARN (known-empty / known-403 endpoints stay 0 in the
                    baseline, so they don't false-alarm).

Compares against a baseline persisted to blob (config/pipeline_baseline.json) so it works
across ephemeral cloud runs. Uses only pandas/pyarrow (already in the image) — no duckdb.

Exit code:
  1  = FAIL (a CRITICAL Gold table missing/empty)   -> scheduler marks the run failed
  0  = PASS or WARN (run completes; regressions printed loudly + written to the report)

Run:  python pipeline_guard.py [--strict]   (--strict: regressions also fail)
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent.parent
GOLD = DATA_DIR / "gold"
BRONZE = DATA_DIR / "bronze"
CONFIG = DATA_DIR / "config"
REPORT = CONFIG / "pipeline_guard_report.json"
BASELINE_BLOB = "config/pipeline_baseline.json"
BASELINE_LOCAL = CONFIG / "pipeline_baseline.json"

CRITICAL_GOLD = ["employee_profile", "roster_summary", "timesheet_summary",
                 "project_job_summary", "weekly_timesheet", "project_bridge", "job_detail"]
DROP_THRESHOLD = 0.30

# Sources we KNOW are unavailable right now — being empty is expected, not a leak.
# Remove a key here when access is restored so the guard starts watching it again.
EXPECTED_EMPTY = {
    "gold:invoice_register",   # Xero — no API permission currently
    "gold:revenue_summary",    # derived from Xero invoices
}


def _blob():
    """Return (container_client) or None if blob unavailable."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("u", str(SCRIPT_DIR / "upload_to_blob.py"))
        u = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(u)
        return u.get_service_client().get_container_client(u.CONTAINER)
    except Exception as ex:
        print(f"  [guard] blob unavailable ({ex}); using local baseline only")
        return None


def load_baseline():
    cc = _blob()
    if cc is not None:
        try:
            data = cc.download_blob(BASELINE_BLOB).readall()
            return json.loads(data)
        except Exception:
            pass
    if BASELINE_LOCAL.exists():
        try:
            return json.loads(BASELINE_LOCAL.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_baseline(counts):
    payload = {"updated_utc": datetime.now(timezone.utc).isoformat(), "counts": counts}
    CONFIG.mkdir(parents=True, exist_ok=True)
    BASELINE_LOCAL.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    cc = _blob()
    if cc is not None:
        try:
            cc.upload_blob(BASELINE_BLOB, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           overwrite=True)
        except Exception as ex:
            print(f"  [guard] baseline blob upload failed: {ex}")


def latest_manifest(subdir):
    d = BRONZE / subdir / "_manifests"
    if not d.exists():
        return None
    runs = sorted(d.glob("run_*.json"))
    return json.loads(runs[-1].read_text(encoding="utf-8")) if runs else None


def collect_counts():
    """Current row counts: opms__/sp__ sources (from manifests) + gold tables."""
    counts = {}
    errored = {}
    # OPMS
    m = latest_manifest("opms")
    for e in (m or {}).get("endpoints", []):
        key = "opms:" + e["name"]
        if "error" in e:
            counts[key] = 0
            errored[key] = e["error"]
        else:
            counts[key] = int(e.get("rows", 0))
    # SharePoint
    m = latest_manifest("bms")
    for l in (m or {}).get("lists", []):
        key = "bms:" + l["name"]
        if l.get("error"):
            counts[key] = 0
            errored[key] = l["error"]
        elif not l.get("schema_only"):
            counts[key] = int(l.get("rows", 0))
    # Gold
    for p in sorted(GOLD.glob("*.parquet")):
        try:
            counts["gold:" + p.stem] = pq.ParquetFile(p).metadata.num_rows
        except Exception:
            counts["gold:" + p.stem] = -1     # unreadable
    return counts, errored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="regression WARNs also fail the run")
    args = ap.parse_args()

    print("\n" + "=" * 60 + "\n[GUARD] anti-leak quality + completeness gate\n" + "=" * 60, flush=True)
    base = (load_baseline() or {}).get("counts", {})
    cur, errored = collect_counts()

    fails, warns = [], []

    # #1 critical gold tables exist & non-empty
    for t in CRITICAL_GOLD:
        k = "gold:" + t
        n = cur.get(k)
        if n is None:
            fails.append(f"critical gold table MISSING: {t}")
        elif n <= 0:
            fails.append(f"critical gold table EMPTY: {t}")

    # #1 row-count drop > 30% vs baseline (gold)
    for k, n in cur.items():
        if not k.startswith("gold:") or k in EXPECTED_EMPTY:
            continue
        b = base.get(k)
        if b and b > 0 and n >= 0 and n < b * (1 - DROP_THRESHOLD):
            warns.append(f"row drop {k}: {b:,} -> {n:,} ({100*(1-n/b):.0f}% down)")

    # #2 regression: had data last run, now 0 / errored
    for k, b in base.items():
        if k in EXPECTED_EMPTY:
            continue
        if b and b > 0 and cur.get(k, 0) == 0:
            why = errored.get(k, "0 rows")
            warns.append(f"REGRESSION {k}: was {b:,}, now {why}")

    report = {"ts": datetime.now(timezone.utc).isoformat(),
              "fails": fails, "warns": warns,
              "sources": len(cur), "errored": errored}
    try:
        CONFIG.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if fails:
        print(f"[GUARD] ❌ FAIL ({len(fails)}):")
        for f in fails:
            print("   ✗ " + f)
    if warns:
        print(f"[GUARD] ⚠ WARN ({len(warns)}):")
        for w in warns:
            print("   ! " + w)
    if not fails and not warns:
        print(f"[GUARD] ✅ PASS — {len(cur)} sources/tables checked, no leaks")

    # update baseline (so next run can compare). Always update on PASS/WARN; skip on FAIL.
    if not fails:
        save_baseline(cur)

    if fails or (args.strict and warns):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
