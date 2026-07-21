"""
Folder-link health + auto-repair — per-item, automated, pipeline-friendly.

DETECT (always): resolve every folder-link column (OpsFolder/ComFolder/PlanningFolder)
against Microsoft Graph /shares and classify per item per column:
    ok / broken (stale link — folder renamed/moved) / missing (empty cell)
-> writes data/agent/link_health.json (read by the System Galaxy JMS-Jobs card,
   ops_metrics today's-problems, data_quality_sentinel). NOTE: kept OUTSIDE logs/ on purpose —
   logs/ is an Azure Files mount on the deployed app, which would shadow a baked copy; the
   snapshot must bake into the image (data/agent/) so it ships with every deploy.

REPAIR (--repair): for each BROKEN link, look the folder up by the item-ID embedded in
the stored value (survives renames), get its CURRENT url, and rewrite ONLY the url part
of the cell — the folder ID, metadata and the trailing permission emails
(<^^^>…<^^^^> = the *FolderContribute people) are preserved byte-for-byte. Never creates
or moves a folder; never touches permissions. Every original value is backed up to
link_repair_backup.jsonl first; every fix is logged to link_repairs.jsonl (drives the
card's "Fixed" ✓). Idempotent: a fixed link resolves 200 next run and is skipped. Links
whose ID no longer resolves are left untouched and flagged (truly gone / cross-site move).

Run:  python check_links.py             # detect only
      python check_links.py --repair    # detect + auto-fix stale links
      python check_links.py --repair --dry-run   # show what would be fixed, write nothing
"""

import os
import sys
import json
import time
import base64
import argparse
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd
from dotenv import load_dotenv

API_DIR = Path(__file__).resolve().parent
DATA_DIR = API_DIR.parents[1]
SILVER_FLAT = DATA_DIR / "silver" / "flat"
LOGS = DATA_DIR / "agent" / "logs"
# link_health snapshot lives in data/agent/ (NOT logs/) so it bakes into the deployed image —
# logs/ is an Azure Files mount that shadows baked files. Repair audit logs stay in logs/.
OUT = DATA_DIR / "agent" / "link_health.json"
REPAIRS = LOGS / "link_repairs.jsonl"
BACKUP = LOGS / "link_repair_backup.jsonl"

TABLES = [
    {"key": "JMS-Jobs", "parquet": "sp__JMS-Jobs.parquet", "id_col": "JobID", "title_col": "Title",
     "item_id_col": "id", "site": "BMS", "list": "JMS-Jobs",
     "columns": ["OpsFolder", "ComFolder", "PlanningFolder"]},
    {"key": "JMS-Projects", "parquet": "sp__JMS-Projects.parquet", "id_col": "ProjectID", "title_col": "Title",
     "item_id_col": "id", "site": "BMS", "list": "JMS-Projects",
     "columns": ["OpsFolder", "ComFolder", "PlanningFolder"]},
]

GRAPH = "https://graph.microsoft.com/v1.0"
ROOT = "https://yourtenant.sharepoint.com"
MAX_WORKERS = 10
RETRY = {429, 500, 502, 503, 504}


def get_token():
    load_dotenv(API_DIR / "credential" / ".env")
    tid, cid, sec = (os.getenv("SHAREPOINT_TENANT_ID"), os.getenv("SHAREPOINT_CLIENT_ID"),
                     os.getenv("SHAREPOINT_CLIENT_SECRET"))
    r = requests.post(f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/token",
                      data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec,
                            "scope": "https://graph.microsoft.com/.default"}, timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]


