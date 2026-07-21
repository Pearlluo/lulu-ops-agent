"""
Extract EVERY file in the SharePoint BMS site's document libraries via Microsoft Graph.

- Same client-credentials flow as extract_sharepoint_bms.py (credential/.env)
- Enumerates all drives (document libraries) on /sites/BMS, then walks each drive with
  the delta API (no recursion, one request per page, throttling-safe)
- Lands one NDJSON line per FILE (folders are skipped) in bronze/bms_files/files.jsonl
  -> gold table `file_index` (built by build_silver_gold.build_file_index)

Usage:
    python extract_bms_files.py                # all libraries on BMS
    python extract_bms_files.py --site IMS     # another site (future use)
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent.parent
OUT_DIR = DATA_DIR / "bronze" / "bms_files"

load_dotenv(SCRIPT_DIR / "credential" / ".env")
TENANT_ID = os.getenv("SHAREPOINT_TENANT_ID")
CLIENT_ID = os.getenv("SHAREPOINT_CLIENT_ID")
CLIENT_SECRET = os.getenv("SHAREPOINT_CLIENT_SECRET")

GRAPH = "https://graph.microsoft.com/v1.0"
HOSTNAME = "yourtenant.sharepoint.com"
TIMEOUT = 60
MAX_RETRIES = 8
RETRY_STATUS = {429, 500, 502, 503, 504}
SELECT = "id,name,size,file,folder,parentReference,webUrl,lastModifiedDateTime,lastModifiedBy"


def get_token():
    r = requests.post(f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
                      data={"grant_type": "client_credentials", "client_id": CLIENT_ID,
                            "client_secret": CLIENT_SECRET,
                            "scope": "https://graph.microsoft.com/.default"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["access_token"]


def gget(session, url):
    """GET with retry on throttle/5xx and one token refresh on 401."""
    for attempt in range(MAX_RETRIES):
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code == 401 and attempt == 0:
            session.headers["Authorization"] = "Bearer " + get_token()
            continue
        if r.status_code in RETRY_STATUS:
            time.sleep(float(r.headers.get("Retry-After", 2)) + attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Graph GET kept failing: {url[:120]}")


def walk_drive(session, site_name, drive, writer):
    """Delta-walk one document library; write one line per file. Returns file count."""
    n = 0
    url = f"{GRAPH}/drives/{drive['id']}/root/delta?$select={SELECT}&$top=500"
    while url:
        j = gget(session, url)
        for it in j.get("value", []):
            if "file" not in it:                      # folders / deleted markers
                continue
            name = it.get("name") or ""
            # parentReference.path looks like '/drives/<id>/root:/General/Logos'
            raw_path = (it.get("parentReference") or {}).get("path", "")
            folder = raw_path.split("root:", 1)[-1].lstrip("/") if "root:" in raw_path else ""
            writer.write(json.dumps({
                "site": site_name,
                "library": drive.get("name"),
                "file_name": name,
                "ext": name.rsplit(".", 1)[-1].lower() if "." in name else "",
                "folder_path": folder,
                "size_kb": round((it.get("size") or 0) / 1024, 1),
                "web_url": it.get("webUrl"),
                "modified_at": it.get("lastModifiedDateTime"),
                "modified_by": ((it.get("lastModifiedBy") or {}).get("user") or {}).get("displayName"),
            }, ensure_ascii=False) + "\n")
            n += 1
        url = j.get("@odata.nextLink")                # deltaLink = done
    return n


def extract_site(session, site_name, writer):
    """All document-library files of one site. Returns {library: count}."""
    site = gget(session, f"{GRAPH}/sites/{HOSTNAME}:/sites/{site_name}")
    drives = gget(session, f"{GRAPH}/sites/{site['id']}/drives?$top=200").get("value", [])
    print(f"[files] site {site_name}: {len(drives)} document librar{'y' if len(drives) == 1 else 'ies'}", flush=True)
    per_lib = {}
    for d in drives:
        n = walk_drive(session, site_name, d, writer)
        per_lib[d.get("name")] = n
        print(f"  {site_name}/{d.get('name')}: {n} files", flush=True)
    return per_lib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", nargs="+", default=["BMS", "IMS", "FDS"],
                    help="site short names under /sites/ (default: BMS IMS FDS)")
    args = ap.parse_args()

    t0 = time.time()
    s = requests.Session()
    s.headers["Authorization"] = "Bearer " + get_token()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DIR / "files.jsonl.tmp"
    total, manifest = 0, {}
    with open(tmp, "w", encoding="utf-8") as w:
        for site_name in args.sites:
            per_lib = extract_site(s, site_name, w)
            manifest[site_name] = per_lib
            total += sum(per_lib.values())
    tmp.replace(OUT_DIR / "files.jsonl")

    (OUT_DIR / "manifest.json").write_text(json.dumps({
        "sites": manifest, "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_files": total,
        "duration_s": round(time.time() - t0, 1)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] {total} files -> {OUT_DIR / 'files.jsonl'} ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
