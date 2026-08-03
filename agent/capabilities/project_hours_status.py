"""
project_hours_status — canonical hours logic (baseline §17/§18).

THE single authoritative computation for "how many hours has this job done /
scheduled / remaining". Every consumer (LuLu tools, MCP, future Power BI and
the Hours Remaining site) must call this or read the same data product —
never re-derive the numbers.

Hard rules:
  * Statuses are never merged: scheduled / actual(day/night) / quote /
    invoiced are separate figures.
  * A figure whose source data is not yet available is reported in
    insufficient_data — NEVER substituted with a different status's number.
  * Output follows the §6 unified agent-result shape.

All reads go through sql_validator (Gold-only, field-gated)."""
import re
from datetime import date

from sql_validator import run_query

_REF_RX = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,19}$")


def _rows(sql):
    rows, cols, r = run_query(sql)
    if not r.ok:
        raise RuntimeError("; ".join(r.errors) or "query failed")
    return [dict(zip(cols, row)) for row in (rows or [])]


def compute(job_ref, user_role="default"):
    """-> §6 unified result dict for one job code (e.g. 'SH-26046')."""
    out = {
        "facts": [], "warnings": [], "exceptions": [], "possible_causes": [],
        "recommended_actions": [], "required_approvals": [],
        "data_freshness": {}, "sources": ["gold/job_detail", "gold/roster_summary",
                                          "gold/weekly_timesheet"],
        "confidence": "high", "insufficient_data": [],
    }
    ref = str(job_ref).strip().upper()
    if not _REF_RX.match(ref):
        out["confidence"] = "low"
        out["exceptions"].append(f"'{job_ref}' is not a valid job reference format.")
        return out

    jobs = _rows("SELECT job_code, job_title, job_status, is_active, project_name, "
                 f"client_name, modified_at FROM job_detail WHERE upper(job_code) = '{ref}'")
    if not jobs:
        out["confidence"] = "low"
        out["exceptions"].append(f"Job '{ref}' not found in job_detail.")
        out["recommended_actions"].append("Check the job reference, or search jobs via get_project_jobs.")
        return out
    job = jobs[0]
    out["facts"].append(
        f"Job {job['job_code']} — {job['job_title']} | BMS project '{job['project_name']}' "
        f"| client {job['client_name']} | status {job['job_status']}")

    # OPMS roster/timesheet project names embed the job code ("SH-26046 - SFT2602 Major"),
    # so hours are matched at JOB granularity by code prefix — not by BMS project.
    like = f"{ref}%"
    today = date.today().isoformat()
    sched = _rows("SELECT sum(hours) AS h, count(*) AS n, max(roster_date) AS maxd "
                  f"FROM roster_summary WHERE upper(project_name) LIKE '{like}'")[0]
    sched_future = _rows("SELECT sum(hours) AS h, count(*) AS n FROM roster_summary "
                         f"WHERE upper(project_name) LIKE '{like}' AND roster_date >= '{today}'")[0]
    actual = _rows("SELECT sum(actual_hours) AS h, sum(roster_hours) AS rh, "
                   "max(work_date) AS maxd FROM weekly_timesheet "
                   f"WHERE upper(project_name) LIKE '{like}'")[0]
    shifts = _rows("SELECT shift_type, sum(actual_hours) AS h FROM weekly_timesheet "
                   f"WHERE upper(project_name) LIKE '{like}' GROUP BY shift_type")

    roster_rows = int(sched["n"] or 0)
    future_rows = int(sched_future["n"] or 0)
    total_sched = round(float(sched["h"] or 0), 1)
    future_sched = round(float(sched_future["h"] or 0), 1)
    observed_sched = round(float(actual["rh"] or 0), 1)   # roster hours as seen by gap logic
    total_actual = round(float(actual["h"] or 0), 1)

    if roster_rows and total_sched == 0:
        # known OPMS/PPL-Rosters quirk: roster rows exist with Hours=0
        out["facts"].append(
            f"Scheduled (roster): {roster_rows} roster entries to {sched['maxd']} "
            f"({future_rows} on future dates) — OPMS hours field is 0 on these entries.")
        out["insufficient_data"].append(
            "scheduled_hours: roster entries carry Hours=0 (known OPMS quirk) — "
            f"entry counts reported instead; observed roster hours from gap analysis: {observed_sched:,} h.")
    else:
        out["facts"].append(
            f"Scheduled (roster): {total_sched:,} h total, of which {future_sched:,} h "
            f"are on future rosters ({roster_rows} entries).")

    out["facts"].append(f"Actual (timesheet-sourced): {total_actual:,} h recorded to {actual['maxd']}.")
    for s in shifts:
        if s["shift_type"]:
            out["facts"].append(f"  {s['shift_type']}: {round(float(s['h'] or 0), 1):,} h")

    past_sched = (total_sched - future_sched) if total_sched else observed_sched
    if past_sched > 0 and total_actual > 0:
        drift = abs(total_actual - past_sched) / past_sched * 100
        if drift > 10:
            out["warnings"].append(
                f"Actual hours differ from scheduled-to-date by {drift:.0f}% "
                f"({total_actual:,} vs {past_sched:,}) — check missing or duplicate timesheets.")

    out["insufficient_data"] += [
        "approved_quote_hours: not in the lake (fact_quote_hours, contract v1.1) — "
        "call get_quote_hours(job_code) for the LIVE quoted hours from the Online "
        "Shutdown Quote tool and quote it as the quote baseline; do NOT substitute "
        "any other figure.",
        "submitted_vs_approved split: timesheet status not yet promoted to gold.",
        "invoiced_hours: blocked on Xero API access.",
    ]
    if future_sched > 0:
        out["recommended_actions"].append(
            f"{future_sched:,} h of future roster exist — fetch the quote baseline with "
            "get_quote_hours(job_code) before relying on a remaining-hours figure.")
        out["confidence"] = "medium"

    out["data_freshness"] = {
        "roster_max_date": str(sched["maxd"]),
        "timesheet_max_date": str(actual["maxd"]),
        "job_record_modified": str(job["modified_at"]),
        "lake_refresh": "nightly 02:00 AWST",
    }
    return out
