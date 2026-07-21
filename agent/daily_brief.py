"""
daily_brief.py — Lulu's proactive morning check. Nobody asks; she checks anyway.

    07:00 daily (Windows Task Scheduler 'LuluDailyBrief'):
      [--refresh-from-blob]  pull the nightly-refreshed Gold from Azure
      evaluate event_registry.yaml rules over the lake (same safety chain as the agent)
      write   logs/briefs/brief_YYYY-MM-DD.md   (the dashboard shows the latest one)
      email   the brief via Microsoft Graph (same app creds + sendMail pattern that
              the payroll-query-system uses)

    python daily_brief.py                 # evaluate + save + email
    python daily_brief.py --no-email      # evaluate + save only
    python daily_brief.py --refresh-from-blob
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_DIR))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BRIEF_DIR = AGENT_DIR / "logs" / "briefs"
# snapshots.jsonl lives in data/agent/ (NOT logs/) so it bakes into the deployed image — logs/ is an
# Azure Files mount that shadows baked files; the freshness panel needs this snapshot in the image.
SNAP_PATH = AGENT_DIR / "snapshots.jsonl"
REGISTRY = AGENT_DIR / "event_registry.yaml"
SEV_ICON = {"red": "🔴", "amber": "🟠", "info": "🔵"}


# ---------------------------------------------------------------- metrics
def collect_metrics():
    """Everything the rules can reference. All queries go through the safety chain."""
    import ops_metrics as M
    from data_quality_sentinel import run_checks

    k = M.get_kpis()
    m = dict(k)
    m["expiring_7d"] = M.get_expiry_ladder()[7]

    sup = M.get_supplier_risk(top_n=1)
    m["top_supplier_expired"] = sup[0]["expired_certs"] if sup else 0
    m["top_supplier_name"] = sup[0]["supplier_name"] if sup else "-"

    # week-on-week must compare two COMPLETE weeks — drop the current partial week
    weeks = M.get_weekly_hours(weeks=4)
    this_monday = dt.date.today() - dt.timedelta(days=dt.date.today().weekday())
    full_weeks = [w for w in weeks if str(w["week_start"])[:10] < this_monday.isoformat()]
    m["hours_last"] = float(full_weeks[-1]["actual_hours"]) if full_weeks else 0.0
    m["hours_prev"] = float(full_weeks[-2]["actual_hours"]) if len(full_weeks) > 1 else 0.0
    m["hours_wow_change_pct"] = (100.0 * (m["hours_last"] - m["hours_prev"]) / m["hours_prev"]
                                 if m["hours_prev"] else 0.0)

    sentinel = run_checks()
    m["sentinel_status"] = sentinel["status"]
    m["_sentinel"] = sentinel

    horizon = next((c for c in sentinel["checks"] if c["check"] == "roster_summary: future horizon"), None)
    if horizon and "max roster_date = " in horizon["detail"]:
        mx = horizon["detail"].split("max roster_date = ")[1].split(",")[0]
        try:
            m["roster_horizon_days"] = (dt.date.fromisoformat(str(mx)) - dt.date.today()).days
        except ValueError:
            m["roster_horizon_days"] = 999
    else:
        m["roster_horizon_days"] = 999

    gold = AGENT_DIR.parent / "gold" / "weekly_timesheet.parquet"
    m["gold_age_hours"] = ((dt.datetime.now().timestamp() - gold.stat().st_mtime) / 3600
                           if gold.exists() else 9999)

    m["_urgent"] = M.get_urgent_expiries(days=7, top_n=8)

    # Commercial Director's inputs (Finance role — the brief goes to leadership)
    try:
        from tools._base import get_query_tool
        qt = get_query_tool()
        r = qt.run("SELECT COUNT(*) AS n, SUM(amount_due) AS due FROM invoice_register "
                   "WHERE invoice_type = 'ACCREC' AND status = 'AUTHORISED' AND amount_due > 0 LIMIT 1",
                   "Finance")
        m["ar_count"], m["ar_total"] = (int(r.rows[0][0] or 0), float(r.rows[0][1] or 0)) if r.ok and r.rows else (0, 0.0)
        r = qt.run("SELECT MAX(invoice_date) AS d FROM invoice_register WHERE invoice_type = 'ACCREC' LIMIT 1", "Finance")
        last_inv = str(r.rows[0][0]) if r.ok and r.rows and r.rows[0][0] else None
        m["xero_age_days"] = (dt.date.today() - dt.date.fromisoformat(last_inv)).days if last_inv else 9999
        r = qt.run("SELECT month, SUM(invoiced) AS inv FROM revenue_summary "
                   "GROUP BY month ORDER BY month DESC LIMIT 2", "Finance")
        if r.ok and r.rows:
            m["rev_last_month_label"], m["rev_last_month"] = r.rows[0][0], float(r.rows[0][1] or 0)
        else:
            m["rev_last_month_label"], m["rev_last_month"] = "-", 0.0
    except Exception:
        m["ar_count"], m["ar_total"], m["xero_age_days"] = 0, 0.0, 9999
        m["rev_last_month_label"], m["rev_last_month"] = "-", 0.0

    # Automation Director's inputs: latest GitHub workflow runs (cached registry, no network)
    try:
        reg = yaml.safe_load((AGENT_DIR / "automation_registry.yaml").read_text(encoding="utf-8"))
        runs = []
        for key, e in (reg.get("automations") or {}).items():
            lr = (e.get("live") or {}).get("latest_run") or {}
            runs.append({"name": e.get("display_name", key),
                         "conclusion": lr.get("conclusion"), "when": lr.get("updated_at", "")[:10]})
        m["_automation_runs"] = runs
        m["automation_failures"] = sum(1 for r in runs if r["conclusion"] not in ("success", None))
    except Exception:
        m["_automation_runs"], m["automation_failures"] = [], 0
    return m


# ---------------------------------------------------------------- rules
def evaluate(metrics, registry):
    alerts = []
    for name, rule in (registry.get("rules") or {}).items():
        value = metrics.get(rule["metric"])
        if value is None:
            continue
        op, th = rule.get("op", ">="), rule["threshold"]
        hit = {"<": value < th, "<=": value <= th, ">": value > th, ">=": value >= th,
               "==": value == th, "!=": value != th,
               "abs>=": abs(value) >= th if isinstance(value, (int, float)) else False}[op]
        if hit:
            msg = rule["message"].format(value=value, **{k: v for k, v in metrics.items()
                                                         if not k.startswith("_")})
            alerts.append({"rule": name, "severity": rule.get("severity", "info"), "message": msg})
    order = {"red": 0, "amber": 1, "info": 2}
    alerts.sort(key=lambda a: order.get(a["severity"], 3))
    return alerts


# ---------------------------------------------------------------- trends (Executive Intelligence)
TREND_KEYS = ["workforce_risk", "expiring_7d", "deployable", "idle_pool",
              "ar_total", "hours_last", "active_projects"]


def load_snapshots():
    p = SNAP_PATH
    if not p.exists():
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8")]


def trend_deltas(metrics, today=None):
    """Compare today's numbers against the previous snapshot + a trailing baseline.
    Returns {key: {'prev': x, 'delta': d, 'anomaly': bool}} — anomaly = > 2 std from
    the trailing 14-day mean (needs >= 7 days of history to arm)."""
    today = (today or dt.date.today()).isoformat()
    snaps = [s for s in load_snapshots() if s.get("date") < today]
    out = {}
    if not snaps:
        return out
    prev = snaps[-1]
    for k in TREND_KEYS:
        cur, pv = metrics.get(k), prev.get(k)
        if not isinstance(cur, (int, float)) or not isinstance(pv, (int, float)):
            continue
        d = {"prev": pv, "delta": cur - pv, "anomaly": False}
        hist = [s[k] for s in snaps[-14:] if isinstance(s.get(k), (int, float))]
        if len(hist) >= 7:
            mean = sum(hist) / len(hist)
            var = sum((x - mean) ** 2 for x in hist) / len(hist)
            std = var ** 0.5
            if std > 0 and abs(cur - mean) > 2 * std:
                d["anomaly"] = True
        out[k] = d
    return out


def _tr(deltas, key, fmt="{:+,.0f}"):
    """Render a delta tag like ' (▲ +12 vs yesterday)' for the brief."""
    d = deltas.get(key)
    if not d or not d["delta"]:
        return ""
    arrow = "▲" if d["delta"] > 0 else "▼"
    tag = f" ({arrow} {fmt.format(d['delta'])} vs yesterday"
    if d["anomaly"]:
        tag += " · UNUSUAL"
    return tag + ")"


# ---------------------------------------------------------------- brief
def build_brief(metrics, alerts, today=None):
    """The morning boardroom: each Director reports; Lulu (CEO) summarises on top."""
    today = today or dt.date.today()
    m = metrics
    deltas = trend_deltas(metrics, today)
    L = [f"# Lulu Daily Brief — {today:%a %d %b %Y}", "", "Good morning Admin,", ""]

    # ---- Lulu's executive summary ----
    if alerts:
        L.append(f"## Executive Summary ({len(alerts)} item{'s' if len(alerts) > 1 else ''})")
        for i, a in enumerate(alerts, 1):
            L.append(f"- {SEV_ICON.get(a['severity'], '•')} {i}. {a['message']}")
    else:
        L.append("## Executive Summary — all departments report normal ✅")

    # ---- department reports ----
    L += ["", "## Operations Director",
          f"- Deployable now: **{m['deployable']}** field workers{_tr(deltas, 'deployable')}"
          f" · idle pool (90d): {m['idle_pool']}",
          f"- Actual hours last full week: **{m['hours_last']:,.0f}h** ({m['hours_wow_change_pct']:+.0f}% WoW)",
          f"- Roster data extends **{m['roster_horizon_days']}** days ahead",
          f"- Active projects: {m['active_projects']}"]

    L += ["", "## HR / Compliance Director",
          f"- Deployment risk: **{m['workforce_risk']}** rostered-worker × expired-cert combos (30d)"
          f"{_tr(deltas, 'workforce_risk')}",
          f"- Certs expiring ≤7d / ≤30d: **{m['expiring_7d']} / {m['expiring_30d']}**"
          f" · already expired: {m['expired_total']:,}",
          f"- Highest-risk supplier: {m['top_supplier_name']} ({m['top_supplier_expired']} expired certs)"]
    if m.get("_urgent"):
        L.append("- Most urgent (≤7 days):")
        for r in m["_urgent"][:6]:
            L.append(f"  - {r['first_name']} {r['last_name']} — {r['competency_name']}"
                     f" — {r['expiry_date']} ({r['days_to_expiry']:.0f}d)")

    L += ["", "## Commercial Director",
          f"- Outstanding receivables: **A${m['ar_total']:,.0f}** across {m['ar_count']} invoice(s)"
          f"{_tr(deltas, 'ar_total', '{:+,.0f}')}",
          f"- Last invoiced month ({m['rev_last_month_label']}): **A${m['rev_last_month']:,.0f}**",
          f"- ⚠ Xero data is **{m['xero_age_days']} days** behind"
          if m['xero_age_days'] > 14 else
          f"- Xero data current to {m['xero_age_days']} days ago"]

    L += ["", "## Automation Director",
          f"- Data quality sentinel: **{m['sentinel_status']}** · local Gold age: {m['gold_age_hours']:.0f}h"]
    if m.get("_automation_runs"):
        bad = [r for r in m["_automation_runs"] if r["conclusion"] not in ("success", None)]
        if bad:
            for r in bad:
                L.append(f"- 🔴 {r['name']}: last deploy **{r['conclusion']}** ({r['when']})")
        else:
            L.append(f"- All {len(m['_automation_runs'])} GitHub automations: last deploys green ✅")

    # ---- recommended actions (derived from alerts) ----
    ACTION = {"workforce_risk_high": "Open compliance review — re-validate certs for rostered workers first",
              "certs_expiring_week": "Book renewals for the ≤7d list above this week",
              "deployable_pool_thin": "Start recruitment / free up bench workers",
              "supplier_risk_concentrated": "Raise at the next supplier review meeting",
              "weekly_hours_swing": "Check rosters & timesheets for the swing",
              "data_quality_not_ok": "Check logs/data_quality_report.json before trusting today's numbers",
              "roster_horizon_short": "Check the OPMS roster extraction window",
              "gold_stale": "Check the nightly refresh / run the pipeline manually"}
    acts = [ACTION[a["rule"]] for a in alerts if a["rule"] in ACTION]
    if acts:
        L += ["", "## Recommended Actions"]
        L += [f"- [ ] {x}" for x in acts]

    L += ["", "---", "_Compiled by Lulu 🐈‍⬛ — your departments reported at "
          f"{dt.datetime.now():%H:%M}; nobody had to ask._"]
    return "\n".join(L)


# ---------------------------------------------------------------- email (Graph, payroll-system pattern)
def send_email(subject, markdown_body, registry):
    import os
    import requests
    from dotenv import load_dotenv
    load_dotenv(AGENT_DIR.parent / "Raw Data" / "API" / "credential" / ".env")

    tenant = os.getenv("SHAREPOINT_TENANT_ID")
    cid = os.getenv("SHAREPOINT_CLIENT_ID")
    sec = os.getenv("SHAREPOINT_CLIENT_SECRET")
    notify = registry.get("notify") or {}
    sender = os.getenv("GRAPH_SENDER", notify.get("from", "test@company.com.au"))
    recipients = notify.get("to") or []
    if not (tenant and cid and sec and recipients):
        return False, "missing Graph credentials or recipients"

    tok = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"client_id": cid, "client_secret": sec,
              "scope": "https://graph.microsoft.com/.default",
              "grant_type": "client_credentials"}, timeout=30)
    tok.raise_for_status()

    run_cat = AGENT_DIR / "static" / "lulu_run.png"
    markdown_body = markdown_body.replace(
        "🐈‍⬛", "<img src='cid:lulucat' height='20' style='vertical-align:middle'>"
        if run_cat.exists() else "")
    html = "<div style='font-family:Segoe UI,sans-serif;font-size:14px'>" + "".join(
        f"<h2 style='margin:14px 0 6px'>{l[3:]}</h2>" if l.startswith("## ")
        else f"<h1 style='margin:4px 0'>{l[2:]}</h1>" if l.startswith("# ")
        else "<hr>" if l == "---"
        else f"<div style='margin:2px 0'>{l[2:]}</div>" if l.startswith("- ")
        else f"<div style='margin:2px 0'>{l}</div>" if l else "<br>"
        for l in markdown_body.replace("**", "").replace("_", "").splitlines()) + "</div>"

    r = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
        headers={"Authorization": f"Bearer {tok.json()['access_token']}",
                 "Content-Type": "application/json"},
        json={"message": {"subject": subject,
                          "body": {"contentType": "HTML", "content": html},
                          "toRecipients": [{"emailAddress": {"address": a}} for a in recipients],
                          "attachments": ([{
                              "@odata.type": "#microsoft.graph.fileAttachment",
                              "name": "lulu_run.png", "contentId": "lulucat", "isInline": True,
                              "contentType": "image/png",
                              "contentBytes": __import__("base64").b64encode(run_cat.read_bytes()).decode(),
                          }] if run_cat.exists() else [])},
              "saveToSentItems": False}, timeout=30)   # testing: keep out of hr@'s Sent folder
    if r.status_code in (200, 202):
        return True, f"sent to {', '.join(recipients)}"
    return False, f"Graph sendMail {r.status_code}: {r.text[:300]}"


# ---------------------------------------------------------------- blob refresh (optional)
def refresh_gold_from_blob():
    """Pull the nightly-refreshed gold/*.parquet from Azure into the local lake."""
    import importlib.util
    api_dir = AGENT_DIR.parent / "Raw Data" / "API"
    spec = importlib.util.spec_from_file_location("u", str(api_dir / "upload_to_blob.py"))
    u = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(u)
    cc = u.get_service_client().get_container_client(u.CONTAINER)
    gold = AGENT_DIR.parent / "gold"
    n = 0
    for blob in cc.list_blobs(name_starts_with="gold/"):
        target = gold / Path(blob.name).name
        with open(target, "wb") as f:
            f.write(cc.download_blob(blob.name).readall())
        n += 1
    print(f"refreshed {n} gold tables from blob")
    return n


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--refresh-from-blob", action="store_true")
    args = ap.parse_args()

    if args.refresh_from_blob:
        try:
            refresh_gold_from_blob()
        except Exception as ex:
            print(f"blob refresh failed ({type(ex).__name__}: {ex}) — using local gold")

    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    metrics = collect_metrics()
    alerts = evaluate(metrics, registry)
    brief = build_brief(metrics, alerts)

    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()

    # ---- executive snapshot: persist today's numbers so tomorrow knows the trend ----
    snap = {"date": today.isoformat()}
    snap.update({k: v for k, v in metrics.items()
                 if not k.startswith("_") and isinstance(v, (int, float, str))})
    snaps_path = SNAP_PATH
    existing = []
    if snaps_path.exists():
        existing = [json.loads(l) for l in open(snaps_path, encoding="utf-8")]
    existing = [s for s in existing if s.get("date") != snap["date"]] + [snap]   # one per day
    with open(snaps_path, "w", encoding="utf-8") as f:
        for s in existing:
            f.write(json.dumps(s, ensure_ascii=False, default=str) + "\n")
    try:                                       # queryable: Lulu can answer trend questions
        import pandas as pd
        pd.DataFrame(existing).to_parquet(AGENT_DIR.parent / "gold" / "executive_snapshot.parquet",
                                          index=False)
    except Exception as ex:
        print(f"snapshot parquet skipped: {ex}")
    out = BRIEF_DIR / f"brief_{today:%Y-%m-%d}.md"
    out.write_text(brief, encoding="utf-8")
    (BRIEF_DIR / "history.jsonl").open("a", encoding="utf-8").write(json.dumps(
        {"date": today.isoformat(), "alerts": len(alerts),
         "red": sum(a["severity"] == "red" for a in alerts),
         "rules": [a["rule"] for a in alerts]}, ensure_ascii=False) + "\n")
    print(f"brief saved: {out.name} ({len(alerts)} alerts)")
    print(brief)

    if not args.no_email:
        n_red = sum(a["severity"] == "red" for a in alerts)
        subject = (f"{registry.get('notify', {}).get('subject_prefix', 'Lulu Daily Brief')} "
                   f"— {today:%a %d %b} · "
                   + (f"{len(alerts)} alert(s)" + (f", {n_red} red" if n_red else "")
                      if alerts else "all clear"))
        ok, info = send_email(subject, brief, registry)
        print(("email sent: " if ok else "EMAIL FAILED: ") + info)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
