"""cockpit/issue_registry.py — unified Issue Registry, built from REAL checks (READ-ONLY).

The issue stream is DETECTED, not mocked: each detector reads a file the company already
produces and emits an issue ONLY when there is a real problem. Nothing is hard-coded; an issue
that no longer reproduces simply isn't emitted (its alert disappears on the next render).

Real sources (all read-only — no DuckDB, no external API, no writes):
    gold/training_compliance.parquet   -> expired / expiring certificates
    link_health.json                   -> broken / missing folder links (written by check_links.py)
    gold/roster_summary.parquet        -> roster_date null rate
    data_quality_report.json           -> sentinel status + report staleness
    snapshots.jsonl                    -> daily-brief staleness, Xero age, receivables, automation fails
    gold/invoice_register.parquet      -> readability (schema) check

build() returns: { issues, by_node, alert_count, node_meta, entities }
Run standalone to see the live stream:  python issue_registry.py
"""
import json
import datetime as _dt
from pathlib import Path

try:
    import yaml
except Exception:                       # pragma: no cover
    yaml = None

COCKPIT_DIR = Path(__file__).resolve().parent
AGENT_DIR = COCKPIT_DIR.parent                      # data/agent
GOLD_DIR = AGENT_DIR.parent / "gold"
LOGS_DIR = AGENT_DIR / "logs"
LINK_HEALTH = AGENT_DIR / "link_health.json"
# these two live in data/agent/ (out of the logs Azure Files mount) so they bake into the cloud image
DQ_REPORT = AGENT_DIR / "data_quality_report.json"
SNAPSHOTS = AGENT_DIR / "snapshots.jsonl"

_SEV_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_GOLD_CACHE = {}
_STALE_DAYS = 2                                      # a daily artifact older than this is "stale"


def _today():
    return _dt.date.today()


def _read_gold(name):
    """Read one Gold parquet (read-only, cached per process). None on any failure."""
    if name in _GOLD_CACHE:
        return _GOLD_CACHE[name]
    df = None
    try:
        import pandas as pd
        p = GOLD_DIR / (name + ".parquet")
        if p.exists():
            df = pd.read_parquet(p)
    except Exception:
        df = None
    _GOLD_CACHE[name] = df
    return df


def _read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_snapshot():
    if not SNAPSHOTS.exists():
        return {}
    try:
        lines = [l for l in SNAPSHOTS.read_text(encoding="utf-8").splitlines() if l.strip()]
        return json.loads(lines[-1]) if lines else {}
    except Exception:
        return {}


def _days_since(date_str):
    """Whole days between an ISO date/datetime string and today. None if unparseable."""
    if not date_str:
        return None
    try:
        d = _dt.date.fromisoformat(str(date_str)[:10])
        return (_today() - d).days
    except Exception:
        return None


def _mk(**kw):
    """Build an issue dict with the full set of keys the cockpit UI expects."""
    base = {"severity": "medium", "agent": "ops", "owner": "Operations", "repairable": False,
            "safety": "manual_only", "status": "open", "evidence_items": [], "related_entities": [],
            "next_actions": [], "audit_refs": [], "related_logs": [], "root_cause": "",
            "business_impact": "", "suggested_fix": "", "action_ref": None, "source": "live",
            "evidence_source": "live", "affected_count": 1,
            "detected_at": _dt.datetime.now().isoformat(timespec="seconds")}
    base.update(kw)
    return base


# ============================ REAL DETECTORS ============================
def _cert_evidence(e, expired):
    """Top-10 evidence rows from training_compliance (real employee IDs / names)."""
    import pandas as pd
    ev = []
    for _, r in e.head(10).iterrows():
        d2e = r.get("days_to_expiry")
        days = (int(abs(d2e)) if (expired and pd.notna(d2e)) else (int(d2e) if pd.notna(d2e) else ""))
        ev.append({"employee_id": (int(r["opms_employee_id"]) if pd.notna(r.get("opms_employee_id")) else ""),
                   "name": (str(r.get("first_name", "")) + " " + str(r.get("last_name", ""))).strip(),
                   "cert_type": r.get("competency_name", ""),
                   "expiry_date": str(r.get("expiry_date", ""))[:10],
                   ("days_expired" if expired else "days_left"): days})
    return ev