def url_of(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan" or "<#>" not in s:
        return None
    u = s.split("<#>", 1)[1].split("<->", 1)[0].strip()
    return u if u.lower().startswith("http") else None


def check_one(session, url):
    enc = "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    for _ in range(5):
        try:
            r = session.get(f"{GRAPH}/shares/{enc}/driveItem", timeout=60)
            if r.status_code in RETRY:
                time.sleep(float(r.headers.get("Retry-After", 1)))
                continue
            return r.status_code
        except Exception:
            time.sleep(1)
    return -1


def load_repairs():
    fixed = {}
    if REPAIRS.exists():
        for line in REPAIRS.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                fixed[(str(d["jobid"]), d["column"])] = d.get("ts", "")
            except Exception:
                pass
    return fixed


def audit_table(session, df, cfg, fixed):
    cols = [c for c in cfg["columns"] if c in df.columns]
    idc, titlec = cfg["id_col"], cfg["title_col"]
    urls = {url_of(v) for c in cols for v in df[c] if url_of(v)}
    urls = list(urls)
    print(f"  {cfg['key']}: testing {len(urls)} unique links across {len(df)} rows", flush=True)

    status = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(check_one, session, u): u for u in urls}
        done = 0
        for f in as_completed(futs):
            status[futs[f]] = f.result()
            done += 1
            if done % 300 == 0:
                print(f"    {done}/{len(urls)}", flush=True)

    def classify(v):
        u = url_of(v)
        if u is None:
            return "missing"
        return "ok" if status.get(u) == 200 else "broken"

    items, agg = [], {"ok": 0, "broken": 0, "missing": 0}
    bycol = {c: {"broken": 0, "missing": 0} for c in cols}
    for i in df.index:
        jid = str(df.at[i, idc]) if idc in df else str(i)
        cells, problem = {}, False
        for c in cols:
            st = classify(df.at[i, c])
            cells[c] = st
            agg[st] += 1
            if st != "ok":
                problem = True
                bycol[c][st] += 1
        if problem:
            items.append({"jobid": jid,
                          "title": str(df.at[i, titlec])[:60] if titlec in df else "",
                          "cells": cells,
                          "fixed": [c for c in cols if (jid, c) in fixed]})
    return {"checked": sum(agg.values()), "ok": agg["ok"], "broken": agg["broken"],
            "missing": agg["missing"], "problem_items": len(items), "byCol": bycol, "items": items}


# ---------------- repair ----------------
def _site_id(session, short):
    return session.get(f"{GRAPH}/sites/{ROOT.split('//')[1]}:/sites/{short}", timeout=60).json()["id"]


def _list_id(session, site_id, name):
    ls = session.get(f"{GRAPH}/sites/{site_id}/lists?$select=id,name,displayName&$top=300",
                     timeout=60).json()["value"]
    for l in ls:
        if l.get("displayName") == name or l.get("name") == name:
            return l["id"]
    return None


def current_url(session, cache, stored):
    """resolve the folder's CURRENT url via the item-ID embedded in the stored value."""
    head = stored.split("<#>", 1)[0]            # e.g. "IMS/Operations:5788"
    if ":" not in head:
        return None
    prefix, fid = head.rsplit(":", 1)
    if not fid.strip().isdigit():
        return None
    if prefix not in cache:
        parts = prefix.split("/")
        sid = _site_id(session, parts[0])
        cache[prefix] = (sid, _list_id(session, sid, "/".join(parts[1:])))
    sid, lid = cache[prefix]
    if not lid:
        return None
    r = session.get(f"{GRAPH}/sites/{sid}/lists/{lid}/items/{fid.strip()}/driveItem?$select=webUrl",
                    timeout=60)
    if r.status_code != 200:
        return None
    return urllib.parse.unquote(r.json()["webUrl"])


