"""Smoke-test every business tool function through the safety chain. Run: python test_tools.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import build_tools

tools = build_tools()
CALLS = [
    # (tool, function, kwargs)
    ("people", "search_employee", {"name": "CARTER"}),
    ("people", "get_employee_profile", {"worker_id": 6}),
    ("people", "find_active_workers", {"position": "boilermaker"}),
    ("people", "find_inactive_workers", {}),
    ("people", "get_supplier_summary", {}),
    ("people", "get_worker_licences", {}),
    ("people", "get_worker_ranking", {"user_role": "HR_Manager"}),
    ("people", "get_worker_ranking", {}),                              # default role -> should be BLOCKED
    ("training", "find_expired_tickets", {"count_only": True}),
    ("training", "find_expiring_tickets", {"days": 30}),
    ("training", "check_worker_compliance", {"worker_id": 6, "competency": "Working at Heights"}),
    ("training", "find_not_eligible_workers", {}),
    ("training", "expiry_forecast", {"months": 6}),
    ("training", "compliance_by_group", {}),
    ("roster", "get_roster_summary", {"period": "2026-06"}),
    ("roster", "find_roster_gaps", {"days": 90}),
    ("roster", "check_roster_risk", {"days_back": 30}),
    ("timesheet", "get_weekly_timesheet", {"date_from": "2026-06-01", "date_to": "2026-06-07"}),
    ("timesheet", "get_weekly_timesheet", {"worker_name": "CARTER", "date_from": "2026-06-01"}),
    ("timesheet", "get_worker_hours", {"worker_id": 4, "month": "2024-04"}),
    ("timesheet", "get_site_hours", {"year": "2024"}),
    ("timesheet", "get_project_hours", {}),
    ("timesheet", "get_timesheet_summary", {"by": "month"}),
    ("timesheet", "top_workers_by_hours", {"top_n": 5}),
    ("project", "get_active_projects", {}),
    ("project", "get_project_jobs", {"client": "Ironstone"}),
    ("project", "get_job_detail", {"client": "ChemCo", "active_only": True}),
    ("project", "get_site_assignments", {"site": "TESTSITE"}),
    ("inventory_asset", "search_assets", {"term": "generator"}),
    ("inventory_asset", "assets_by_status", {}),
    ("inventory_asset", "get_inventory_summary", {}),
    ("inventory_asset", "find_low_stock", {"threshold": 5}),
    ("inventory_asset", "get_ppe_signouts", {}),
    ("inventory_asset", "get_ppe_signouts", {"person": "BELL", "location": "Perth"}),
    ("inventory_asset", "get_ppe_monthly_usage", {"months": 6}),
    ("inventory_asset", "hardware_stock", {}),
    ("finance", "get_purchase_summary", {}),                            # default -> counts only
    ("finance", "get_purchase_summary", {"user_role": "Finance"}),      # Finance -> spend
    ("finance", "get_rate_card", {"user_role": "Finance"}),
    ("finance", "get_rate_card", {}),                                   # default -> titles only
    ("hseq", "get_hseq_register", {"overdue_only": True}),
    ("hseq", "get_audit_issues", {"limit": 50}),
    ("hseq", "audit_event_breakdown", {}),
    ("insight", "find_deployable_workers", {}),
    ("insight", "site_compliance_report", {"site": "TESTSITE"}),
    ("insight", "supplier_compliance_risk", {}),
    ("insight", "worker_360", {"worker_id": 6}),
]

ok = blocked = fail = 0
for tname, fname, kwargs in CALLS:
    fn = getattr(tools[tname], fname)
    try:
        r = fn(**kwargs)
        if r.ok:
            ok += 1
            print(f"  [OK   ] {tname}.{fname}{'' if 'user_role' not in kwargs else ' ('+kwargs['user_role']+')'} "
                  f"-> {r.row_count} rows | {r.confidence} | {r.summary[:90]}")
        else:
            blocked += 1
            print(f"  [BLOCK] {tname}.{fname} -> {r.validator_errors[:1]}")
    except Exception as e:
        fail += 1
        print(f"  [FAIL ] {tname}.{fname} -> {type(e).__name__}: {e}")

print(f"\n{ok} executed OK, {blocked} correctly blocked by validator, {fail} crashed (want 0)")
