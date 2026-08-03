"""blob_gold.py — keep the cloud app's Gold lake fresh by pulling the nightly-built
parquet from the configured storage backend, instead of relying on what was baked
into the image.

Since 2026-07-28 this is a FACADE over gold_repository.py (GoldRepository):
the public API (pull_gold / pull_state / regenerate_local_state / last_pull_epoch)
is unchanged — lulu_ops_center and friends never notice — but the storage backend
is now swappable via env GOLD_BACKEND:

    GOLD_BACKEND=azure_blob   (default) blob lulu-data/gold/  — today's production
    GOLD_BACKEND=onelake      Fabric OneLake <lakehouse>.Lakehouse/Files/gold/

Rollback from a bad OneLake cutover = flip the env var back, restart. Nothing else.

Design (unchanged):
- No-op when the backend is unconfigured (local dev) -> the baked/local gold is used.
- TTL-guarded via a marker file so a long-running replica only re-pulls periodically.
- Fail-safe: any error leaves the existing on-disk gold untouched (returns None).
- Atomic-ish writes (download to .tmp, then replace) so a half-download can't be read.
"""
import os
import time
from pathlib import Path

from gold_repository import GOLD_DIR, AGENT_DIR, get_repository

_MARKER = GOLD_DIR / ".blob_sync"                          # epoch of last successful pull
_STATE_MARKER = GOLD_DIR / ".state_sync"                   # epoch of last state-file (link audit) pull
_VERSION_MARKER = GOLD_DIR / ".blob_version"               # backend version of last successful sync
_VERSION_CHECK = GOLD_DIR / ".blob_version_check"          # throttle marker for freshness polls


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


def _ttl_fresh(marker: Path, ttl_seconds):
    try:
        return marker.exists() and time.time() - marker.stat().st_mtime < ttl_seconds
    except Exception:
        return False


def pull_gold(force=False, ttl_seconds=1800):
    """Mirror the backend's gold/*.parquet into GOLD_DIR.

    Returns the number of files downloaded (int > 0) when a real sync happened,
    False when skipped by the TTL, and None when the backend is unconfigured or
    the pull failed (existing gold is left in place either way).
    """
    if not _in_cloud():
        return None                                       # local dev: use whatever's on disk
    if not force and _ttl_fresh(_MARKER, ttl_seconds):
        return False                                      # synced recently — skip the network call
    n = get_repository().sync_gold(GOLD_DIR)
    if n is None:
        return None                                       # unconfigured or network/auth error
    try:
        _MARKER.write_text(str(int(time.time())))
        ver = get_repository().get_version()              # so pull_gold_if_newer() doesn't
        if ver:                                           # immediately re-download at first poll
            _VERSION_MARKER.write_text(str(ver))
    except Exception:
        pass
    return n


def pull_gold_if_newer(ttl_seconds=300):
    """Hot-reload guard for long-running replicas — the MCP gateway calls this on
    request so a manual lulu_refresh (or the nightly run) reaches queries WITHOUT
    an app restart. At most once per TTL it asks the backend for its newest-gold
    version; a full re-sync happens only when that version moved. Cost profile:
    throttled = one stat(); polled = one blob listing; download only when gold
    actually changed. Returns files synced (int), False when throttled or
    unchanged, None off-cloud / backend error (existing gold untouched)."""
    if not _in_cloud():
        return None
    if _ttl_fresh(_VERSION_CHECK, ttl_seconds):
        return False
    try:
        _VERSION_CHECK.parent.mkdir(parents=True, exist_ok=True)
        _VERSION_CHECK.write_text(str(int(time.time())))
    except Exception:
        pass
    try:
        ver = get_repository().get_version()
    except Exception:
        return None
    if not ver:
        return None
    try:
        last = _VERSION_MARKER.read_text().strip()
    except Exception:
        last = ""
    if str(ver) == last:
        return False
    n = get_repository().sync_gold(GOLD_DIR)
    if n:
        try:
            _VERSION_MARKER.write_text(str(ver))
            _MARKER.write_text(str(int(time.time())))
        except Exception:
            pass
    return n


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
    """Mirror backend state/<file> (link_health.json, ...) into data/agent/. Cloud-only, fail-safe.
    These are non-gold UI freshness files (e.g. the folder-link audit) the nightly job uploads.
    Has its own TTL so it can run on every page load INDEPENDENTLY of pull_gold() — gold only
    changes nightly, but the link audit must not stay stale all day because gold didn't move."""
    if not _in_cloud():
        return None
    if not force and _ttl_fresh(_STATE_MARKER, ttl_seconds):
        return False                                       # pulled recently — skip the network call
    n = get_repository().sync_state(AGENT_DIR)
    if n is None:
        return None
    try:
        _STATE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _STATE_MARKER.write_text(str(int(time.time())))
    except Exception:
        pass                                               # marker is an optimisation only
    return n
