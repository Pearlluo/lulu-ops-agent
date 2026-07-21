"""
Full lake refresh orchestrator: Bronze -> upload -> Silver/Gold -> flat -> watermark.

Runs the same scripts you run by hand, in order, as one job. Designed to run either
locally or inside a scheduled cloud container (Azure Container Apps Job). On each run it
does a FULL refresh (re-extract everything, rebuild, overwrite blob) — simplest path to
keep the agent's Gold layer always current. Incremental can be layered on later.

Exit non-zero if any step fails (so the scheduler marks the run failed).

Run:
    python run_pipeline.py                 # full pipeline
    python run_pipeline.py --skip-extract  # rebuild silver/gold from existing bronze only
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PY = sys.executable


def step(name, args):
    print(f"\n{'='*60}\n[STEP] {name}\n{'='*60}", flush=True)
    t0 = time.time()
    r = subprocess.run([PY, "-u", str(SCRIPT_DIR / args[0]), *args[1:]], cwd=str(SCRIPT_DIR))
    dt = time.time() - t0
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] {name} exited {r.returncode} after {dt:.0f}s")
    print(f"[OK] {name} ({dt:.0f}s)", flush=True)


def update_watermark(run_started):
    """Write a fresh watermark to blob config/watermarks.json (full-refresh timestamp)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("u", str(SCRIPT_DIR / "upload_to_blob.py"))
    u = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(u)
    from azure.storage.blob import ContentSettings
    cc = u.get_service_client().get_container_client(u.CONTAINER)
    wm = {
        "last_run_started_utc": run_started,
        "last_run_finished_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "full_refresh",
        "sharepoint": {"incremental_field": "Modified", "last_modified_watermark": run_started},
        "opms": {"change_log_created_after": run_started,
                 "timesheets_modified_since": run_started},
    }
    cc.upload_blob("config/watermarks.json", json.dumps(wm, ensure_ascii=False, indent=2).encode("utf-8"),
                   overwrite=True, content_settings=ContentSettings(content_type="application/json"))
    print("[OK] watermark updated in blob", flush=True)


def upload_agent_state():
    """Upload the UI freshness/audit files the agent reads (currently link_health.json, written by
    check_links.py) to blob under state/. The cloud app pulls these so the dashboard isn't stuck on
    whatever was baked into its image. Reuses the same blob client as the layer uploads."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("u", str(SCRIPT_DIR / "upload_to_blob.py"))
    u = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(u)
    from azure.storage.blob import ContentSettings
    cc = u.get_service_client().get_container_client(u.CONTAINER)
    agent_dir = SCRIPT_DIR.parent.parent / "agent"          # data/agent (same place check_links writes)
    for fname in ("link_health.json",):
        f = agent_dir / fname
        if f.exists():
            cc.upload_blob(f"state/{fname}", f.read_bytes(), overwrite=True,
                           content_settings=ContentSettings(content_type="application/json"))
            print(f"[OK] uploaded state/{fname} to blob", flush=True)
        else:
            print(f"[WARN] {f} not found — skipped state upload", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-extract", action="store_true", help="rebuild from existing bronze only")
    args = ap.parse_args()

    run_started = datetime.now(timezone.utc).isoformat()
    print(f"PIPELINE START {run_started}", flush=True)

    if not args.skip_extract:
        step("Extract OPMS",        ["extract_opms.py"])
        step("Extract SharePoint",  ["extract_sharepoint_bms.py"])
        step("Extract site files",  ["extract_bms_files.py"])   # BMS+IMS+FDS -> gold file_index (find_files)
        step("Extract FDS lists",   ["extract_sharepoint_bms.py", "--site", "FDS"])  # -> gold fds_* tables
        step("Upload Bronze",       ["upload_to_blob.py"])

    step("Build Silver + Gold", ["build_silver_gold.py"])
    step("Build Silver flat",   ["build_silver_flat.py"])
    step("Quality Gate",        ["pipeline_guard.py"])     # anti-leak: FAIL on empty critical Gold
    step("Link Health",         ["check_links.py", "--repair"])  # audit + auto-fix stale folder links (backed up, idempotent); -> data/agent/link_health.json

    try:
        upload_agent_state()                                 # push link_health.json to blob state/ so the cloud app can pull it
    except Exception as ex:
        print(f"[WARN] agent-state upload failed: {ex}", flush=True)

    try:
        update_watermark(run_started)
    except Exception as ex:
        print(f"[WARN] watermark update failed: {ex}", flush=True)

    print(f"\nPIPELINE DONE {datetime.now(timezone.utc).isoformat()}", flush=True)


if __name__ == "__main__":
    main()
