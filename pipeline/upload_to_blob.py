"""
Upload the local Bronze lake to Azure Blob Storage in the lulu-data layout.

Target container `lulu-data`:
    bronze/sharepoint/...   <- local data/bronze/bms/
    bronze/opms/...         <- local data/bronze/opms/
    silver/  gold/          <- placeholders (.keep) for future Parquet
    config/entity_registry.yaml
    config/watermarks.json

Auth: reads an Azure Storage connection string from .env. It auto-detects the key
name among the common ones below (value is never printed). If your key has a
different name, add it to CONN_ENV_NAMES or set CONTAINER/account vars directly.

Run:
    pip install azure-storage-blob python-dotenv
    python upload_to_blob.py              # full upload (idempotent, overwrites)
    python upload_to_blob.py --dry-run    # list what WOULD upload, no network
    python upload_to_blob.py --config-only  # only (re)write config/ + placeholders
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent.parent              # .../LuLuAgent/data
BRONZE_DIR = DATA_DIR / "bronze"

load_dotenv(SCRIPT_DIR / "credential" / ".env")

CONTAINER = "lulu-data"

# local-folder -> blob-prefix mapping (note bms -> sharepoint rename)
SOURCES = {
    BRONZE_DIR / "bms": "bronze/sharepoint",
    BRONZE_DIR / "opms": "bronze/opms",
}

# .env key names this script will look for (first one set wins)
CONN_ENV_NAMES = [
    "AZURE_STORAGE_CONNECTION_STRING",
    "BLOB_CONNECTION_STRING",
    "AZURE_BLOB_CONNECTION_STRING",
    "STORAGE_CONNECTION_STRING",
    "BLOB_CONN_STR",
]
# alternative: account URL + SAS / key
ACCOUNT_URL_NAMES = ["AZURE_STORAGE_ACCOUNT_URL", "BLOB_ACCOUNT_URL", "AZURE_BLOB_URL"]
SAS_NAMES = ["AZURE_STORAGE_SAS_TOKEN", "BLOB_SAS_TOKEN", "AZURE_SAS"]
KEY_NAMES = ["AZURE_STORAGE_KEY", "BLOB_ACCOUNT_KEY", "AZURE_STORAGE_ACCOUNT_KEY"]


def get_service_client():
    from azure.storage.blob import BlobServiceClient

    for name in CONN_ENV_NAMES:
        val = os.getenv(name)
        if val:
            print(f"auth: connection string via .env key '{name}'")
            return BlobServiceClient.from_connection_string(val)

    url = next((os.getenv(n) for n in ACCOUNT_URL_NAMES if os.getenv(n)), None)
    sas = next((os.getenv(n) for n in SAS_NAMES if os.getenv(n)), None)
    key = next((os.getenv(n) for n in KEY_NAMES if os.getenv(n)), None)
    if url and (sas or key):
        print(f"auth: account url + {'SAS' if sas else 'key'}")
        return BlobServiceClient(account_url=url, credential=(sas or key))

    raise RuntimeError(
        "No Azure Blob credentials found in .env. Looked for: "
        + ", ".join(CONN_ENV_NAMES + ACCOUNT_URL_NAMES)
        + ". Tell me the exact key name you used."
    )


def iter_files(root):
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def content_type_for(path):
    s = path.suffix.lower()
    return {".json": "application/json", ".ndjson": "application/x-ndjson",
            ".yaml": "application/x-yaml", ".parquet": "application/octet-stream",
            ".md": "text/markdown"}.get(s, "application/octet-stream")


def build_config_files(ingest_date):
    """Generate starter config/ content from the local bronze manifests."""
    # watermarks: seed last-run per source from the latest bronze ingest_date
    watermarks = {
        "_note": "Last successful extraction watermark per source/object. Updated by the extractors.",
        "sharepoint": {"last_full_ingest_date": ingest_date,
                       "incremental_field": "Modified",
                       "last_modified_watermark": None},
        "opms": {"last_full_ingest_date": ingest_date,
                 "change_log_created_after": None,
                 "timesheets_modified_since": None,
                 "training_modified_since": None,
                 "roster_window_days": 90},
    }

    entity_registry = """# entity_registry.yaml — maps Bronze objects to Silver/Gold entities.