def _d_certs():
    out = []
    df = _read_gold("training_compliance")
    if df is None:
        return out
    if "is_expired" in df.columns:
        e = df[df["is_expired"] == True]
        if len(e):
            out.append(_mk(id="iss-cert-expired", type="expired_certificate", node="opms", agent="biz",
                           entity="certificates", owner="HR / Compliance", severity="high",
                           affected_count=int(len(e)), title=f"{len(e):,} certificates are expired",
                           root_cause="Workers hold competencies past their expiry date in OPMS.",
                           business_impact="Rostering a worker on an expired certificate is a compliance breach.",
                           suggested_fix="Email renewal reminders to the affected workers' supervisors.",
                           action_ref="notify_cert_expiry", safety="needs_approval", repairable=True,
                           evidence_source="gold:training_compliance", evidence_items=_cert_evidence(e, True),
                           related_entities=["roster"], next_actions=["Email expiry reminders", "Book renewals"]))
    if "is_expiring_soon" in df.columns:
        s = df[df["is_expiring_soon"] == True]
        if "days_to_expiry" in s.columns:
            s = s[s["days_to_expiry"] <= 7]
        if len(s):
            out.append(_mk(id="iss-cert-7d", type="expired_certificate", node="opms", agent="biz",
                           entity="certificates", owner="HR / Compliance", severity="medium",
                           affected_count=int(len(s)), title=f"{len(s):,} certificates expiring within 7 days",
                           root_cause="Competencies reach their expiry date within the next 7 days.",
                           business_impact="These workers become non-deployable unless renewed this week.",
                           suggested_fix="Book renewals for the ≤7-day list this week.",
                           action_ref="book_cert_renewals", safety="needs_approval", repairable=True,
                           evidence_source="gold:training_compliance", evidence_items=_cert_evidence(s, False),
                           related_entities=["roster"], next_actions=["Book renewals"]))
    return out


def _d_links():
    lh = _read_json(LINK_HEALTH)
    if not lh:
        return []
    broken = missing = 0
    items = []
    for tbl, v in lh.items():
        if not isinstance(v, dict) or "broken" not in v:
            continue
        broken += int(v.get("broken", 0))
        missing += int(v.get("missing", 0))
        for it in (v.get("items") or []):
            for col, stt in (it.get("cells") or {}).items():
                if stt != "ok":
                    items.append({"job_id": it.get("jobid", ""), "title": str(it.get("title", ""))[:48],
                                  "table": tbl, "folder": col, "status": stt})
    total = broken + missing
    if total == 0:
        return []
    checked = str(lh.get("checked_at", ""))[:10]
    return [_mk(id="iss-links", type="broken_folder_link", node="bms", agent="file",
                entity="jms_jobs", owner="File Agent", severity="medium", affected_count=total,
                title=f"{total} JMS folder link(s) unreachable (checked {checked})",
                root_cause="SharePoint folders were moved/renamed; the stored links are stale.",
                business_impact="Site crews can't open job documents — delays.",
                suggested_fix="Re-map the stale links to their current SharePoint location.",
                action_ref="repair_folder_links", safety="needs_approval", repairable=True,
                evidence_source="live:link_health.json", evidence_items=items[:20],
                detected_at=str(lh.get("checked_at", "")), related_entities=["bms_permissions"],
                next_actions=["Dry-run repair", "Repair folder links"])]


