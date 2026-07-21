"""
Operations Center metrics — the data behind the dashboard.

Every number here goes through the SAME safety chain as the agent
(QueryTool -> sql_validator -> DuckDB -> Gold). Exact COUNTs (not LIMIT-capped),
so the boss's KPI cards are precise. No UI code in this file.
"""

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_DIR))

from query_tool import QueryTool          # noqa: E402
from tools import build_tools             # noqa: E402

_qt = None
_tools = None

# the admin's Plan B: deployable counts FIELD trades only
FIELD_POSITIONS = ["driver", "operator", "scaffold", "boilermaker", "trade assistant", "rigger"]
_FIELD_POS_SQL = " OR ".join(f"e.position_name ILIKE '%{p}%'" for p in FIELD_POSITIONS)


def qt():
    global _qt
    if _qt is None:
        _qt = QueryTool()
    return _qt


def tools():
    global _tools
    if _tools is None:
        _tools = build_tools()
    return _tools


def _count(sql):
    r = qt().run(sql)
    return int(r.rows[0][0]) if r.ok and r.rows else 0


def _rows(sql, role="default"):
    r = qt().run(sql, role)
    return [dict(zip(r.cols, row)) for row in r.rows] if r.ok else []


# ---------------- KPI cards (exact counts) ----------------
def get_kpis():
    return {
        # rostered last 30d AND holding expired certs = deployment risk combos
        "workforce_risk": _count(
            "SELECT COUNT(*) AS n FROM ("
            "SELECT DISTINCT r.opms_employee_id, t.competency_name "
            "FROM roster_summary r JOIN training_compliance t "
            "ON r.opms_employee_id = t.opms_employee_id "
            "WHERE t.is_expired = true "
            "AND CAST(r.roster_date AS DATE) >= CURRENT_DATE - INTERVAL '30' DAY)"),
        "expiring_30d": _count(
            "SELECT COUNT(*) AS n FROM training_compliance "
            "WHERE is_expired = false AND days_to_expiry BETWEEN 0 AND 30"),
        "idle_pool": _count(
            "SELECT COUNT(*) AS n FROM employee_profile e WHERE e.is_active = true "
            "AND e.opms_employee_id NOT IN (SELECT r.opms_employee_id FROM roster_summary r "
            "WHERE r.opms_employee_id IS NOT NULL "
            "AND CAST(r.roster_date AS DATE) >= CURRENT_DATE - INTERVAL '90' DAY)"),
        "active_projects": _count(
            "SELECT COUNT(*) AS n FROM project_job_summary WHERE is_active = true"),
        "expired_total": _count(
            "SELECT COUNT(*) AS n FROM training_compliance WHERE is_expired = true"),
        # Plan B: deployable = FIELD workers only (driver/operator/scaffolder/boilermaker/TA/rigger)
        "deployable": _count(
            "SELECT COUNT(*) AS n FROM employee_profile e WHERE e.is_active = true "
            "AND (" + _FIELD_POS_SQL + ") "
            "AND e.opms_employee_id NOT IN (SELECT t.opms_employee_id FROM training_compliance t "
            "WHERE t.is_expired = true AND t.opms_employee_id IS NOT NULL) "
            "AND e.opms_employee_id NOT IN (SELECT r.opms_employee_id FROM roster_summary r "
            "WHERE r.opms_employee_id IS NOT NULL AND CAST(r.roster_date AS DATE) >= CURRENT_DATE)"),
    }


def get_business_exposure():
    """Business-impact framing for the dashboard: people and jobs, not record counts.
    2,163 expired certificate RECORDS might be only ~80 actual workers — the boss needs
    the worker/job numbers, the records belong in the detail view."""
    at_risk_sub = ("SELECT t.opms_employee_id FROM training_compliance t "
                   "WHERE t.is_expired = true AND t.opms_employee_id IS NOT NULL")
    return {
        # distinct ACTIVE workers holding >=1 expired cert
        "workers_at_risk": _count(
            "SELECT COUNT(DISTINCT t.opms_employee_id) AS n FROM training_compliance t "
            "WHERE t.is_expired = true AND t.is_active = true"),
        # of those, how many are rostered today or later (immediate deployment exposure)
        "at_risk_rostered": _count(
            "SELECT COUNT(DISTINCT r.opms_employee_id) AS n FROM roster_summary r "
            "WHERE CAST(r.roster_date AS DATE) >= CURRENT_DATE "
            f"AND r.opms_employee_id IN ({at_risk_sub})"),
        # upcoming projects that have at least one at-risk worker on the roster
        "jobs_exposed": _count(
            "SELECT COUNT(DISTINCT r.project_name) AS n FROM roster_summary r "
            "WHERE CAST(r.roster_date AS DATE) >= CURRENT_DATE AND r.project_name IS NOT NULL "
            f"AND r.opms_employee_id IN ({at_risk_sub})"),
    }


# ---------------- Workforce Command Center ----------------
def get_supplier_risk(top_n=8):
    return _rows(
        "SELECT e.supplier_name, COUNT(DISTINCT e.opms_employee_id) AS workers_with_expired, "
        "COUNT(*) AS expired_certs FROM employee_profile e JOIN training_compliance t "
        "ON e.opms_employee_id = t.opms_employee_id "
        "WHERE t.is_expired = true AND e.supplier_name IS NOT NULL "
        f"GROUP BY e.supplier_name ORDER BY expired_certs DESC LIMIT {int(top_n)}")


