"""
alert.py — pipeline failure email via Microsoft Graph (client credentials).

The nightly lulu-refresh job failed silently for 20 days once (2026-06-29 →
07-19); this closes that hole. Uses the same app registration the pipeline
already authenticates SharePoint with (Mail.Send is granted — daily_brief
sends through it), so no new secrets are needed in the Container Apps Job.

Env: SHAREPOINT_TENANT_ID / SHAREPOINT_CLIENT_ID / SHAREPOINT_CLIENT_SECRET
     GRAPH_SENDER   (default test@yourtenant.example)
     LULU_ALERT_TO  (comma-separated, default admin@yourtenant.example)

Fail-safe: never raises — an alert failure must not mask the original error.
"""
import os
import traceback

import requests


def send_alert(subject, body_text):
    try:
        tenant = os.getenv("SHAREPOINT_TENANT_ID")
        cid = os.getenv("SHAREPOINT_CLIENT_ID")
        sec = os.getenv("SHAREPOINT_CLIENT_SECRET")
        if not (tenant and cid and sec):
            print("[ALERT] Graph credentials missing — cannot email", flush=True)
            return False
        sender = os.getenv("GRAPH_SENDER", "test@yourtenant.example")
        recipients = [a.strip() for a in
                      os.getenv("LULU_ALERT_TO", "admin@yourtenant.example").split(",") if a.strip()]

        tok = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={"client_id": cid, "client_secret": sec,
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"}, timeout=30)
        tok.raise_for_status()

        html = ("<div style='font-family:Segoe UI,sans-serif;font-size:14px'>"
                + "".join(f"<div>{line or '&nbsp;'}</div>" for line in body_text.splitlines()[:80])
                + "</div>")
        r = requests.post(
            f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
            headers={"Authorization": f"Bearer {tok.json()['access_token']}",
                     "Content-Type": "application/json"},
            json={"message": {"subject": subject,
                              "body": {"contentType": "HTML", "content": html},
                              "toRecipients": [{"emailAddress": {"address": a}} for a in recipients]},
                  "saveToSentItems": False}, timeout=30)
        ok = r.status_code in (200, 202)
        print(f"[ALERT] {'sent' if ok else f'FAILED {r.status_code}: {r.text[:200]}'}", flush=True)
        return ok
    except Exception:
        print("[ALERT] send_alert crashed:\n" + traceback.format_exc(), flush=True)
        return False