def _d_roster():
    df = _read_gold("roster_summary")
    if df is None or "roster_date" not in df.columns:
        return []
    total = len(df)
    miss = int(df["roster_date"].isna().sum())
    if miss == 0:
        return []
    pct = (100.0 * miss / total) if total else 0.0
    return [_mk(id="iss-roster", type="missing_field", node="opms", agent="biz", entity="roster",
                owner="Operations", severity="medium", affected_count=miss,
                title=f"{pct:.1f}% of roster rows are missing roster_date",
                root_cause="Roster rows arrived without a roster_date during the last build.",
                business_impact="Roster-based metrics (deployability, horizon) under-count.",
                suggested_fix="Backfill roster_date from the source window during rebuild.",
                action_ref="backfill_roster_date", safety="needs_approval", repairable=True,
                evidence_source="gold:roster_summary",
                evidence_items=[{"metric": "roster rows total", "value": f"{total:,}"},
                                {"metric": "rows missing roster_date", "value": f"{miss:,} ({pct:.1f}%)"}])]


def _d_freshness():
    out = []
    snap = _latest_snapshot()
    if snap:
        age = _days_since(snap.get("date"))
        if age is not None and age > _STALE_DAYS:
            out.append(_mk(id="iss-snapshot-stale", type="data_freshness", node="lake", agent="ops",
                           entity="data_quality", owner="Operations", severity="high", affected_count=age,
                           title=f"Daily-brief snapshot is {age} days stale (last {snap.get('date')})",
                           root_cause="The daily brief / snapshot job has not produced a fresh snapshot.",
                           business_impact="Today's-problems and finance figures are showing old numbers.",
                           suggested_fix="Re-run daily_brief.py --no-email to refresh the snapshot.",
                           action_ref="rerun_daily_brief", safety="safe_auto", repairable=True,
                           evidence_source="live:snapshots.jsonl",
                           evidence_items=[{"metric": "snapshot date", "value": snap.get("date", "?")},
                                           {"metric": "days stale", "value": age}]))
    rpt = _read_json(DQ_REPORT)
    if rpt:
        status = rpt.get("status", "?")
        if status in ("WARN", "FAIL"):
            bad = [c for c in rpt.get("checks", []) if not c.get("ok")]
            out.append(_mk(id="iss-dq-fail", type="data_freshness",
                           node="lake", agent="ops", entity="data_quality", owner="Operations",
                           severity=("critical" if status == "FAIL" else "medium"),
                           affected_count=len(bad), title=f"Data-quality sentinel: {status} "
                           f"({rpt.get('fails', 0)} fail, {rpt.get('warns', 0)} warn)",
                           root_cause="One or more data-quality checks did not pass on the latest run.",
                           business_impact="Downstream metrics built on the failing tables may be wrong.",
                           suggested_fix="Open the data_quality_report and fix the failing checks.",
                           action_ref="rerun_data_quality_sentinel", safety="safe_auto", repairable=True,
                           evidence_source="live:data_quality_report.json",
                           evidence_items=[{"check": c.get("check"), "level": c.get("level"),
                                            "detail": c.get("detail")} for c in bad[:20]]))
        age = _days_since(rpt.get("ts"))
        if age is not None and age > _STALE_DAYS:
            out.append(_mk(id="iss-dq-stale", type="data_freshness", node="lake", agent="ops",
                           entity="data_quality", owner="Operations", severity="medium", affected_count=age,
                           title=f"Data-quality report is {age} days stale (last {str(rpt.get('ts',''))[:10]})",
                           root_cause="The data-quality sentinel has not run recently.",
                           business_impact="We have no fresh confirmation that the Gold layer is sound.",
                           suggested_fix="Re-run the data-quality sentinel.",
                           action_ref="rerun_data_quality_sentinel", safety="safe_auto", repairable=True,
                           evidence_source="live:data_quality_report.json",
                           evidence_items=[{"metric": "report timestamp", "value": str(rpt.get("ts", ""))[:19]},
                                           {"metric": "days stale", "value": age}]))
    return out


