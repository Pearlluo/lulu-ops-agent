"""
Claude tool-use schemas for LuluAgent's 10 business tools (9 data tools + the GitHub
automation-estate knowledge tool).

Generated from a spec table so names/types stay in sync with tools/*.py.
Deliberately does NOT expose: SQL, DuckDB, parquet paths, or the lake layout —
Claude only sees business capabilities and parameters. Role gating is described
in each tool's description; enforcement happens server-side in sql_validator
(Claude can never choose the role — the caller supplies it).
"""

# (tool_key, function, description, {param: (json_type, required, description, default)})
SPEC = [
    # ---------------- People ----------------
    ("people", "search_employee",
     "Search for workers by (partial) name. Returns id, name, position, supplier, active status. "
     "Use this first when the user names a person and you need their worker id.",
     {"name": ("string", True, "Full or partial worker name, e.g. 'CARTER'", None)}),
    ("people", "get_employee_profile",
     "Get one worker's profile: position, company, supplier, ops section, arrangement, work contact, active status. "
     "Provide worker_id OR name.",
     {"worker_id": ("integer", False, "OPMS worker id", None),
      "name": ("string", False, "Worker name (used if no id)", None)}),
    ("people", "find_active_workers",
     "List active workers, optionally filtered by position keyword. NOTE: active flag only covers the ~446 BMS-tracked workers of 2,065 total.",
     {"position": ("string", False, "Position keyword, e.g. 'boilermaker'", None)}),
    ("people", "find_inactive_workers",
     "List inactive/terminated workers (BMS-tracked).", {}),
    ("people", "get_supplier_summary",
     "List labour suppliers with how many workers each supplies, plus contact details.",
     {"active_only": ("boolean", False, "Only active suppliers", True)}),
    ("people", "get_worker_licences",
     "List licences held by workers (optionally one worker).",
     {"worker_id": ("integer", False, "OPMS worker id", None),
      "name": ("string", False, "Worker name", None)}),
    ("people", "get_worker_ranking",
     "Top workers by mobilisation ranking score. RESTRICTED: scores require the HR_Manager role; "
     "for other roles this call will be refused by the security layer.",
     {"top_n": ("integer", False, "How many top workers", 10)}),

    # ---------------- Training / compliance ----------------
    ("training", "find_expired_tickets",
     "Find expired certificates/tickets across the workforce. Set count_only=true for just the total number.",
     {"count_only": ("boolean", False, "Return only the count", False)}),
    ("training", "find_expiring_tickets",
     "Find certificates expiring within N days (default 30), most urgent first. Use for '快到期/expiring soon' questions.",
     {"days": ("integer", False, "Window in days", 30)}),
    ("training", "check_worker_compliance",
     "Verdict on whether a worker is compliant for a competency (COMPLIANT iff a non-expired matching cert exists). "
     "Use for 'can X work / is X eligible' questions. Provide worker_id or worker_name, plus optionally the competency.",
     {"worker_id": ("integer", False, "OPMS worker id", None),
      "worker_name": ("string", False, "Worker name", None),
      "competency": ("string", False, "Competency/cert keyword, e.g. 'Working at Heights'", None)}),
    ("training", "find_not_eligible_workers",
     "Workers who CANNOT be deployed because they hold expired certs, grouped per worker, worst first. "
     "Use for '不能上岗/cannot work' questions.", {}),
    ("training", "expiry_forecast",
     "Forecast: how many certs expire in each of the next N months.",
     {"months": ("integer", False, "Months ahead", 6)}),
    ("training", "compliance_by_group",
     "Expired-vs-total cert counts per competency group (worst groups first).", {}),

    # ---------------- Roster ----------------
    ("roster", "get_roster_summary",
     "Who is rostered, filtered by period / date range / project / worker. For relative dates "
     "('last week', '上个礼拜', 'tomorrow') resolve them to YYYY-MM-DD using today's date from the context.",
     {"period": ("string", False, "Period prefix 'YYYY-MM' or 'YYYY'", None),
      "date_from": ("string", False, "Range start 'YYYY-MM-DD' (inclusive)", None),
      "date_to": ("string", False, "Range end 'YYYY-MM-DD' (inclusive)", None),
      "project": ("string", False, "Project name keyword", None),
      "worker_id": ("integer", False, "OPMS worker id", None)}),
    ("roster", "find_roster_gaps",
     "Active workers with NO roster entries in the last N days — the idle bench/availability list.",
     {"days": ("integer", False, "Lookback days", 90)}),
    ("roster", "check_roster_risk",
     "CROSS-CHECK: workers rostered in the last N days who hold EXPIRED certs = deployment compliance risk.",
     {"days_back": ("integer", False, "Roster lookback days", 30),
      "project": ("string", False, "Limit to one project", None)}),

    # ---------------- Timesheets / hours ----------------
    ("timesheet", "get_weekly_timesheet",
     "ACTUAL worked hours — the weekly timesheet (OPMS actual hours matched onto the roster, minus "
     "sign-out/sign-in gap deductions; plant lines excluded). PREFER THIS over get_roster_summary for any "
     "'timesheet for <week/date range>' question ('上周的timesheet'). Resolve relative dates to YYYY-MM-DD first. "
     "IMPORTANT: for 'everyone's / all workers' timesheet questions set group_by='matrix' — the company's "
     "weekly-report layout (one row per person, Mon..Sun DS/NS day columns, totals). Its summary already "
     "contains a ready markdown table: REPRODUCE THAT TABLE VERBATIM in your answer, do not reformat. "
     "group_by='worker' = totals only; omit group_by only for one person's daily detail.",
     {"date_from": ("string", False, "Range start 'YYYY-MM-DD' (inclusive)", None),
      "date_to": ("string", False, "Range end 'YYYY-MM-DD' (inclusive)", None),
      "worker_id": ("integer", False, "OPMS worker id", None),
      "worker_name": ("string", False, "Worker name keyword", None),
      "project": ("string", False, "Project name keyword", None),
      "site": ("string", False, "Site name keyword", None),
      "group_by": ("string", False, "'worker' = one row per person (weekly totals); 'day' = daily totals; "
                                    "omit = day-entry detail", None)}),
    ("timesheet", "get_worker_hours",
     "Total hours worked by one worker (timesheets), optionally for one month/year.",
     {"worker_id": ("integer", False, "OPMS worker id", None),
      "worker_name": ("string", False, "Worker name", None),
      "month": ("string", False, "Month 'YYYY-MM'", None),
      "year": ("string", False, "Year 'YYYY'", None)}),
    ("timesheet", "get_site_hours",
     "Hours worked per site (timesheets), optionally for one year.",
     {"site": ("string", False, "Site name keyword", None),
      "year": ("string", False, "Year 'YYYY'", None)}),
    ("timesheet", "get_project_hours",
     "Approximate hours per project (from rostered hours — timesheets track sites, not projects).",
     {"project": ("string", False, "Project name keyword", None)}),
    ("timesheet", "get_timesheet_summary",
     "Hours aggregated by site or by month.",
     {"year": ("string", False, "Year 'YYYY'", None),
      "by": ("string", False, "Group by 'site' or 'month'", "site")}),
    ("timesheet", "top_workers_by_hours",
     "Workers ranked by total hours worked. Use for 'who worked the most hours'.",
     {"year": ("string", False, "Year 'YYYY' (omit for all time)", None),
      "top_n": ("integer", False, "How many", 10)}),

    # ---------------- Projects / jobs ----------------
    ("project", "resolve_project_client",
     "OPMS<->BMS project/client bridge: given a job code ('SH-25006'), project name, or client keyword, "
     "returns which client a project/job belongs to and the BMS project it maps to (job-code-prefix matching "
     "logic from the rates pipeline). Use for 'which client is project X / 这个项目是哪个客户的' questions.",
     {"term": ("string", True, "Job code, project name, or client keyword", None)}),
    ("project", "get_active_projects",
     "List active projects with client and job counts, optionally for one client.",
     {"client": ("string", False, "Client name keyword", None)}),
    ("project", "get_project_jobs",
     "Job counts (total/active) per project, filtered by client or project.",
     {"client": ("string", False, "Client name keyword", None),
      "project": ("string", False, "Project name keyword", None)}),
    ("project", "get_job_detail",
     "Look up jobs: code, title, status, project, client, work location, lead.",
     {"job_code": ("string", False, "Job code or title keyword", None),
      "client": ("string", False, "Client name keyword", None),
      "active_only": ("boolean", False, "Only active jobs", False)}),
    ("project", "get_site_assignments",
     "Who is assigned to which site (crew lists). Filter by site and/or worker.",
     {"site": ("string", False, "Site name keyword", None),
      "worker": ("string", False, "Worker name keyword", None)}),

    # ---------------- Inventory / assets ----------------
    ("inventory_asset", "search_assets",
     "Search plant/equipment assets by name/id/model, optionally by status.",
     {"term": ("string", False, "Search keyword", None),
      "status": ("string", False, "Status filter, e.g. 'Operational'", None)}),
    ("inventory_asset", "assets_by_status",
     "Count of assets per status (Operational / Missing / Out of Service / Disposed).", {}),
    ("inventory_asset", "get_inventory_summary",
     "Stock on hand by item/location/category. Filter by item or location keyword.",
     {"item": ("string", False, "Item keyword, e.g. 'boot'", None),
      "location": ("string", False, "Location keyword", None)}),
    ("inventory_asset", "find_low_stock",
     "Items at or below a stock threshold (includes out-of-stock). Use for low-stock/PPE-restock questions.",
     {"threshold": ("integer", False, "Stock threshold", 5)}),
    ("inventory_asset", "get_ppe_signouts",
     "PPE/workwear sign-out ledger: who took what, from which store, for which job/project. "
     "Use for 'what PPE did <person> sign out / 领用' questions. Newest first, max 100 lines.",
     {"person": ("string", False, "Person name keyword (names stored SURNAME FIRSTNAME)", None),
      "item": ("string", False, "Item name or code keyword", None),
      "location": ("string", False, "Store location keyword, e.g. 'Perth'", None),
      "job": ("string", False, "Job code or project name keyword", None),
      "month": ("string", False, "Month 'YYYY-MM'", None)}),
    ("inventory_asset", "get_ppe_monthly_usage",
     "Monthly PPE sign-out totals (units, lines, distinct people), optionally filtered by "
     "item/store/project. Use for PPE usage trends / 每月PPE用量 questions.",
     {"item": ("string", False, "Item name or code keyword", None),
      "location": ("string", False, "Store location keyword", None),
      "project": ("string", False, "Project name keyword", None),
      "months": ("integer", False, "How many recent months", 6)}),
    ("inventory_asset", "hardware_stock",
     "IT/hardware items with stock counts, optionally filtered by name/code.",
     {"term": ("string", False, "Hardware keyword", None)}),

    # ---------------- Finance (role-gated) ----------------
    ("finance", "get_purchase_summary",
     "Purchases per supplier. RESTRICTED: invoice amounts/total spend require the Finance role — "
     "for other roles this returns counts and dates only and says so.",
     {"supplier": ("string", False, "Supplier name keyword", None)}),
    ("finance", "get_client_revenue",
     "Revenue invoiced per client (Xero sales invoices). RESTRICTED: amounts require the Finance role — "
     "other roles get invoice counts only. Use for '哪个客户收入最高 / how much have we billed <client>'. "
     "NOTE: Xero data currently ends ~April 2026 — always mention this caveat.",
     {"client": ("string", False, "Client name keyword, e.g. 'CementCo'", None),
      "year": ("string", False, "Year 'YYYY' or month 'YYYY-MM' prefix", None)}),
    ("finance", "get_outstanding_invoices",
     "Unpaid client invoices (accounts receivable), ordered by due date. RESTRICTED: amounts need Finance role.",
     {"client": ("string", False, "Client name keyword", None)}),
    ("finance", "get_project_revenue",
     "Revenue invoiced against one JOB CODE (mined from invoice references, e.g. 'SH-26036'). "
     "Combine with get_weekly_timesheet hours + rates for project profitability. Finance role for amounts.",
     {"job_code": ("string", True, "Job code, e.g. 'SH-26036'", None)}),
    ("finance", "get_rate_card",
     "Charge rate cards by project/position. RESTRICTED: day/night rate values require the Finance role — "
     "other roles see only which rate lines exist.",
     {"project": ("string", False, "Project keyword", None),
      "position": ("string", False, "Position/rate title keyword", None)}),

    # ---------------- HSEQ / audit ----------------
    ("hseq", "get_hseq_register",
     "Safety issues & corrective actions, with open/overdue flags and priority.",
     {"open_only": ("boolean", False, "Only open actions", False),
      "overdue_only": ("boolean", False, "Only overdue actions", False),
      "priority": ("string", False, "Priority filter, e.g. 'High'", None)}),
    ("hseq", "get_audit_issues",
     "Change/audit events on employee records (most recent first). History starts 2025-01-08.",
     {"worker_id": ("integer", False, "Filter to one worker", None),
      "event_type": ("string", False, "Event type keyword", None),
      "limit": ("integer", False, "Max events", 100)}),
    ("hseq", "audit_event_breakdown",
     "Count of audit events per event type.", {}),

    # ---------------- Cross-domain intelligence ----------------
    ("insight", "resolve_entity",
     "SEARCH LAYER (实体归一): map a fuzzy/misspelled/abbreviated name ('Acmegroup', 'MG', 'Carter', "
     "'Acme Grup') to the canonical site/project/client/supplier/company/person name in Gold. "
     "ALWAYS call this when a filtered query returns 0 rows (the name is probably wrong, not the data), "
     "or BEFORE filtering when the user's wording doesn't look like an exact system name. "
     "If it returns one match, retry your query with that exact value; if ambiguous, ask the user.",
     {"term": ("string", True, "The name as the user said it, e.g. 'Acmegroup'", None)}),
    ("insight", "find_deployable_workers",
     "Workers DEPLOYABLE right now: active + zero expired certs + not currently/future rostered.", {}),
    ("insight", "site_compliance_report",
     "Site readiness check: the site's crew joined to their expired certs. Use for 'is site X ready' questions "
     "(combine with find_deployable_workers for the replacement pool).",
     {"site": ("string", True, "Site name keyword", None)}),
    ("insight", "supplier_compliance_risk",
     "Which labour suppliers concentrate compliance risk (workers with expired certs per supplier).", {}),
    ("insight", "worker_360",
     "Full 360 view of one worker: profile + cert status + recent roster + total hours + licences, in one call.",
     {"worker_id": ("integer", True, "OPMS worker id (use search_employee first if you only have a name)", None)}),

    # ---------------- Automation estate (GitHub workflows / internal systems) ----------------
    ("automation", "list_automations",
     "List every GitHub automation/workflow project in the Acme estate (timesheet automation, rates updater, "
     "resume sync, quote tool, rating system, payroll queries…). Use for 'what automations/systems do we have'.",
     {"category": ("string", False, "Filter by category keyword, e.g. 'Automation', 'AI', 'Web'", None)}),
    ("automation", "get_automation_detail",
     "Full card for ONE automation project: business purpose, tech stack, deployment, GitHub Actions workflows "
     "(triggers + Azure target app), related systems, latest deploy result, AND its exact internal LOGIC "
     "(matching keys, formulas, business rules extracted from source). Use when the user names a system or asks "
     "how a system works/matches data.",
     {"name": ("string", True, "Automation/repo/system name or keyword, e.g. 'timesheet automation', 'quote'", None)}),
    ("automation", "find_automation",
     "Which system/automation handles X? Searches purposes, keywords, related systems and APIs. "
     "Use for 'which system updates rates / 哪个系统管费率' style questions.",
     {"keyword": ("string", True, "Topic keyword, e.g. 'rates', 'gap hours', 'resume', '报价'", None)}),
    ("automation", "get_automation_runs",
     "Latest GitHub Actions workflow runs — did the automation deploy/run successfully? Live from GitHub when "
     "possible, otherwise cached from the last sync. Omit name to health-check ALL automations at once.",
     {"name": ("string", False, "One automation name (omit for all)", None),
      "limit": ("integer", False, "Runs per repo when one automation is named", 3)}),
    # ---------------- Files (BMS document libraries index) ----------------
    ("files", "find_files",
     "Find FILES stored in the BMS / IMS / FDS SharePoint document libraries by filename/folder keywords (logos, forms, "
     "templates, policies, controlled documents...). Multi-word keywords AND together, e.g. 'veteran logo'. "
     "Returns name, library, folder path, modified date and the direct web link. Names/paths only, not contents.",
     {"keyword": ("string", True, "Filename/folder keywords, e.g. 'veteran employment supporter logo'", None),
      "library": ("string", False, "Limit to one library, e.g. 'Corporate', 'Commercial', 'Operations'", None),
      "ext": ("string", False, "File extension filter, e.g. 'png', 'pdf', 'docx'", None),
      "site": ("string", False, "Limit to one SharePoint site: 'BMS', 'IMS' (job folders) or 'FDS' (forms)", None)}),
    ("files", "list_folder",
     "List everything inside one folder of the BMS / IMS / FDS document libraries (newest first). Use after find_files "
     "when the user wants the whole folder, e.g. 'what else is in the Logos folder?'.",
     {"folder": ("string", True, "Folder path keyword, e.g. 'Logos', 'SH-26044'", None),
      "library": ("string", False, "Limit to one library", None),
      "site": ("string", False, "Limit to one SharePoint site: BMS / IMS / FDS", None)}),

    # ---------------- FDS (Form Distribution System) lists ----------------
    ("fds", "list_fds_lists",
     "Catalogue of the FDS (Form Distribution System) SharePoint lists mirrored as queryable tables: "
     "workers, jobs, daily sheets, form distribution rules, assets, SRC requests, store items... "
     "Use FIRST when a question mentions FDS or you're unsure which FDS list holds the answer.", {}),
    ("fds", "search_fds",
     "Rows from ONE FDS list (form-distribution data), optionally keyword-filtered across its text fields. "
     "e.g. search_fds('dailysheets'), search_fds('jobs', keyword='SH-26044'), search_fds('formdistribute').",
     {"list_name": ("string", True, "FDS list name, e.g. 'workers', 'jobs', 'dailysheets', 'formdistribute'", None),
      "keyword": ("string", False, "Filter keyword matched across the list's text fields", None),
      "limit": ("integer", False, "Max rows (default 50, cap 200)", 50)}),
]


def build_tool_definitions():
    """Claude tool-use schema list (input_schema JSON Schema per tool)."""
    defs = []
    for _tool, fn, desc, params in SPEC:
        props, required = {}, []
        for pname, (ptype, preq, pdesc, pdefault) in params.items():
            p = {"type": ptype, "description": pdesc + (f" (default: {pdefault})" if pdefault is not None else "")}
            props[pname] = p
            if preq:
                required.append(pname)
        schema = {"type": "object", "properties": props, "additionalProperties": False}
        if required:
            schema["required"] = required
        defs.append({"name": fn, "description": desc, "input_schema": schema})
    return defs


def build_dispatch():
    """{function_name: tool_key} for routing Claude's tool_use blocks to tool instances."""
    return {fn: tool for tool, fn, _d, _p in SPEC}


TOOL_DEFINITIONS = build_tool_definitions()
DISPATCH = build_dispatch()

if __name__ == "__main__":
    import json
    print(f"{len(TOOL_DEFINITIONS)} tool definitions")
    names = [t["name"] for t in TOOL_DEFINITIONS]
    assert len(names) == len(set(names)), "duplicate tool names!"
    print(json.dumps(TOOL_DEFINITIONS[0], indent=2))
