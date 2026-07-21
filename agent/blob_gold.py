"""blob_gold.py — keep the cloud app's Gold lake fresh by pulling the nightly-built
parquet from Azure Blob, instead of relying on what was baked into the image.

The nightly `lulu-refresh` job already builds Gold and uploads it to blob
`lulu-data/gold/` (build_silver_gold.py -> upload_layer(GOLD, "gold")). This module
is the matching PULL side: the app downloads those parquet on startup / on a TTL.

Design:
- No-op when BLOB_CONNECTION_STRING is absent (local dev) -> the baked/local gold is used.
- TTL-guarded via a marker file so a long-running replica only re-pulls periodically.
- Fail-safe: any error leaves the existing on-disk gold untouched (returns None).
- Atomic-ish writes (download to .tmp, then replace) so a half-download can't be read.
"""
import os
import time
from pathlib import Path

GOLD_DIR = Path(__file__).resolve().parents[1] / "gold"   # data/gold
AGENT_DIR = Path(__file__).resolve().parent               # data/agent
_CONTAINER = "lulu-data"
_PREFIX = "gold/"
_MARKER = GOLD_DIR / ".blob_sync"                          # epoch of last successful pull
_STATE_MARKER = GOLD_DIR / ".state_sync"                   # epoch of last state-file (link audit) pull
# non-gold UI freshness/audit files the pipeline mirrors under blob state/ (see run_pipeline.upload_agent_state)
_STATE_FILES = ("link_health.json",)
_CONN_KEYS = ("BLOB_CONNECTION_STRING", "AZURE_STORAGE_CONNECTION_STRING",
              "AZURE_BLOB_CONNECTION_STRING", "STORAGE_CONNECTION_STRING")


def _conn_str():
    for k in _CONN_KEYS:
        v = os.getenv(k)
        if v:
            return v
    return None


def _in_cloud():
    # Azure Container Apps injects these at runtime; absent on the admin's local machine.
    # Gating on this prevents a local run from overwriting the (better, Xero-fresh) LOCAL gold
    # with the cloud-built blob gold.
    return bool(os.getenv("CONTAINER_APP_NAME") or os.getenv("CONTAINER_APP_REVISION"))


def last_pull_epoch():
    try:
        return int(_MARKER.read_text().strip())
    except Exception:
        return None


def pull_gold(force=False, ttl_seconds=1800):
    """Download gold/*.parquet from blob into GOLD_DIR.

    Returns the number of files downloaded (int > 0) when a real sync happened,
    False when skipped by the TTL, and None when there's no connection string or
    the pull failed (existing gold is left in place either way).
    """
    cs = _conn_str()
    if not cs or not _in_cloud():
        return None                                       # local dev: use whatever's on disk
    if not force and _MARKER.exists():
        try:
            if time.time() - _MARKER.stat().st_mtime < ttl_seconds:
                return False                              # synced recently — skip the network call
        except Exception:
            pass
    try:
        from azure.storage.blob import BlobServiceClient
        cc = BlobServiceClient.from_connection_string(cs).get_container_client(_CONTAINER)
        GOLD_DIR.mkdir(parents=True, exist_ok=True)
        n = 0
        for b in cc.list_blobs(name_starts_with=_PREFIX):
            name = b.name.split("/", 1)[-1]               # strip the "gold/" prefix
            if not name.endswith(".parquet"):
                continue
            dest = GOLD_DIR / name
            tmp = dest.with_name(dest.name + ".tmp")
            with open(tmp, "wb") as f:
                f.write(cc.download_blob(b.name).readall())
            tmp.replace(dest)                             # swap in only once fully written
            n += 1
        _MARKER.write_text(str(int(time.time())))
        return n
    except Exception:
        return None                                       # network/auth error -> keep existing gold


def regenerate_local_state():
    """Cloud-only: recompute the freshness artifacts that aren't in blob — data_quality_report.json
    (sentinel) and snapshots.jsonl + today's brief (daily_brief) — from the freshly-pulled gold, so
    they aren't stuck at the image-build date. The app image has every dep these scripts need; runs
    them as subprocesses with safe flags. Fail-safe: any error leaves the baked files in place.
    Returns the number of scripts that succeeded (0-2), or None when not in the cloud."""
    if not _in_cloud():
        return None
    import subprocess
    import sys as _sys
    ok = 0
    for argv in (["data_quality_sentinel.py"], ["daily_brief.py", "--no-email"]):
        try:
            r = subprocess.run([_sys.executable, str(AGENT_DIR / argv[0]), *argv[1:]],
                               cwd=str(AGENT_DIR), timeout=240, capture_output=True)
            if r.returncode == 0:
                ok += 1
        except Exception:
            pass
    return ok


def pull_state(force=False, ttl_seconds=600):
    """Download blob state/<file> (link_health.json, ...) into data/agent/. Cloud-only, fail-safe.
    These are non-gold UI freshness files (e.g. the folder-link audit) the nightly job uploads.
    Has its own TTL so it can run on every page load INDEPENDENTLY of pull_gold() — gold only
    changes nightly, but the link audit must not stay stale all day because gold didn't move."""
    cs = _conn_str()
    if not cs or not _in_cloud():
        return None
    if not force and _STATE_MARKER.exists():
        try:
            if time.time() - _STATE_MARKER.stat().st_mtime < ttl_seconds:
                return False                               # pulled recently — skip the network call
        except Exception:
            pass
    try:
        from azure.storage.blob import BlobServiceClient
        cc = BlobServiceClient.from_connection_string(cs).get_container_client(_CONTAINER)
        n = 0
        for fname in _STATE_FILES:
            try:
                data = cc.download_blob(f"state/{fname}").readall()
            except Exception:
                continue                                   # blob not present yet -> keep baked file
            dest = AGENT_DIR / fname
            tmp = dest.with_name(dest.name + ".tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            tmp.replace(dest)
            n += 1
        try:
            _STATE_MARKER.parent.mkdir(parents=True, exist_ok=True)
            _STATE_MARKER.write_text(str(int(time.time())))
        except Exception:
            pass                                           # marker is an optimisation only
        return n
    except Exception:
        return None