def _d_finance():
    out = []
    snap = _latest_snapshot()
    xa = snap.get("xero_age_days")
    # 9999 is the daily-brief sentinel for "no Xero data" — that happens when invoice_register is
    # the broken 0-column parquet, which iss-invoice already reports. Don't show a fake "9999 days".
    if isinstance(xa, (int, float)) and 30 < xa < 9000:
        out.append(_mk(id="iss-xero-stale", type="failed_sync", node="xero", agent="fin",
                       entity="invoice_register", owner="Finance", severity="high", affected_count=int(xa),
                       title=f"Xero data is {int(xa)} days behind (as of {snap.get('date')})",
                       root_cause="The Xero / OpsDB mirror has not synced recently.",
                       business_impact="Receivables and revenue figures end ~"
                                       f"{snap.get('rev_last_month_label', '?')}.",
                       suggested_fix="Restore Xero API access, then re-sync the OpsDB mirror.",
                       action_ref="recheck_xero", safety="manual_only", repairable=False,
                       evidence_source="live:snapshots.jsonl",
                       evidence_items=[{"metric": "Xero age (days)", "value": int(xa)},
                                       {"metric": "revenue ends", "value": snap.get("rev_last_month_label", "?")}]))
    arc = snap.get("ar_count")
    if isinstance(arc, (int, float)) and arc > 0:
        art = snap.get("ar_total", 0) or 0
        out.append(_mk(id="iss-ar", type="finance_mismatch", node="xero", agent="fin", entity="ar_aging",
                       owner="Finance", severity="medium", affected_count=int(arc),
                       title=f"{int(arc)} invoices outstanding · ${art:,.0f} receivable",
                       root_cause="Issued invoices remain unpaid past their due window.",
                       business_impact="Cash tied up in receivables.",
                       suggested_fix="Review the aged-receivables list with Finance.",
                       action_ref="review_ar", safety="manual_only", repairable=False,
                       evidence_source="live:snapshots.jsonl",
                       evidence_items=[{"metric": "outstanding invoices", "value": int(arc)},
                                       {"metric": "receivables total", "value": f"${art:,.0f}"}]))
    # invoice_register readability (the known 0-column-parquet failure mode)
    p = GOLD_DIR / "invoice_register.parquet"
    if p.exists():
        ncols = None
        try:
            import pyarrow.parquet as _pq
            ncols = len(_pq.ParquetFile(str(p)).schema_arrow.names)
        except Exception:
            ncols = None
        if ncols == 0:
            out.append(_mk(id="iss-invoice", type="finance_mismatch", node="xero", agent="fin",
                           entity="invoice_register", owner="Finance / Data", severity="critical",
                           affected_count=1, title="invoice_register table is unreadable (0-column parquet)",
                           root_cause="An empty pipeline output wrote a 0-column parquet.",
                           business_impact="Finance views that read invoice_register are incomplete.",
                           suggested_fix="Rebuild invoice_register once Xero data refreshes.",
                           action_ref="rebuild_invoice_register", safety="needs_approval", repairable=True,
                           evidence_source="live:gold/invoice_register.parquet",
                           evidence_items=[{"metric": "columns", "value": 0}]))
    return out


def _d_automation():
    snap = _latest_snapshot()
    af = snap.get("automation_failures")
    if isinstance(af, (int, float)) and af > 0:
        return [_mk(id="iss-automation", type="failed_sync", node="functions", agent="ops",
                    entity="daily_jobs", owner="Operations", severity="high", affected_count=int(af),
                    title=f"{int(af)} automation job(s) failing",
                    root_cause="One or more scheduled automation jobs reported a failure.",
                    business_impact="Downstream data may not refresh on schedule.",
                    suggested_fix="Investigate the failing job logs.",
                    action_ref="investigate_oom", safety="manual_only", repairable=False,
                    evidence_source="live:snapshots.jsonl",
                    evidence_items=[{"metric": "automation failures", "value": int(af)}])]
    return []


