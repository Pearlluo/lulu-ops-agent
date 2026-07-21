"""
Extract OPMS Data API into the Bronze layer (NDJSON per endpoint).

OPMS is NOT a flat "list everything" API like SharePoint — endpoints are layered:
  Phase 1  no-param dimensions        -> always safe (employee/all, sites, positions, ...)
  Phase 2  tenant reference tables     -> need OPMS_TENANT (airports, countries, genders, ...)
  Phase 3  employee_id-driven          -> ids come from /employee/all (employee, roster, payslips, ...)
  Phase 4  site_id-driven              -> ids come from /sites (work_types, allowance_types, employee_codes)
  Phase 5  incremental facts           -> change_log / timesheets/entries / training/search

Auth mirrors `OPMS Token.py` (Basic client_credentials -> Bearer). Reuses credential/.env.

>>> THINGS YOU MUST SET / VERIFY (see CONFIG below) <<<
  1. OPMS_TENANT          -- required for Phase 2 reference tables (else they're skipped)
  2. WINDOW_START / WINDOW_END -- date window for roster / timesheets / training facts
  3. IDS_PARAM_STYLE      -- 'csv' or 'repeat': how the API wants employee_ids (verify with one call)
  4. TRAINING_SEARCH_STATUS -- required value for /training/search 'status' param
  5. TIMESHEETS_MODIFIED_SINCE -- required for /timesheets/entries (defaults to WINDOW_START)

Usage:
    python extract_opms.py --phase 1            # safe dimensions only (recommended first run)
    python extract_opms.py --phase 1 2 3 4 5    # everything configured
    python extract_opms.py                       # all phases (skips any that are unconfigured)
"""

import argparse
import json
import time
from base64 import b64encode
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
import os


# =========================================================
# PATHS & CONFIG
# =========================================================

SCRIPT_DIR = Path(__file__).resolve().parent          # .../Raw Data/API
DATA_DIR = SCRIPT_DIR.parent.parent                   # .../LuLuAgent/data
OUTPUT_ROOT = DATA_DIR / "bronze" / "opms"

load_dotenv(SCRIPT_DIR / "credential" / ".env")

CLIENT_ID = os.getenv("OPMS_CLIENT_ID")
CLIENT_SECRET = os.getenv("OPMS_CLIENT_SECRET")

API_BASE = "https://api.opms.com.au"
TOKEN_URL = "https://auth.opms.com.au/api/authenticate/token"

# ---- THINGS TO SET ----
OPMS_TENANT = os.getenv("OPMS_TENANT")                # e.g. "acme" or a GUID — None => Phase 2 skipped
WINDOW_START = "2024-01-01"                            # facts: roster / timesheets / training window start
# IMPORTANT: include FUTURE roster (+90d). 'Who is deployable' checks roster_date >= today —
# if the window stops at extraction day, future rostered staff falsely look available.
WINDOW_END = (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%Y-%m-%d")
IDS_PARAM_STYLE = "csv"                                # verified: OPMS wants ids=1,2,3 (csv)
# /training/search: no working offset param & page_size caps < count, so partition by employee_ids.
# Verified valid statuses (others 400): completed(10k)/pending/archived/declined/requested/cancelled.
TRAINING_SEARCH_STATUSES = ["completed", "pending", "archived", "declined", "requested", "cancelled"]
TRAINING_CHUNK = 20                                    # employees per /training/search call
TRAINING_PAGE_SIZE = 500                               # max safe page_size (verified 500 ok, 1000 -> 400)
TIMESHEETS_MODIFIED_SINCE = WINDOW_START + "T00:00:00Z"  # this endpoint needs full ISO datetime, not a date
TIMESHEET_PAGE_SIZE = 25                               # /timesheets/entries caps page_size at 25
CHANGE_LOG_CREATED_AFTER = None                        # None => full change_log; else ISO date for incremental

ID_CHUNK = 50                                          # how many ids per id-driven request
MAX_WINDOW_DAYS = 90                                    # OPMS roster/joballocations cap = 90 days per call
PAGE_SIZE = 200
SLEEP_SECONDS = 0.3
TIMEOUT_SECONDS = 60
MAX_RETRIES = 8
RETRY_STATUS = {429, 502, 503, 504}


# =========================================================
# AUTH (Basic client_credentials -> Bearer, cached)
# =========================================================

_token_cache = {"value": None, "expires_at": 0.0}


def get_token(force=False):
    if not force and _token_cache["value"] and time.time() < _token_cache["expires_at"] - 120:
        return _token_cache["value"]

    b64 = b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {b64}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(TOKEN_URL, headers=headers,
                                 data={"grant_type": "client_credentials"},
                                 timeout=TIMEOUT_SECONDS)
            if resp.status_code in RETRY_STATUS:
                time.sleep(SLEEP_SECONDS * attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            _token_cache["value"] = data["access_token"]
            _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600))
            return _token_cache["value"]
        except Exception as ex:
            print(f"  token attempt {attempt} failed: {ex}")
            time.sleep(SLEEP_SECONDS * attempt)
    raise RuntimeError("Unable to get OPMS token")


