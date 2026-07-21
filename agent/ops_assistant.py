"""ops_assistant.py — turns a raw engine answer into an OPERATIONAL answer contract.

Presentation/derivation layer ONLY (not a business tool — no SQL, no new agent tool, no writes).
Given what an engine already returned (tables touched, domain, answer), it derives the extra
context that makes Ask LuLu an Ops Analyst instead of a chatbot:

    data freshness  — are the sources behind this answer fresh? (reuses the real signals from
                      snapshots.jsonl / data_quality_report.json / link_health.json)
    evidence        — which source systems + when last updated
    risk            — relevant OPEN issues from the live issue_registry detection
    missing data    — for intents whose data isn't captured yet (e.g. shutdown demand), say so
    intent          — classify the question (Compliance / Workforce / Shutdown / Billing / …)
    next actions    — relevant approval-gated actions from the cockpit action catalogue

All read-only. Safe to call on every answer.
"""
import datetime as _dt
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
COCKPIT_DIR = AGENT_DIR / "cockpit"

# ---- which source system a Gold table comes from (for evidence + freshness) ----
TABLE_SOURCE = {
    # OPMS (people / certs / roster / timesheet)
    "employee_profile": "OPMS", "training_compliance": "OPMS", "roster_summary": "OPMS",
    "timesheet_summary": "OPMS", "weekly_timesheet": "OPMS", "site_assignment": "OPMS",
    "worker_ranking": "OPMS", "licence_register": "OPMS", "supplier_summary": "OPMS",
    # SharePoint / BMS (jobs / projects / docs / HSEQ / assets)
    "project_job_summary": "SharePoint/BMS", "project_bridge": "SharePoint/BMS",
    "job_detail": "SharePoint/BMS", "hseq_register": "SharePoint/BMS",
    "asset_register": "SharePoint/BMS", "hardware_register": "SharePoint/BMS",
    "inventory_summary": "SharePoint/BMS",
    # Xero / OpsDB (finance)
    "invoice_register": "Xero", "revenue_summary": "Xero", "purchase_summary": "Xero",
    "executive_snapshot": "Xero",
}

QUICK_PROMPTS = {
    "Compliance": ["Who is not compliant today?",
                   "Which certificates are expiring in the next 30 days?",
                   "Which workers can't be assigned to site work?"],
    "Workforce": ["Who is available this week?", "Who is on the bench?",
                  "Which supplier has the biggest compliance risk?"],
    "Shutdown": ["What do we know about the next shutdown?",
                 "Do we have enough compliant workers for upcoming work?",
                 "What requirements are missing for the next shutdown?"],
    "Billing": ["Which invoices are outstanding?", "How stale is our Xero data?",
                "What is our total receivable?"],
    "Project": ["Which projects have the most at-risk workers?",
                "Show roster risk by project."],
    "Data Health": ["Which data sources are stale?", "Is the data-quality report current?",
                    "Are any folder links broken?"],
}

_INTENT_KEYS = [
    ("Shutdown",   ["shutdown", "turnaround", "检修", "停产", "大修"]),
    ("Compliance", ["compliant", "compliance", "certificate", "cert", "ticket", "expir",
                    "证", "合规", "过期", "到期"]),
    ("Billing",    ["invoice", "bill", "receivable", "overdue", "xero", "revenue",
                    "发票", "工时", "billing", "应收"]),
    ("Data Health", ["stale", "sync", "data quality", "freshness", "broken", "missing file",
                     "数据", "新鲜", "坏了", "陈旧"]),
    ("Workforce",  ["available", "bench", "idle", "deploy", "roster", "who can",
                    "可以派", "板凳", "排班", "能去", "可派"]),
    ("Project",    ["project", "job", "status", "项目", "进度"]),
    ("Document",   ["document", "folder", "file", "文件", "文档"]),
]

# intent -> which issue entities/types are relevant + which actions to surface
_INTENT_ENTITIES = {
    "Compliance": {"certificates"}, "Workforce": {"roster"},
    "Billing": {"invoice_register", "ar_aging"}, "Data Health": {"data_quality", "silver_tables"},
    "Project": {"jms_jobs", "project_bridge"}, "Document": {"jms_jobs", "missing_documents"},
    "Shutdown": {"roster", "certificates"},
}
_INTENT_ACTIONS = {
    "Compliance": ["notify_cert_expiry", "book_cert_renewals"],
    "Billing": ["review_ar", "rebuild_invoice_register"],
    "Data Health": ["rerun_data_quality_sentinel", "recheck_folder_links"],
    "Workforce": [], "Project": [], "Document": ["review_missing_docs"], "Shutdown": [],
}


def _today():
    return _dt.date.today()


def _days_since(s):
    try:
        return (_today() - _dt.date.fromisoformat(str(s)[:10])).days
    except Exception:
        return None


def _ireg():
    import sys
    if str(COCKPIT_DIR) not in sys.path:
        sys.path.insert(0, str(COCKPIT_DIR))
    import issue_registry as ir
    return ir


