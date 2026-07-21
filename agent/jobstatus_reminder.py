"""
JobStatus reminder — nudge the creator when a job/project is logged without a status.

Scans JMS-Jobs (JobStatus) and JMS-Projects (Status) LIVE; for any item with a blank
status, emails the person who created it asking them to fill it in. De-dupes via a log
(won't re-nudge the same item within COOLDOWN_DAYS).

SAFE BY DEFAULT: dry-run (lists who *would* be emailed, sends nothing). Add --send to
actually email creators. Reuses the same Graph app creds + sendMail as daily_brief.

Run:  python jobstatus_reminder.py            # dry-run (no email)
      python jobstatus_reminder.py --send     # email the creators
"""
import os
import sys
import json
import argparse
import datetime as dt
from pathlib import Path

import requests
from dotenv import load_dotenv

AGENT_DIR = Path(__file__).resolve().parent
ENV = AGENT_DIR.parent / "Raw Data" / "API" / "credential" / ".env"
LOG = AGENT_DIR / "logs" / "jobstatus_reminders.jsonl"
SITE = "BMS"
TABLES = [("JMS-Jobs", "JobStatus"), ("JMS-Projects", "Status")]
COOLDOWN_DAYS = 3      # don't re-nudge the same item within this window
RECENT_DAYS = 3        # only nudge NEWLY-created items (created within this window), not the backlog
GRAPH = "https://graph.microsoft.com/v1.0"


def token():
    load_dotenv(ENV)
    t, c, s = (os.getenv("SHAREPOINT_TENANT_ID"), os.getenv("SHAREPOINT_CLIENT_ID"),
               os.getenv("SHAREPOINT_CLIENT_SECRET"))
    r = requests.post(f"https://login.microsoftonline.com/{t}/oauth2/v2.0/token",
                      data={"client_id": c, "client_secret": s, "grant_type": "client_credentials",
                            "scope": "https://graph.microsoft.com/.default"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def recently_nudged():
    seen = {}
    if LOG.exists():
        cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=COOLDOWN_DAYS)
        for line in LOG.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                ts = dt.datetime.fromisoformat(d["ts"])
                if ts > cut:
                    seen[d["key"]] = ts
            except Exception:
                pass
    return seen


def find_missing(s, headers):
    bms = s.get(f"{GRAPH}/sites/yourtenant.sharepoint.com:/sites/{SITE}",
                headers=headers, timeout=60).json()["id"]
    lists = s.get(f"{GRAPH}/sites/{bms}/lists?$select=id,name,displayName&$top=300",
                  headers=headers, timeout=60).json()["value"]
    out = []
    for tbl, field in TABLES:
        lid = next((l["id"] for l in lists if l.get("displayName") == tbl or l.get("name") == tbl), None)
        if not lid:
            continue
        cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RECENT_DAYS)
        url = f"{GRAPH}/sites/{bms}/lists/{lid}/items?$expand=fields($select=Title,{field})&$top=200"
        while url:
            j = s.get(url, headers=headers, timeout=60).json()
            for x in j.get("value", []):
                f = x.get("fields", {})
                if str(f.get(field, "")).strip():
                    continue                                  # status already filled
                created = x.get("createdDateTime")
                try:
                    if not created or dt.datetime.fromisoformat(created.replace("Z", "+00:00")) < cut:
                        continue                              # only NEWLY-created items
                except Exception:
                    continue
                user = (x.get("createdBy", {}) or {}).get("user", {}) or {}
                out.append({"table": tbl, "item_id": x["id"], "title": f.get("Title", ""),
                            "creator": user.get("displayName", "?"), "email": user.get("email")})
            url = j.get("@odata.nextLink")
    return out


def send(s, headers, sender, to, subject, body):
    r = s.post(f"{GRAPH}/users/{sender}/sendMail", headers=headers, timeout=30,
               data=json.dumps({"message": {"subject": subject,
                   "body": {"contentType": "HTML", "content": body},
                   "toRecipients": [{"emailAddress": {"address": to}}]}, "saveToSentItems": False}))
    return r.status_code < 300, f"{r.status_code} {r.text[:150] if r.status_code>=300 else ''}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually email creators (default: dry-run)")
    args = ap.parse_args()

    s = requests.Session()
    at = token()
    headers = {"Authorization": "Bearer " + at, "Content-Type": "application/json"}
    sender = os.getenv("GRAPH_SENDER", "test@company.com.au")
    seen = recently_nudged()
    missing = find_missing(s, headers)
    print(f"missing status: {len(missing)} item(s)")

    sent = 0
    for m in missing:
        key = f"{m['table']}:{m['item_id']}"
        if key in seen:
            print(f"  [skip cooldown] {m['table']} {m['title'][:40]}")
            continue
        if not m["email"]:
            print(f"  [no creator email] {m['table']} {m['title'][:40]} (creator {m['creator']})")
            continue
        subject = f"Please add a status to {m['table'].split('-')[1][:-1]}: {m['title']}"
        body = (f"<div style='font-family:Segoe UI,sans-serif;font-size:14px'>Hi {m['creator'].split()[0]},<br><br>"
                f"The {m['table']} entry <b>{m['title']}</b> was logged without a status. "
                f"Please open it and set the status so it flows through correctly.<br><br>— Lulu</div>")
        if args.send:
            ok, info = send(s, headers, sender, m["email"], subject, body)
            print(f"  [{'sent' if ok else 'FAIL'}] {m['email']} <- {m['title'][:40]} {info}")
            if ok:
                with open(LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                                        "key": key, "email": m["email"]}) + "\n")
                sent += 1
        else:
            print(f"  [dry-run would email] {m['email']} <- {m['title'][:40]}")
    print(f"[OK] {'sent '+str(sent) if args.send else 'dry-run'} (cooldown {COOLDOWN_DAYS}d)")
    sys.exit(0)


if __name__ == "__main__":
    main()