# =========================================================
# GET with retry / backoff / 401-refresh
# =========================================================

def opms_get(path, params=None):
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    for attempt in range(1, MAX_RETRIES + 1):
        token = get_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT_SECONDS)
            if resp.status_code == 401:
                get_token(force=True)
                continue
            if resp.status_code in RETRY_STATUS:
                wait = float(resp.headers.get("Retry-After", SLEEP_SECONDS * attempt))
                print(f"  {resp.status_code} retry {attempt}/{MAX_RETRIES} (wait {wait}s)")
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                # non-retryable client error (403/404/400/...) — fail fast
                raise RuntimeError(f"HTTP {resp.status_code} {url} :: {resp.text[:200]}")
            time.sleep(SLEEP_SECONDS)
            return resp.json()
        except (requests.ConnectionError, requests.Timeout) as ex:
            print(f"  network attempt {attempt} failed: {ex}")
            time.sleep(SLEEP_SECONDS * attempt)
    raise RuntimeError(f"GET failed after {MAX_RETRIES} retries: {url}")


def ids_param(values):
    """Format a list of ids per the configured style."""
    vals = [str(v) for v in values]
    if IDS_PARAM_STYLE == "repeat":
        return vals            # requests will repeat the key for a list value
    return ",".join(vals)      # csv


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def date_windows(start_str, end_str, max_days):
    """Yield (start, end) ISO date strings in <= max_days spans covering [start, end]."""
    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end = datetime.strptime(end_str, "%Y-%m-%d").date()
    cur = start
    while cur <= end:
        win_end = min(cur + timedelta(days=max_days), end)
        yield cur.strftime("%Y-%m-%d"), win_end.strftime("%Y-%m-%d")
        cur = win_end + timedelta(days=1)


# =========================================================
# WRITERS
# =========================================================

def out_dir_for(name, ingest_date):
    d = OUTPUT_ROOT / name.replace("/", "_") / f"ingest_date={ingest_date}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_rows(name, rows, ingest_date):
    d = out_dir_for(name, ingest_date)
    path = d / "items.ndjson"
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows), str(d)


