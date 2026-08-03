"""Sync this folder's pipeline code to the Fabric Prod Lakehouse (Files/code/api)
so the nightly Fabric notebook always runs the SAME code as the Azure
lulu-refresh job.

Runs in CI (deploy-pipeline.yml, after azure/login as oidc-msi-lulu which holds
Contributor on the workspace); also runnable locally under `az login`.

2026-08-03 lesson: this copy went stale for 3 days (weekly_timesheet parity
DIFF 64.7%) because syncing was manual. Only *.py + requirements.txt at this
folder's top level are uploaded — credential/ and .env are never touched."""
import os
import subprocess
import sys
from pathlib import Path

import requests

WORKSPACE = "00000000-0000-4000-a000-000000000012"   # LuLu Fabric Prod
LAKEHOUSE = "00000000-0000-4000-a000-000000000013"   # lulu Lakehouse artifact id
TARGET = "Files/code/api"
HERE = Path(__file__).resolve().parent


def _token():
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://storage.azure.com/",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=os.name == "nt")
    tok = out.stdout.strip()
    if not tok:
        sys.exit(f"no OneLake token: {out.stderr.strip()[:200]}")
    return tok


def main():
    files = sorted(HERE.glob("*.py")) + [HERE / "requirements.txt"]
    h = {"Authorization": f"Bearer {_token()}", "x-ms-version": "2023-11-03"}
    base = f"https://onelake.dfs.fabric.microsoft.com/{WORKSPACE}/{LAKEHOUSE}/{TARGET}"
    failed = 0
    for f in files:
        if not f.exists():
            continue
        data = f.read_bytes()
        url = f"{base}/{f.name}"
        r1 = requests.put(url, headers=h, params={"resource": "file"})          # create/truncate
        r2 = requests.patch(url, headers={**h, "Content-Type": "application/octet-stream"},
                            params={"action": "append", "position": "0"}, data=data)
        r3 = requests.patch(url, headers=h, params={"action": "flush", "position": str(len(data))})
        ok = r1.status_code == 201 and r2.status_code == 202 and r3.status_code == 200
        print(f"{'OK  ' if ok else 'FAIL'} {f.name} ({len(data)} bytes)"
              + ("" if ok else f" create={r1.status_code} append={r2.status_code} flush={r3.status_code}"))
        failed += 0 if ok else 1
    print(f"synced {len(files) - failed}/{len(files)} files -> {TARGET}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