def get_expiry_ladder():
    return {d: _count("SELECT COUNT(*) AS n FROM training_compliance "
                      f"WHERE is_expired = false AND days_to_expiry BETWEEN 0 AND {d}")
            for d in (7, 30, 90)}


def get_risk_by_project(top_n=8, days=90):
    return _rows(
        "SELECT r.project_name, COUNT(DISTINCT r.opms_employee_id) AS at_risk_workers "
        "FROM roster_summary r JOIN training_compliance t "
        "ON r.opms_employee_id = t.opms_employee_id "
        "WHERE t.is_expired = true AND r.project_name IS NOT NULL "
        f"AND CAST(r.roster_date AS DATE) >= CURRENT_DATE - INTERVAL '{int(days)}' DAY "
        f"GROUP BY r.project_name ORDER BY at_risk_workers DESC LIMIT {int(top_n)}")


def get_deployable_preview(top_n=12):
    return _rows(
        "SELECT e.first_name, e.last_name, e.position_name, e.supplier_name "
        "FROM employee_profile e WHERE e.is_active = true "
        "AND (" + _FIELD_POS_SQL + ") "
        "AND e.opms_employee_id NOT IN (SELECT t.opms_employee_id FROM training_compliance t "
        "WHERE t.is_expired = true AND t.opms_employee_id IS NOT NULL) "
        "AND e.opms_employee_id NOT IN (SELECT r.opms_employee_id FROM roster_summary r "
        "WHERE r.opms_employee_id IS NOT NULL AND CAST(r.roster_date AS DATE) >= CURRENT_DATE) "
        f"ORDER BY e.last_name LIMIT {int(top_n)}")


def get_urgent_expiries(days=7, top_n=12):
    return _rows(
        "SELECT first_name, last_name, competency_name, expiry_date, days_to_expiry "
        "FROM training_compliance WHERE is_expired = false "
        f"AND days_to_expiry BETWEEN 0 AND {int(days)} ORDER BY days_to_expiry LIMIT {int(top_n)}")


def get_expiry_forecast(months=6):
    """Certs expiring per month — the PBI trend bar."""
    return _rows(
        "SELECT SUBSTR(expiry_date, 1, 7) AS month, COUNT(*) AS certs_expiring "
        "FROM training_compliance WHERE is_expired = false "
        f"AND days_to_expiry BETWEEN 0 AND {int(months) * 31} "
        "GROUP BY SUBSTR(expiry_date, 1, 7) ORDER BY month LIMIT 12")


def get_workforce_by_supplier(top_n=8):
    """Active headcount per supplier — the PBI donut."""
    return _rows(
        "SELECT supplier_name, COUNT(*) AS workers FROM employee_profile "
        "WHERE is_active = true AND supplier_name IS NOT NULL "
        f"GROUP BY supplier_name ORDER BY workers DESC LIMIT {int(top_n)}")


def get_weekly_hours(weeks=10):
    """Actual worked hours per ISO week (weekly_timesheet) — the PBI area trend."""
    return _rows(
        "SELECT CAST(DATE_TRUNC('week', CAST(work_date AS DATE)) AS VARCHAR) AS week_start, "
        "SUM(actual_hours) AS actual_hours, COUNT(DISTINCT opms_employee_id) AS workers "
        "FROM weekly_timesheet "
        f"WHERE CAST(work_date AS DATE) >= CURRENT_DATE - INTERVAL '{int(weeks) * 7}' DAY "
        "GROUP BY DATE_TRUNC('week', CAST(work_date AS DATE)) ORDER BY week_start LIMIT 60")


def get_recommendations(k=None):
    """Today's problems, stated directly — no question needed."""
    k = k or get_kpis()
    recs = []
    if k["workforce_risk"] > 0:
        recs.append(("🔴", f"{k['workforce_risk']} rostered-worker/expired-cert combinations in the last 30 days "
                            "— re-validate these certs FIRST (deployment compliance exposure)."))
    lad = get_expiry_ladder()
    if lad[7] > 0:
        recs.append(("🟠", f"{lad[7]} certs expire within 7 days — book renewals this week."))
    if k["idle_pool"] > 0 and k["workforce_risk"] > 0:
        recs.append(("🟢", f"{k['deployable']} fully-compliant workers are deployable now and "
                            f"{k['idle_pool']} active workers sit idle (90d no roster) — backfill from this pool."))
    sup = get_supplier_risk(1)
    if sup:
        recs.append(("🟠", f"Supplier {sup[0]['supplier_name']} concentrates the risk "
                            f"({sup[0]['expired_certs']} expired certs across {sup[0]['workers_with_expired']} workers) "
                            "— raise it in the next supplier review."))

    # folder-link health (from check_links.py -> data/agent/link_health.json) — surfaced as a problem
    try:
        import json as _json
        from pathlib import Path as _P
        _lh = _P(__file__).resolve().parent / "link_health.json"
        if _lh.exists():
            for _tbl, _v in _json.loads(_lh.read_text(encoding="utf-8")).items():
                if not isinstance(_v, dict) or "missing" not in _v:
                    continue  # skip 'checked_at' etc.
                if _v.get("broken"):
                    recs.append(("🔴", f"{_tbl}: {_v['broken']} stale folder links across {_v.get('problem_items','?')} "
                                       "jobs — contract folders were renamed/renumbered; re-run the MFS folder flow on "
                                       "those jobs to repoint the links."))
                if _v.get("missing"):
                    recs.append(("🟠", f"{_tbl}: {_v['missing']} jobs missing a folder link — usually a required "
                                       "field was left blank, or the creator never clicked refresh. Chase the owner."))
    except Exception:
        pass
    return recs