_DETECTORS = [_d_certs, _d_links, _d_roster, _d_freshness, _d_finance, _d_automation]


def _detect_issues():
    issues = []
    for d in _DETECTORS:
        try:
            issues += d()
        except Exception:
            pass
    return issues


# ============================ assembly (unchanged shape) ============================
def _load_yaml(name):
    p = COCKPIT_DIR / name
    if not p.exists() or yaml is None:
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _worse(a, b):
    return a if _SEV_RANK.get(a, 0) >= _SEV_RANK.get(b, 0) else b


def build():
    """Return the unified issue view, freshly detected from real sources on every render."""
    issues = _detect_issues()
    actions = _load_yaml("actions.yaml").get("actions", {}) or {}
    nodes = _load_yaml("nodes.yaml").get("nodes", {}) or {}
    entities_cfg = _load_yaml("entities.yaml").get("entities", {}) or {}

    by_node = {}
    open_issues = []
    for iss in issues:
        if not isinstance(iss, dict) or iss.get("status") == "resolved":
            continue
        ref = iss.get("action_ref")
        act = actions.get(ref) if (ref and isinstance(actions, dict)) else None
        if isinstance(act, dict):
            iss.setdefault("action_label", act.get("label", ref))
            if not iss.get("safety"):
                iss["safety"] = act.get("safety", "manual_only")
        else:
            iss.setdefault("action_label", iss.get("suggested_fix", ""))

        open_issues.append(iss)
        sev = iss.get("severity", "low")
        for key in {iss.get("node"), iss.get("agent")}:
            if not key:
                continue
            nh = by_node.setdefault(key, {"count": 0, "severity": "low"})
            nh["count"] += 1
            nh["severity"] = _worse(nh["severity"], sev)

    node_meta = {}
    for key, n in (nodes.items() if isinstance(nodes, dict) else []):
        n = n or {}
        deps = n.get("depends_on", []) or []
        dep_labels = [(nodes.get(d, {}) or {}).get("label", d) for d in deps]
        rel = [i for i in open_issues if i.get("node") == key or i.get("agent") == key]
        logs = [{"t": str(i.get("detected_at", ""))[:16].replace("T", " "),
                 "text": "detected · " + i.get("title", "")} for i in rel[:6]]
        audit = []
        for i in rel:
            for ref in (i.get("audit_refs") or []):
                audit.append({"t": str(i.get("detected_at", ""))[:10], "text": ref})
        node_meta[key] = {"label": n.get("label", key), "type": n.get("type", ""),
                          "purpose": n.get("business_purpose", ""), "deps": dep_labels,
                          "logs": logs, "audit": audit}

    entities = {}
    for ek, e in (entities_cfg.items() if isinstance(entities_cfg, dict) else []):
        e = e or {}
        eiss = [i for i in open_issues if i.get("entity") == ek]
        sev = "low"
        for i in eiss:
            sev = _worse(sev, i.get("severity", "low"))
        entities[ek] = {"key": ek, "label": e.get("label", ek),
                        "parent_system": e.get("parent_system", ""), "agent": e.get("agent", ""),
                        "type": e.get("type", ""), "purpose": e.get("business_purpose", ""),
                        "issue_ids": [i.get("id") for i in eiss],
                        "count": len(eiss), "severity": (sev if eiss else "healthy")}

    return {"issues": open_issues, "by_node": by_node, "alert_count": len(open_issues),
            "node_meta": node_meta, "entities": entities}


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    v = build()
    print(f"alert_count = {v['alert_count']}  (all detected live from real sources)\n")
    for i in v["issues"]:
        print(f"  [{i['severity']:8}] {i['id']:18} {i['title']}")
        print(f"             src={i['evidence_source']}  action={i.get('action_ref')}  safety={i.get('safety')}")
