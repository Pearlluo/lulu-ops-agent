"""
gold_repository.py — storage-backend abstraction for the Gold lake (Fabric-ready).

LuLu consumes Gold as LOCAL parquet files (DuckDB + sql_validator build views over
data/gold/*.parquet), so the repository contract is a MIRROR contract — "make the
local gold dir fresh from backend X" — not a per-table DataFrame API. The read
path stays untouched; swapping backends never touches query_tool/sql_validator.

Backends (selected by env GOLD_BACKEND, default azure_blob):
  azure_blob : today's production — nightly pipeline uploads to blob lulu-data/gold/
  onelake    : Fabric OneLake via the ADLS Gen2 API — the nightly Fabric pipeline
               writes the same parquet mirror under <lakehouse>/Files/gold/.
               UNTESTED until the Fabric workspace exists; health_check() is the
               cutover gate. Rollback = set GOLD_BACKEND=azure_blob, restart.

Config (onelake):
  ONELAKE_WORKSPACE   Fabric workspace name (e.g. "LuLu Fabric Prod")
  ONELAKE_LAKEHOUSE   Lakehouse name WITHOUT suffix (e.g. "lulu")
  ONELAKE_TENANT_ID / ONELAKE_CLIENT_ID / ONELAKE_CLIENT_SECRET
                      service principal; falls back to SHAREPOINT_* creds (same
                      app registration can be granted Fabric workspace access)

Both backends are fail-safe: any error returns None and leaves on-disk gold alone.
"""
import os
import time
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

GOLD_DIR = Path(__file__).resolve().parents[1] / "gold"    # data/gold
AGENT_DIR = Path(__file__).resolve().parent                # data/agent
STATE_FILES = ("link_health.json",)


@runtime_checkable
class GoldRepository(Protocol):
    name: str

    def sync_gold(self, dest_dir: Path) -> Optional[int]:
        """Mirror every gold parquet into dest_dir (atomic per file).
        Returns file count, or None on any failure (existing files untouched)."""
        ...

    def sync_state(self, dest_dir: Path) -> Optional[int]:
        """Mirror non-gold state files (link_health.json, ...) into dest_dir."""
        ...

    def list_tables(self) -> list:
        """Gold table names available on the backend (no .parquet suffix)."""
        ...

    def get_version(self) -> Optional[str]:
        """Backend freshness marker — ISO timestamp of the newest gold file."""
        ...

    def health_check(self) -> bool:
        """True when the backend is reachable AND has at least one gold table."""
        ...


def _atomic_write(dest: Path, data: bytes):
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    tmp.replace(dest)


# ---------------------------------------------------------------- azure blob

class AzureBlobGoldRepository:
    """Today's production backend: blob container lulu-data, prefixes gold/ + state/."""

    name = "azure_blob"
    _CONTAINER = "lulu-data"
    _CONN_KEYS = ("BLOB_CONNECTION_STRING", "AZURE_STORAGE_CONNECTION_STRING",
                  "AZURE_BLOB_CONNECTION_STRING", "STORAGE_CONNECTION_STRING")

    def _conn_str(self):
        for k in self._CONN_KEYS:
            v = os.getenv(k)
            if v:
                return v
        return None

    def _container(self):
        cs = self._conn_str()
        if not cs:
            return None
        from azure.storage.blob import BlobServiceClient
        return BlobServiceClient.from_connection_string(cs).get_container_client(self._CONTAINER)

    def sync_gold(self, dest_dir: Path):
        try:
            cc = self._container()
            if cc is None:
                return None
            dest_dir.mkdir(parents=True, exist_ok=True)
            n = 0
            for b in cc.list_blobs(name_starts_with="gold/"):
                fname = b.name.split("/", 1)[-1]
                if not fname.endswith(".parquet"):
                    continue
                _atomic_write(dest_dir / fname, cc.download_blob(b.name).readall())
                n += 1
            return n
        except Exception:
            return None

    def sync_state(self, dest_dir: Path):
        try:
            cc = self._container()
            if cc is None:
                return None
            n = 0
            for fname in STATE_FILES:
                try:
                    data = cc.download_blob(f"state/{fname}").readall()
                except Exception:
                    continue                                # not uploaded yet -> keep local copy
                _atomic_write(dest_dir / fname, data)
                n += 1
            return n
        except Exception:
            return None

    def list_tables(self):
        try:
            cc = self._container()
            if cc is None:
                return []
            return sorted(b.name.split("/", 1)[-1][:-8]     # strip "gold/" and ".parquet"
                          for b in cc.list_blobs(name_starts_with="gold/")
                          if b.name.endswith(".parquet"))
        except Exception:
            return []

    def get_version(self):
        try:
            cc = self._container()
            if cc is None:
                return None
            stamps = [b.last_modified for b in cc.list_blobs(name_starts_with="gold/")
                      if b.name.endswith(".parquet")]
            return max(stamps).isoformat() if stamps else None
        except Exception:
            return None

    def health_check(self):
        return bool(self.list_tables())