# Source of truth for which bronze object feeds which curated entity.
# Authoritative field schemas: the extracted _columns.json (BMS) / item shapes (OPMS).
version: 1
silver:
  dim_person:
    sources: [bronze/opms/employee, bronze/sharepoint/PPL-People]
    join_key: opms_employee_id   # OPMS employee.id == BMS PPL-People.OPMSID
    pk: opms_employee_id
  dim_position:
    sources: [bronze/opms/positions, bronze/sharepoint/PPL-Positions]
    join_key: opms_position_id
    pk: opms_position_id
  dim_project:
    sources: [bronze/sharepoint/JMS-Projects]
    pk: bms_project_id
  dim_client:
    sources: [bronze/sharepoint/JMS-Clients]
    pk: bms_client_id
gold:
  employee_profile:
    sources: [silver/dim_person, silver/dim_position]
  roster_summary:
    sources: [bronze/opms/roster, bronze/sharepoint/PPL-Rosters]
    grain: [opms_employee_id, roster_date]
"""
    return json.dumps(watermarks, ensure_ascii=False, indent=2), entity_registry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config-only", action="store_true")
    args = ap.parse_args()

    ingest_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    watermarks_json, entity_registry_yaml = build_config_files(ingest_date)

    # plan the bronze upload
    plan = []
    if not args.config_only:
        for local_root, prefix in SOURCES.items():
            if not local_root.exists():
                print(f"WARN: {local_root} missing — skipped")
                continue
            for f in iter_files(local_root):
                rel = f.relative_to(local_root).as_posix()
                plan.append((f, f"{prefix}/{rel}"))

    total_bytes = sum(f.stat().st_size for f, _ in plan)
    print(f"container: {CONTAINER}")
    print(f"bronze files to upload: {len(plan)} ({total_bytes/1024/1024:.1f} MB)")
    print("config files: config/entity_registry.yaml, config/watermarks.json")
    print("placeholders: silver/.keep, gold/.keep")

    if args.dry_run:
        for _, blob in plan[:15]:
            print("  would upload ->", blob)
        if len(plan) > 15:
            print(f"  ... and {len(plan)-15} more")
        return

    svc = get_service_client()
    try:
        svc.create_container(CONTAINER)
        print(f"created container {CONTAINER}")
    except Exception:
        print(f"container {CONTAINER} already exists")
    cc = svc.get_container_client(CONTAINER)

    from azure.storage.blob import ContentSettings

    # config + placeholders
    cc.upload_blob("config/watermarks.json", watermarks_json.encode("utf-8"), overwrite=True,
                   content_settings=ContentSettings(content_type="application/json"))
    cc.upload_blob("config/entity_registry.yaml", entity_registry_yaml.encode("utf-8"), overwrite=True,
                   content_settings=ContentSettings(content_type="application/x-yaml"))
    for ph in ("silver/.keep", "gold/.keep"):
        cc.upload_blob(ph, b"", overwrite=True)
    print("config/ + placeholders written")

    # bronze
    done = 0
    sent = 0
    for f, blob_name in plan:
        with open(f, "rb") as fh:
            cc.upload_blob(blob_name, fh, overwrite=True,
                           content_settings=ContentSettings(content_type=content_type_for(f)))
        done += 1
        sent += f.stat().st_size
        if done % 25 == 0 or done == len(plan):
            print(f"  {done}/{len(plan)} files, {sent/1024/1024:.1f} MB")

    print(f"\nDONE. Uploaded {done} bronze files + config to {CONTAINER}/")


if __name__ == "__main__":
    main()
