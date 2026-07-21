"""
Extract all SharePoint BMS lists via Microsoft Graph.

- Reuses the client-credentials flow from `Sharepoint Token.py` / credential/.env
- Lands raw items as NDJSON in the Bronze layer, one folder per list
- Saves each list's column schema + a run manifest
- Retries 429/5xx (honours Retry-After), refreshes token on 401
- Excludes credential / log / test lists; sensitive lists are schema-only by default

Usage:
    python extract_sharepoint_bms.py                  # full pull, all eligible lists
    python extract_sharepoint_bms.py --lists PPL-People JMS-Jobs
    python extract_sharepoint_bms.py --since 2026-06-01T00:00:00Z   # incremental on Modified
    python extract_sharepoint_bms.py --include-sensitive            # also pull sensitive values
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


# =========================================================
# PATHS & CONFIG
# =========================================================

SCRIPT_DIR = Path(__file__).resolve().parent          # .../Raw Data/API
DATA_DIR = SCRIPT_DIR.parent.parent                   # .../LuLuAgent/data
OUTPUT_ROOT = DATA_DIR / "bronze" / "bms"             # .../data/bronze/bms

load_dotenv(SCRIPT_DIR / "credential" / ".env")

TENANT_ID = os.getenv("SHAREPOINT_TENANT_ID")
CLIENT_ID = os.getenv("SHAREPOINT_CLIENT_ID")
CLIENT_SECRET = os.getenv("SHAREPOINT_CLIENT_SECRET")

GRAPH = "https://graph.microsoft.com/v1.0"
SITE_HOSTNAME = "yourtenant.sharepoint.com"
SITE_PATH = "/sites/BMS"

PAGE_SIZE = 200                                       # Graph items page size (stable with $expand=fields)
SLEEP_SECONDS = 0.2                                   # polite throttle between requests
TIMEOUT_SECONDS = 60
MAX_RETRIES = 8
RETRY_STATUS = {429, 500, 502, 503, 504}

# Never extract (credentials / operational logs / test / empty placeholders / SP system lists)
EXCLUDE_LISTS = {
    "SYS-APITokens",
    "SYS-FlowErrorLog",
    "PPL-PeopleTest",
    "AMS-AssetsTest",
    "TestList",
    # SharePoint system / default lists (not business data)
    "Access Requests",
    "Web Template Extensions",
    "Events",
    "Documents",
}

# Sensitive: pull column schema only, NO item values (unless --include-sensitive)
SENSITIVE_LISTS = {
    "PPL-PayrollDeductions",
    "Archived Staff Data",
}


# =========================================================
# AUTH (mirrors Sharepoint Token.py, with caching + refresh)
# =========================================================

_token_cache = {"value": None, "expires_at": 0.0}


def get_token(force=False):
    if not force and _token_cache["value"] and time.time() < _token_cache["expires_at"] - 120:
        return _token_cache["value"]

    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(token_url, data=payload, timeout=TIMEOUT_SECONDS)
            if resp.status_code in RETRY_STATUS:
                time.sleep(SLEEP_SECONDS * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            _token_cache["value"] = data["access_token"]
            _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600))
            return _token_cache["value"]
        except Exception as ex:
            print(f"  token attempt {attempt + 1} failed: {ex}")
            time.sleep(SLEEP_SECONDS * (attempt + 1))

    raise RuntimeError("Unable to get SharePoint/Graph token")


# =========================================================
# GRAPH GET with retry / backoff / 401-refresh
# =========================================================

def graph_get(url, params=None):
    for attempt in range(MAX_RETRIES):
        token = get_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT_SECONDS)

            if resp.status_code == 401:
                get_token(force=True)
                continue

            if resp.status_code in RETRY_STATUS:
                wait = float(resp.headers.get("Retry-After", SLEEP_SECONDS * (attempt + 1)))
                print(f"  {resp.status_code} retry {attempt + 1}/{MAX_RETRIES} (wait {wait}s)")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            time.sleep(SLEEP_SECONDS)
            return resp.json()

        except requests.RequestException as ex:
            print(f"  request attempt {attempt + 1} failed: {ex}")
            time.sleep(SLEEP_SECONDS * (attempt + 1))

    raise RuntimeError(f"GET failed after {MAX_RETRIES} retries: {url}")


# =========================================================
# SITE / LISTS / COLUMNS
# =========================================================

def resolve_site_id():
    data = graph_get(f"{GRAPH}/sites/{SITE_HOSTNAME}:{SITE_PATH}")
    print(f"Site: {data.get('displayName')}  id={data['id']}")
    return data["id"]


def list_all_lists(site_id):
    """Return [(name, list_id, item_count, template)] for every list on the site."""
    out = []
    url = f"{GRAPH}/sites/{site_id}/lists"
    params = {"$select": "id,name,displayName,list", "$top": 200, "$expand": "drive"}
    while url:
        data = graph_get(url, params=params)
        params = None  # nextLink already carries the query
        for lst in data.get("value", []):
            name = lst.get("displayName") or lst.get("name")
            template = (lst.get("list") or {}).get("template")
            out.append((name, lst["id"], template))
        url = data.get("@odata.nextLink")
    return out


def get_list_columns(site_id, list_id):
    data = graph_get(f"{GRAPH}/sites/{site_id}/lists/{list_id}/columns")
    return data.get("value", [])


# =========================================================
# ITEM EXTRACTION
# =========================================================

def module_of(list_name):
    """PPL-People -> ppl ; 'Archived Staff Data' -> misc"""
    if "-" in list_name:
        return list_name.split("-", 1)[0].lower()
    return "misc"


def safe_name(list_name):
    return list_name.replace(" ", "_").replace("/", "_")


def extract_list_items(site_id, list_id, list_name, out_dir, since=None):
    """Page all items (fields expanded) into NDJSON. Returns row count."""
    items_path = out_dir / "items.ndjson"
    count = 0

    params = {"$expand": "fields", "$top": PAGE_SIZE}
    if since:
        # Modified is indexed; Prefer header lets non-indexed-safe filters through
        params["$filter"] = f"fields/Modified gt '{since}'"

    url = f"{GRAPH}/sites/{site_id}/lists/{list_id}/items"
    headers_note = bool(since)

    with open(items_path, "w", encoding="utf-8") as fh:
        first = True
        while url:
            if first and headers_note:
                # one-off call needs the Prefer header for $filter on fields
                token = get_token()
                resp = requests.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Prefer": "HonorNonIndexedQueriesWarningMayFailRandomly",
                    },
                    params=params,
                    timeout=TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                data = resp.json()
                time.sleep(SLEEP_SECONDS)
            else:
                data = graph_get(url, params=params)

            params = None
            first = False

            for item in data.get("value", []):
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                count += 1

            url = data.get("@odata.nextLink")
            if count and count % 2000 == 0:
                print(f"    ... {count} rows")

    return count


# =========================================================
# MAIN
# =========================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default="BMS",
                        help="site short name under /sites/ — output lands in bronze/<site lower>/")
    parser.add_argument("--lists", nargs="*", help="Only these list display names")
    parser.add_argument("--since", help="ISO8601, incremental on Modified, e.g. 2026-06-01T00:00:00Z")
    parser.add_argument("--include-sensitive", action="store_true",
                        help="Also pull item values for sensitive lists")
    args = parser.parse_args()

    global SITE_PATH, OUTPUT_ROOT
    SITE_PATH = f"/sites/{args.site}"
    OUTPUT_ROOT = DATA_DIR / "bronze" / args.site.lower()

    ingest_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_started = datetime.now(timezone.utc).isoformat()

    site_id = resolve_site_id()
    all_lists = list_all_lists(site_id)
    print(f"Discovered {len(all_lists)} lists on the site.\n")

    targets = []
    for name, list_id, template in all_lists:
        if name in EXCLUDE_LISTS or name.startswith("PageDiagnosticsResultList"):
            continue
        if template == "documentLibrary" and args.site != "BMS":
            continue                       # non-BMS library files live in file_index (extract_bms_files.py);
                                           # BMS keeps its historical behaviour (library lists extracted too)
        if args.lists and name not in args.lists:
            continue
        targets.append((name, list_id, template))

    if args.lists:
        missing = set(args.lists) - {t[0] for t in targets}
        for m in missing:
            print(f"  WARNING: requested list not found / excluded: {m}")

    manifest = {
        "run_started_utc": run_started,
        "ingest_date": ingest_date,
        "site_id": site_id,
        "mode": "incremental" if args.since else "full",
        "since": args.since,
        "lists": [],
    }

    for name, list_id, template in targets:
        module = module_of(name)
        out_dir = OUTPUT_ROOT / module / safe_name(name) / f"ingest_date={ingest_date}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Always save the column schema
        try:
            columns = get_list_columns(site_id, list_id)
            (out_dir / "_columns.json").write_text(
                json.dumps(columns, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as ex:
            columns = []
            print(f"  [{name}] columns failed: {ex}")

        is_sensitive = name in SENSITIVE_LISTS and not args.include_sensitive

        if is_sensitive:
            print(f"[{name}] SENSITIVE -> schema only ({len(columns)} columns), no values")
            manifest["lists"].append({
                "name": name, "module": module, "template": template,
                "schema_only": True, "rows": 0, "path": str(out_dir),
            })
            continue

        try:
            print(f"[{name}] extracting (template={template}) ...")
            rows = extract_list_items(site_id, list_id, name, out_dir, since=args.since)
            print(f"[{name}] done: {rows} rows -> {out_dir}")
            manifest["lists"].append({
                "name": name, "module": module, "template": template,
                "schema_only": False, "rows": rows, "path": str(out_dir),
            })
        except Exception as ex:
            print(f"[{name}] FAILED: {ex}")
            manifest["lists"].append({
                "name": name, "module": module, "template": template,
                "error": str(ex), "path": str(out_dir),
            })

    manifest["run_finished_utc"] = datetime.now(timezone.utc).isoformat()
    manifest_dir = OUTPUT_ROOT / "_manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"run_{ingest_date}_{datetime.now(timezone.utc).strftime('%H%M%S')}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for l in manifest["lists"] if "error" not in l and not l.get("schema_only"))
    total_rows = sum(l.get("rows", 0) for l in manifest["lists"])
    print(f"\nDONE. {ok} lists extracted, {total_rows} total rows. Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