def append_rows(fh, rows):
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def as_list(data):
    """Normalise OPMS responses to a list of records ({data:[...]} or [...] or {...})."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data.get("timesheets"), list):
            return data["timesheets"]
        return [data]
    return []


# =========================================================
# PHASE 1 — no-param dimensions (always safe)
# =========================================================

PHASE1 = [
    ("employee_all", "/employee/all", None),
    ("areas", "/areas", None),                 # spec didn't expose schema but returns data with no params
    ("employee_status", "/employee_status", None),
    ("employers", "/employers", None),
    ("positions", "/positions", None),
    ("companies", "/companies", None),
    ("sites", "/sites", None),
    ("users", "/users", None),
    ("competencies", "/competencies", None),
    ("expense_claim_categories", "/expense_claims/categories", None),
]


def run_phase1(ingest_date, manifest):
    print("\n=== PHASE 1: no-param dimensions ===")
    for name, path, params in PHASE1:
        try:
            rows = as_list(opms_get(path, params))
            n, loc = write_rows(name, rows, ingest_date)
            print(f"[{name}] {n} rows")
            manifest.append({"endpoint": path, "name": name, "rows": n, "path": loc})
        except Exception as ex:
            print(f"[{name}] FAILED: {ex}")
            manifest.append({"endpoint": path, "name": name, "error": str(ex)})

    # sites/employees — cursor 'after'
    try:
        rows, after = [], None
        while True:
            params = {"page_size": PAGE_SIZE}
            if after:
                params["after"] = after          # request param is 'after'; response carries 'next_cursor'
            data = opms_get("/sites/employees", params)
            batch = as_list(data)
            rows.extend(batch)
            after = data.get("next_cursor") if isinstance(data, dict) else None
            if not after or not batch:
                break
        n, loc = write_rows("sites_employees", rows, ingest_date)
        print(f"[sites_employees] {n} rows")
        manifest.append({"endpoint": "/sites/employees", "name": "sites_employees", "rows": n, "path": loc})
    except Exception as ex:
        print(f"[sites_employees] FAILED: {ex}")
        manifest.append({"endpoint": "/sites/employees", "name": "sites_employees", "error": str(ex)})


# =========================================================
# PHASE 2 — tenant reference tables (need OPMS_TENANT)
# =========================================================

PHASE2 = [
    ("airports", "/airports/{t}"),
    ("countries", "/countries/{t}"),
    ("genders", "/genders/{t}"),
    ("indigenous_status", "/indigenous_status/{t}"),
    ("nationalities", "/nationalities/{t}"),
]


def run_phase2(ingest_date, manifest):
    print("\n=== PHASE 2: tenant reference tables ===")
    if not OPMS_TENANT:
        print("  SKIPPED — set OPMS_TENANT in .env or CONFIG to enable.")
        return
    for name, tmpl in PHASE2:
        path = tmpl.format(t=OPMS_TENANT)
        try:
            rows = as_list(opms_get(path))
            n, loc = write_rows(name, rows, ingest_date)
            print(f"[{name}] {n} rows")
            manifest.append({"endpoint": path, "name": name, "rows": n, "path": loc})
        except Exception as ex:
            print(f"[{name}] FAILED: {ex}")
            manifest.append({"endpoint": path, "name": name, "error": str(ex)})


# =========================================================
# helpers to gather driving ids
# =========================================================

def load_employee_ids(ingest_date):
    path = OUTPUT_ROOT / "employee_all" / f"ingest_date={ingest_date}" / "items.ndjson"
    if not path.exists():
        print("  employee_all not found — run Phase 1 first.")
        return []
    ids = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if "id" in rec:
                ids.append(rec["id"])
    return ids


def load_site_ids(ingest_date):
    path = OUTPUT_ROOT / "sites" / f"ingest_date={ingest_date}" / "items.ndjson"
    if not path.exists():
        return []
    ids = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if "id" in rec:
                ids.append(rec["id"])
    return ids


# =========================================================
# PHASE 3 — employee_id-driven
# =========================================================

def run_phase3(ingest_date, manifest):
    print("\n=== PHASE 3: employee_id-driven ===")
    emp_ids = load_employee_ids(ingest_date)
    if not emp_ids:
        print("  SKIPPED — no employee ids (run Phase 1 first).")
        return
    print(f"  driving with {len(emp_ids)} employee ids, chunk={ID_CHUNK}, style={IDS_PARAM_STYLE}")

    # 3a. Full employee HR records  -> /employee?ids=
    _id_driven_stream("employee", "/employee", "ids", emp_ids, ingest_date, manifest)

    # 3b. Roster (date-windowed)    -> /roster?employee_ids=&start_date=&end_date=  (<=90d per call)
    _id_date_windowed_stream("roster", "/roster", "employee_ids", emp_ids, ingest_date, manifest)

    # 3c. Payslips                  -> /payslips?worker_ids=
    _id_driven_stream("payslips", "/payslips", "worker_ids", emp_ids, ingest_date, manifest)

    # 3d. Action items (per-employee views) -> /action_items/*?employee_ids=
    _id_driven_stream("action_outstanding_documents", "/action_items/outstanding_documents",
                      "employee_ids", emp_ids, ingest_date, manifest)
    _id_driven_stream("action_sign_requests", "/action_items/sign_requests",
                      "employee_ids", emp_ids, ingest_date, manifest)
    _id_driven_stream("action_job_allocations", "/action_items/job_allocations",
                      "employee_ids", emp_ids, ingest_date, manifest)

    # 3e. Job allocations (date-windowed; schema unexposed — pull raw if it returns)
    _id_date_windowed_stream("joballocations", "/joballocations", "employee_ids", emp_ids, ingest_date, manifest)


def _id_driven_stream(name, path, id_param, ids, ingest_date, manifest, extra=None):
    d = out_dir_for(name, ingest_date)
    total = 0
    try:
        with open(d / "items.ndjson", "w", encoding="utf-8") as fh:
            for chunk in chunked(ids, ID_CHUNK):
                params = dict(extra or {})
                params[id_param] = ids_param(chunk)
                batch = as_list(opms_get(path, params))
                append_rows(fh, batch)
                total += len(batch)
        print(f"[{name}] {total} rows")
        manifest.append({"endpoint": path, "name": name, "rows": total, "path": str(d)})
    except Exception as ex:
        print(f"[{name}] FAILED: {ex}")
        manifest.append({"endpoint": path, "name": name, "error": str(ex)})


def _id_date_windowed_stream(name, path, id_param, ids, ingest_date, manifest):
    """id-driven AND date-windowed (<= MAX_WINDOW_DAYS). Tags each row with its window."""
    d = out_dir_for(name, ingest_date)
    windows = list(date_windows(WINDOW_START, WINDOW_END, MAX_WINDOW_DAYS))
    total = 0
    try:
        with open(d / "items.ndjson", "w", encoding="utf-8") as fh:
            for (ws, we) in windows:
                for chunk in chunked(ids, ID_CHUNK):
                    params = {id_param: ids_param(chunk), "start_date": ws, "end_date": we}
                    batch = as_list(opms_get(path, params))
                    for r in batch:
                        if isinstance(r, dict):
                            r["_window_start"], r["_window_end"] = ws, we
                    append_rows(fh, batch)
                    total += len(batch)
            print(f"[{name}] {total} rows ({len(windows)} windows x {len(list(chunked(ids, ID_CHUNK)))} id-chunks)")
        manifest.append({"endpoint": path, "name": name, "rows": total, "path": str(d), "windows": len(windows)})
    except Exception as ex:
        print(f"[{name}] FAILED: {ex}")
        manifest.append({"endpoint": path, "name": name, "error": str(ex)})


# =========================================================
# PHASE 4 — site_id-driven
# =========================================================

def run_phase4(ingest_date, manifest):
    print("\n=== PHASE 4: site_id-driven ===")
    site_ids = load_site_ids(ingest_date)
    if not site_ids:
        print("  SKIPPED — no site ids (run Phase 1 first).")
        return
    print(f"  driving with {len(site_ids)} site ids")

    for name, tmpl in [("site_work_types", "/sites/{s}/work_types"),
                       ("site_allowance_types", "/sites/{s}/allowance_types")]:
        d = out_dir_for(name, ingest_date)
        total, bad = 0, 0
        with open(d / "items.ndjson", "w", encoding="utf-8") as fh:
            for sid in site_ids:
                try:                                     # per-site: one bad site must not kill the rest
                    batch = as_list(opms_get(tmpl.format(s=sid)))
                    for r in batch:
                        if isinstance(r, dict):
                            r["_site_id"] = sid          # tag with the driving site
                    append_rows(fh, batch)
                    total += len(batch)
                except Exception:
                    bad += 1
        note = f" ({bad}/{len(site_ids)} sites failed - 404/403)" if bad else ""
        print(f"[{name}] {total} rows{note}")
        manifest.append({"endpoint": tmpl, "name": name, "rows": total, "path": str(d), "sites_failed": bad})

    # employee_codes (site_ids optional, single call)
    try:
        rows = as_list(opms_get("/timesheets/employee_codes", {"site_ids": ids_param(site_ids)}))
        n, loc = write_rows("timesheet_employee_codes", rows, ingest_date)
        print(f"[timesheet_employee_codes] {n} rows")
        manifest.append({"endpoint": "/timesheets/employee_codes", "name": "timesheet_employee_codes",
                         "rows": n, "path": loc})
    except Exception as ex:
        print(f"[timesheet_employee_codes] FAILED: {ex}")
        manifest.append({"endpoint": "/timesheets/employee_codes", "name": "timesheet_employee_codes",
                         "error": str(ex)})


# =========================================================
# PHASE 5 — incremental facts (cursor / modified_since)
# =========================================================

def run_phase5(ingest_date, manifest):
    print("\n=== PHASE 5: incremental facts ===")

    # 5a. change_log — cursor 'next', optional created_after
    d = out_dir_for("change_log", ingest_date)
    total = 0
    try:
        with open(d / "items.ndjson", "w", encoding="utf-8") as fh:
            params = {"page_size": PAGE_SIZE}
            if CHANGE_LOG_CREATED_AFTER:
                params["created_after"] = CHANGE_LOG_CREATED_AFTER
            nxt = None
            while True:
                if nxt:
                    params["next"] = nxt
                data = opms_get("/change_log", params)
                batch = as_list(data)
                append_rows(fh, batch)
                total += len(batch)
                nxt = data.get("next") if isinstance(data, dict) else None
                if not nxt or not batch:
                    break
        print(f"[change_log] {total} rows")
        manifest.append({"endpoint": "/change_log", "name": "change_log", "rows": total, "path": str(d)})
    except Exception as ex:
        print(f"[change_log] FAILED: {ex}")
        manifest.append({"endpoint": "/change_log", "name": "change_log", "error": str(ex)})

    # 5b. timesheets/entries — cursor 'next_cursor', modified_since required
    d = out_dir_for("timesheet_entries", ingest_date)
    total = 0
    try:
        with open(d / "items.ndjson", "w", encoding="utf-8") as fh:
            params = {"modified_since": TIMESHEETS_MODIFIED_SINCE, "page_size": TIMESHEET_PAGE_SIZE}
            cursor = None
            while True:
                if cursor:
                    params["after"] = cursor       # response carries 'next_cursor'; request param is 'after'
                data = opms_get("/timesheets/entries", params)
                batch = as_list(data)
                append_rows(fh, batch)
                total += len(batch)
                cursor = data.get("next_cursor") if isinstance(data, dict) else None
                if not cursor or not batch:
                    break
        print(f"[timesheet_entries] {total} rows")
        manifest.append({"endpoint": "/timesheets/entries", "name": "timesheet_entries", "rows": total, "path": str(d)})
    except Exception as ex:
        print(f"[timesheet_entries] FAILED: {ex}")
        manifest.append({"endpoint": "/timesheets/entries", "name": "timesheet_entries", "error": str(ex)})

    # 5c. training/search — no working offset param; partition by status x employee_ids chunk.
    emp_ids = load_employee_ids(ingest_date)
    if not emp_ids:
        print("[training_search] SKIPPED — no employee ids (run Phase 1 first).")
        manifest.append({"endpoint": "/training/search", "name": "training_search", "skipped": "no employee ids"})
    else:
        d = out_dir_for("training_search", ingest_date)
        total, truncated = 0, 0
        try:
            with open(d / "items.ndjson", "w", encoding="utf-8") as fh:
                for status in TRAINING_SEARCH_STATUSES:
                    for chunk in chunked(emp_ids, TRAINING_CHUNK):
                        params = {"status": status, "employee_ids": ids_param(chunk),
                                  "page_size": TRAINING_PAGE_SIZE}
                        batch = as_list(opms_get("/training/search", params))
                        if len(batch) >= TRAINING_PAGE_SIZE:
                            truncated += 1          # chunk hit the page cap — may be incomplete
                        for r in batch:
                            if isinstance(r, dict):
                                r["_status"] = status
                        append_rows(fh, batch)
                        total += len(batch)
                    print(f"  training_search[{status}] cumulative {total}")
            note = f" (WARN: {truncated} chunks hit page cap)" if truncated else ""
            print(f"[training_search] {total} rows{note}")
            manifest.append({"endpoint": "/training/search", "name": "training_search",
                             "rows": total, "path": str(d), "capped_chunks": truncated})
        except Exception as ex:
            print(f"[training_search] FAILED: {ex}")
            manifest.append({"endpoint": "/training/search", "name": "training_search", "error": str(ex)})


# =========================================================
# MAIN
# =========================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", nargs="*", type=int, default=[1, 2, 3, 4, 5],
                        help="Which phases to run (default all)")
    args = parser.parse_args()

    ingest_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manifest = []
    run_started = datetime.now(timezone.utc).isoformat()

    print(f"OPMS extract | base={API_BASE} | window={WINDOW_START}..{WINDOW_END} | tenant={'SET' if OPMS_TENANT else 'UNSET'}")
    print("token ...", "OK len", len(get_token()))

    if 1 in args.phase:
        run_phase1(ingest_date, manifest)
    if 2 in args.phase:
        run_phase2(ingest_date, manifest)
    if 3 in args.phase:
        run_phase3(ingest_date, manifest)
    if 4 in args.phase:
        run_phase4(ingest_date, manifest)
    if 5 in args.phase:
        run_phase5(ingest_date, manifest)

    mdir = OUTPUT_ROOT / "_manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    mpath = mdir / f"run_{ingest_date}_{datetime.now(timezone.utc).strftime('%H%M%S')}.json"
    mpath.write_text(json.dumps({
        "run_started_utc": run_started,
        "run_finished_utc": datetime.now(timezone.utc).isoformat(),
        "ingest_date": ingest_date,
        "window": [WINDOW_START, WINDOW_END],
        "phases": args.phase,
        "endpoints": manifest,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(m.get("rows", 0) for m in manifest)
    ok = sum(1 for m in manifest if "error" not in m and "skipped" not in m)
    print(f"\nDONE. {ok} endpoints, {total} total rows. Manifest: {mpath}")


if __name__ == "__main__":
    main()