# ---------------------------------------------------------------- onelake

class OneLakeGoldRepository:
    """Fabric OneLake backend over the ADLS Gen2 API.

    Reads the parquet MIRROR the Fabric pipeline writes to
    <workspace>/<lakehouse>.Lakehouse/Files/gold/ — deliberately the same file
    layout as blob, so this class is a URL+auth change, not a format change.
    (Delta tables under Tables/ serve SQL endpoint / Direct Lake, not this app.)
    """

    name = "onelake"
    _ACCOUNT_URL = "https://onelake.dfs.fabric.microsoft.com"

    def _config(self):
        ws = os.getenv("ONELAKE_WORKSPACE")
        lh = os.getenv("ONELAKE_LAKEHOUSE")
        tenant = os.getenv("ONELAKE_TENANT_ID", os.getenv("SHAREPOINT_TENANT_ID"))
        cid = os.getenv("ONELAKE_CLIENT_ID", os.getenv("SHAREPOINT_CLIENT_ID"))
        sec = os.getenv("ONELAKE_CLIENT_SECRET", os.getenv("SHAREPOINT_CLIENT_SECRET"))
        if not all((ws, lh, tenant, cid, sec)):
            return None
        return ws, lh, tenant, cid, sec

    def _fs(self):
        cfg = self._config()
        if cfg is None:
            return None, None
        ws, lh, tenant, cid, sec = cfg
        from azure.identity import ClientSecretCredential
        from azure.storage.filedatalake import DataLakeServiceClient
        cred = ClientSecretCredential(tenant_id=tenant, client_id=cid, client_secret=sec)
        svc = DataLakeServiceClient(account_url=self._ACCOUNT_URL, credential=cred)
        return svc.get_file_system_client(ws), f"{lh}.Lakehouse/Files/gold"

    def _list_gold_paths(self):
        fs, prefix = self._fs()
        if fs is None:
            return None, None, None
        paths = [p for p in fs.get_paths(path=prefix, recursive=False)
                 if not p.is_directory and p.name.endswith(".parquet")]
        return fs, prefix, paths

    def sync_gold(self, dest_dir: Path):
        try:
            fs, _prefix, paths = self._list_gold_paths()
            if fs is None:
                return None
            dest_dir.mkdir(parents=True, exist_ok=True)
            n = 0
            for p in paths:
                fname = p.name.rsplit("/", 1)[-1]
                data = fs.get_file_client(p.name).download_file().readall()
                _atomic_write(dest_dir / fname, data)
                n += 1
            return n
        except Exception:
            return None

    def sync_state(self, dest_dir: Path):
        try:
            fs, prefix, _ = self._list_gold_paths()
            if fs is None:
                return None
            state_prefix = prefix.rsplit("/", 1)[0] + "/state"
            n = 0
            for fname in STATE_FILES:
                try:
                    data = fs.get_file_client(f"{state_prefix}/{fname}").download_file().readall()
                except Exception:
                    continue
                _atomic_write(dest_dir / fname, data)
                n += 1
            return n
        except Exception:
            return None

    def list_tables(self):
        try:
            _fs, _prefix, paths = self._list_gold_paths()
            if paths is None:
                return []
            return sorted(p.name.rsplit("/", 1)[-1][:-8] for p in paths)
        except Exception:
            return []

    def get_version(self):
        try:
            _fs, _prefix, paths = self._list_gold_paths()
            if not paths:
                return None
            return max(p.last_modified for p in paths).isoformat()
        except Exception:
            return None

    def health_check(self):
        return bool(self.list_tables())


# ---------------------------------------------------------------- selection

_BACKENDS = {
    "azure_blob": AzureBlobGoldRepository,
    "onelake": OneLakeGoldRepository,
}

_repo_cache = {}


def get_repository(backend=None) -> GoldRepository:
    """Backend from arg or GOLD_BACKEND env (default azure_blob). Unknown -> ValueError."""
    key = (backend or os.getenv("GOLD_BACKEND", "azure_blob")).strip().lower()
    if key not in _BACKENDS:
        raise ValueError(f"Unknown GOLD_BACKEND '{key}' (expected one of {sorted(_BACKENDS)})")
    if key not in _repo_cache:
        _repo_cache[key] = _BACKENDS[key]()
    return _repo_cache[key]


def read_table(table_name: str, dest_dir: Path = GOLD_DIR):
    """Convenience: local mirrored table as a DataFrame (mirror first via sync_gold)."""
    import pandas as pd
    p = dest_dir / f"{table_name}.parquet"
    if not p.exists():
        raise FileNotFoundError(f"gold table '{table_name}' not mirrored at {p}")
    return pd.read_parquet(p)