def source_status():
    """Freshness of each source system, from the same real signals the cockpit uses.
    Returns {source: {"asof": str, "status": "ok|warn|stale", "note": str}}."""
    ir = _ireg()
    snap = ir._latest_snapshot()
    rpt = ir._read_json(ir.DQ_REPORT) or {}
    lh = ir._read_json(ir.LINK_HEALTH) or {}
    out = {}

    # data lake / OPMS+SharePoint Gold freshness ← data-quality report run + daily snapshot
    rpt_age = _days_since(rpt.get("ts"))
    snap_age = _days_since(snap.get("date"))
    lake_age = min([a for a in (rpt_age, snap_age) if a is not None], default=None)
    out["OPMS"] = {"asof": str(snap.get("date", "?")),
                   "status": ("ok" if (snap_age is not None and snap_age <= 2) else "stale"),
                   "note": (f"{snap_age}d old" if snap_age is not None else "unknown")}
    # SharePoint folder side ← link_health checked_at
    lh_age = _days_since(lh.get("checked_at"))
    out["SharePoint/BMS"] = {"asof": str(lh.get("checked_at", "?"))[:10],
                             "status": ("ok" if (lh_age is not None and lh_age <= 2) else "stale"),
                             "note": (f"{lh_age}d old" if lh_age is not None else "unknown")}
    # Xero ← snapshot xero_age_days
    xa = snap.get("xero_age_days")
    out["Xero"] = {"asof": (snap.get("rev_last_month_label", "?")),
                   "status": ("ok" if (isinstance(xa, (int, float)) and xa <= 30) else "stale"),
                   "note": (f"{int(xa)}d behind" if isinstance(xa, (int, float)) else "unknown")}
    out["Data lake"] = {"asof": str(rpt.get("ts", "?"))[:10],
                        "status": ("ok" if (rpt_age is not None and rpt_age <= 2) else "stale"),
                        "note": (f"DQ report {rpt_age}d old" if rpt_age is not None else "unknown")}
    return out


def sources_for(tables):
    """Distinct source systems behind the tables this answer used."""
    srcs = []
    for t in (tables or []):
        s = TABLE_SOURCE.get(t)
        if s and s not in srcs:
            srcs.append(s)
    return srcs


def freshness_warning(tables):
    """A one-line caution if any source behind this answer is stale (else '')."""
    st = source_status()
    bad = []
    for s in sources_for(tables):
        d = st.get(s, {})
        if d.get("status") == "stale":
            bad.append(f"{s} ({d.get('note','')})")
    if not bad:
        return ""
    return "I can answer this, but " + ", ".join(bad) + " — so this may be incomplete."


def classify_intent(question, domain=""):
    q = (question or "").lower()
    for name, keys in _INTENT_KEYS:
        if any(k in q for k in keys):
            return name
    # fall back to the deterministic planner's own domain label
    d = (domain or "").lower()
    if "complian" in d or "cert" in d:
        return "Compliance"
    if "finance" in d or "invoice" in d:
        return "Billing"
    return "General"


def risks_for(intent, tables):
    """Relevant OPEN issues from the live detection stream (read-only)."""
    try:
        v = _ireg().build()
    except Exception:
        return []
    ents = set(_INTENT_ENTITIES.get(intent, set()))
    # also include any entity whose table is in this answer
    hits = []
    for iss in v.get("issues", []):
        if iss.get("entity") in ents:
            hits.append({"title": iss.get("title", ""), "severity": iss.get("severity", ""),
                         "id": iss.get("id"), "action_ref": iss.get("action_ref")})
    return hits


def actions_for(intent):
    """Approval-gated actions worth offering for this intent (keys into cockpit/actions.yaml)."""
    keys = list(_INTENT_ACTIONS.get(intent, []))
    keys.append("export_affected_records")          # export is always useful
    out = []
    try:
        import sys
        if str(COCKPIT_DIR) not in sys.path:
            sys.path.insert(0, str(COCKPIT_DIR))
        import action_runner as ar
        cat = ar.load_actions()
        for k in keys:
            a = cat.get(k)
            if a:
                out.append({"key": k, "label": a.get("label", k),
                            "safety": a.get("safety", "manual_only"), "writes": bool(a.get("writes"))})
    except Exception:
        pass
    return out


_INTENT_SRC = {"Compliance": "OPMS", "Workforce": "OPMS", "Shutdown": "OPMS", "Billing": "Xero",
               "Project": "SharePoint/BMS", "Document": "SharePoint/BMS", "Data Health": "Data lake"}


def build_context(question, domain="", tables=None):
    """One call -> the full answer-contract context for an answer (read-only)."""
    intent = classify_intent(question, domain)
    srcs = sources_for(tables)
    isrc = _INTENT_SRC.get(intent)
    if isrc and isrc not in srcs:
        srcs.append(isrc)
    st = source_status()
    fresh = ([{"source": s, **st.get(s, {})} for s in srcs] if srcs
             else [{"source": k, **v} for k, v in st.items()])
    stale = [f"{f['source']} ({f.get('note', '')})" for f in fresh if f.get("status") == "stale"]
    warn = ("I can answer this, but " + ", ".join(stale) + " — so this may be incomplete."
            if stale else "")
    return {"intent": intent, "freshness": fresh, "freshness_warning": warn,
            "risks": risks_for(intent, tables), "actions": actions_for(intent),
            "missing": missing_data(intent)}


def missing_data(intent):
    """If this intent needs data we haven't captured, say exactly what's missing (else None)."""
    if intent == "Shutdown":
        return {"what": "structured shutdown requirement",
                "need": ["site", "date", "trade", "required headcount", "required tickets", "PPE"],
                "say": ("I can see the available compliant workers, but I can't confirm the required "
                        "headcount — no structured shutdown requirement has been captured yet."),
                "action_label": "Create shutdown requirement"}
    return None