def repair_table(session, df, cfg, audit, dry_run=False):
    cols = [c for c in cfg["columns"] if c in df.columns]
    idc, item_col = cfg["id_col"], cfg["item_id_col"]
    site_id = _site_id(session, cfg["site"])
    list_id = _list_id(session, site_id, cfg["list"])
    by_jid = {str(df.at[i, idc]): i for i in df.index}
    cache = {}
    fixed_now, skipped = 0, 0

    for it in audit["items"]:
        i = by_jid.get(it["jobid"])
        if i is None:
            continue
        item_id = str(df.at[i, item_col])
        for c in cols:
            if it["cells"].get(c) != "broken":
                continue
            stored = str(df.at[i, c])
            cur = current_url(session, cache, stored)
            if not cur:
                skipped += 1                       # ID no longer resolves -> leave for a human
                continue
            old_url = stored.split("<#>", 1)[1].split("<->", 1)[0]
            if old_url == cur:
                continue
            new_val = stored.replace(old_url, cur)
            if dry_run:
                print(f"  [dry] {it['jobid']} {c}: -> {cur[60:][:60]}", flush=True)
                fixed_now += 1
                continue
            # back up original, then write
            with open(BACKUP, "a", encoding="utf-8") as bf:
                bf.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                     "jobid": it["jobid"], "item_id": item_id, "column": c,
                                     "old_value": stored}, ensure_ascii=False) + "\n")
            pr = session.patch(f"{GRAPH}/sites/{site_id}/lists/{list_id}/items/{item_id}/fields",
                               data=json.dumps({c: new_val}),
                               headers={"Content-Type": "application/json"}, timeout=60)
            if pr.status_code >= 400:
                print(f"  [FAIL] {it['jobid']} {c}: PATCH {pr.status_code} {pr.text[:120]}", flush=True)
                continue
            with open(REPAIRS, "a", encoding="utf-8") as rf:
                rf.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                     "jobid": it["jobid"], "column": c}, ensure_ascii=False) + "\n")
            fixed_now += 1
            # re-verify the rewritten link, then flip this cell green so the card reflects the fix
            if check_one(session, cur) == 200 and it["cells"].get(c) != "ok":
                it["cells"][c] = "ok"
                audit["ok"] += 1
                audit["broken"] = max(0, audit["broken"] - 1)
    if not dry_run:
        audit["problem_items"] = sum(1 for it in audit["items"]
                                     if any(v != "ok" for v in it["cells"].values()))
    print(f"[repair] {cfg['key']}: {'would fix' if dry_run else 'fixed'} {fixed_now}, "
          f"skipped {skipped} (ID unresolved)", flush=True)
    return fixed_now


def fetch_live(session, cfg):
    """Pull the list's folder columns LIVE from SharePoint (not the possibly-stale parquet)."""
    sid = _site_id(session, cfg["site"])
    lid = _list_id(session, sid, cfg["list"])
    if not lid:
        return None
    sel = ",".join([cfg["id_col"], cfg["title_col"]] + cfg["columns"])
    url = f"{GRAPH}/sites/{sid}/lists/{lid}/items?$expand=fields($select={sel})&$top=200"
    rows = []
    while url:
        j = session.get(url, timeout=60).json()
        for x in j.get("value", []):
            f = x.get("fields", {})
            f[cfg["item_id_col"]] = x["id"]
            rows.append(f)
        url = j.get("@odata.nextLink")
    df = pd.DataFrame(rows)
    for c in [cfg["id_col"], cfg["title_col"], cfg["item_id_col"]] + cfg["columns"]:
        if c not in df.columns:
            df[c] = None
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repair", action="store_true", help="auto-fix stale links (writes to SharePoint)")
    ap.add_argument("--dry-run", action="store_true", help="with --repair: show fixes, write nothing")
    args = ap.parse_args()

    out = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    # must exist BEFORE repair_table appends to BACKUP/REPAIRS — on a fresh cloud
    # container logs/ isn't there yet and the open() would abort the whole repair pass
    LOGS.mkdir(parents=True, exist_ok=True)
    fixed = load_repairs()
    try:
        s = requests.Session()
        s.headers.update({"Authorization": "Bearer " + get_token()})
        for cfg in TABLES:
            df = fetch_live(s, cfg)            # LIVE list values — NOT the (possibly stale) parquet
            if df is None or df.empty:
                print(f"[WARN] could not fetch live {cfg['key']}", flush=True)
                continue
            res = audit_table(s, df, cfg, fixed)
            out[cfg["key"]] = res
            print(f"[OK] {cfg['key']}: ok={res['ok']} broken={res['broken']} missing={res['missing']} "
                  f"| {res['problem_items']} problem items", flush=True)
            if args.repair:
                repair_table(s, df, cfg, res, dry_run=args.dry_run)
                if not args.dry_run:
                    # re-stamp 'fixed' from the updated log so the card lights up immediately
                    fixed2 = load_repairs()
                    for it in res["items"]:
                        it["fixed"] = [c for c in cfg["columns"]
                                       if (it["jobid"], c) in fixed2 and c in it["cells"]]
    except Exception as ex:
        print(f"[WARN] link check failed: {ex}", flush=True)
        out["error"] = str(ex)

    LOGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] wrote {OUT}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
