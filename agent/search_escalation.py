"""
search_escalation.py — the 0-row fallback chain. "Not found" is the LAST resort.

    Gold query -> 0 rows
      1. re-check parsed intent (tool/domain/dates/entity) + date-coverage sanity
      2. entity resolution (Acmegroup -> Acme Group / MG; spacing, casing, typos)
      3. retry the SAME Gold query with the resolved entity        -> success? done.
      4. probe RELATED Gold tables: does the entity exist at all?
           timesheet  -> roster_summary / site_assignment / project_job_summary
           roster     -> employee_profile / site_assignment
           compliance -> training_compliance / employee_profile
      5. Admin_IT only: RAW lookup in Silver-flat (Bronze mirror), labelled UNVALIDATED
      6. only then answer — with WHAT was searched, WHY it may be empty, and a
         suggested clarification. Never a bare "no records".

Steps 3/4 run through the SAME safety chain (validator -> DuckDB -> Gold).
Step 5 uses QueryTool.raw_debug_lookup (role-gated, template-only, audit-logged).
"""

from time_entity_parser import time_range_label

# which Gold tables can confirm an entity exists, per tool domain
RELATED_PROBES = {
    # tool/function family: [(table, ilike-able column, what it proves)]
    "timesheet": [("roster_summary", "project_name", "rostered on a project"),
                  ("site_assignment", "site_name", "assigned to the site"),
                  ("project_job_summary", "project_name", "a known project"),
                  ("project_job_summary", "client_name", "a known client")],
    "roster": [("employee_profile", "first_name || ' ' || last_name", "a known worker"),
               ("site_assignment", "site_name", "assigned to the site")],
    "training": [("training_compliance", "first_name || ' ' || last_name", "has training records"),
                 ("employee_profile", "first_name || ' ' || last_name", "a known worker")],
    "people": [("employee_profile", "first_name || ' ' || last_name", "a known worker"),
               ("site_assignment", "site_name", "assigned to the site")],
    "project": [("project_job_summary", "project_name", "a known project"),
                ("project_bridge", "job_code", "a bridged job code")],
}

# the date column of each primary table (for coverage sanity checks)
DATE_COVERAGE = {
    "get_weekly_timesheet": ("weekly_timesheet", "work_date"),
    "get_roster_summary": ("roster_summary", "roster_date"),
    "get_worker_hours": ("timesheet_summary", "month"),
    "get_timesheet_summary": ("timesheet_summary", "month"),
}

TEXT_FILTER_TYPES = {"site": ["site"], "project": ["project"], "client": ["client"],
                     "supplier": ["supplier"], "worker_name": ["person"], "name": ["person"],
                     "term": None}


def _intent_recheck(plan, qt, user_role):
    """Step 1 — restate what Lulu understood + date-coverage sanity."""
    notes = [f"intent: domain={plan.domain or '?'} tool={plan.tool}.{plan.function} args={plan.args}"]
    label = time_range_label(plan.args or {})
    if label:
        notes.append(f"time understood as: {label}")
    cov = DATE_COVERAGE.get(plan.function)
    if cov and (plan.args.get("date_from") or plan.args.get("date_to")):
        table, col = cov
        r = qt.run(f"SELECT MIN({col}) AS lo, MAX({col}) AS hi FROM {table} LIMIT 1", user_role)
        if r.ok and r.rows:
            lo, hi = r.rows[0]
            df, dt_ = plan.args.get("date_from"), plan.args.get("date_to")
            if lo and dt_ and str(dt_) < str(lo):
                notes.append(f"⚠ requested range ends {dt_} but {table} data starts {lo} — range is BEFORE coverage")
            if hi and df and str(df) > str(hi):
                notes.append(f"⚠ requested range starts {df} but {table} data ends {hi} — range is AFTER coverage")
    return notes


def _probe_related(plan, qt, user_role):
    """Step 4 — does the entity exist anywhere related? (validator-chained Gold queries)"""
    findings = []
    probes = RELATED_PROBES.get(plan.tool, [])
    terms = [v for k, v in (plan.args or {}).items()
             if k in TEXT_FILTER_TYPES and isinstance(v, str)]
    for term in terms:
        safe = term.replace("'", "''")
        for table, col, proves in probes:
            first = col.split("||")[0].strip()
            r = qt.run(f"SELECT COUNT(*) AS n FROM {table} WHERE {col} ILIKE '%{safe}%' LIMIT 1",
                       user_role)
            if r.ok and r.rows and r.rows[0][0]:
                findings.append(f"'{term}' IS {proves}: {r.rows[0][0]} row(s) in {table}")
            _ = first  # (kept for readability of col expressions)
    return findings


def escalate(plan, result, retry_fn, qt, user_role):
    """Run the full chain on a 0-row Gold result.
    Returns (result, notes): result may be a successful retry; notes feed plan_steps/answer."""
    from entity_resolver import resolve, suggest

    notes = ["search escalation: Gold returned 0 rows — running fallback chain"]
    notes += _intent_recheck(plan, qt, user_role)                       # step 1

    # steps 2+3 — entity resolution + retry
    candidates_msg = None
    for key, types in TEXT_FILTER_TYPES.items():
        raw = (plan.args or {}).get(key)
        if not raw or not isinstance(raw, str):
            continue
        res = resolve(raw, types=types)
        match = res.get("match")
        if match and match["value"].lower() != raw.lower():
            retry_args = dict(plan.args, **{key: match["value"]})
            try:
                r2 = retry_fn(**retry_args, user_role=user_role)
            except Exception:
                continue
            notes.append(f"entity resolution: '{raw}' → '{match['value']}' ({match['type']}, {res['status']})")
            if r2.ok and r2.row_count > 0:
                plan.args = retry_args
                r2.summary = (f"(没有 '{raw}' 的记录 — 实体归一为 '{match['value']}' 后重查) " + r2.summary)
                notes.append("retry with resolved entity: SUCCESS")
                return r2, notes
            notes.append("retry with resolved entity: still 0 rows")
            result = r2
        elif res.get("candidates"):
            opts = " / ".join(f"{c['value']} ({c['type']})" for c in res["candidates"][:3])
            candidates_msg = f"'{raw}' 没有精确匹配 — 可能是: {opts}"
            notes.append(f"entity candidates: {opts}")
        else:
            sug = suggest(raw, limit=3, types=types)
            if sug:
                opts = " / ".join(f"{c['value']} ({c['score']:.0f})" for c in sug)
                candidates_msg = f"'{raw}' 找不到相近实体 — 最接近: {opts}"
                notes.append(f"nearest entities: {opts}")

    related = _probe_related(plan, qt, user_role)                       # step 4
    notes += [f"related-table probe: {f}" for f in related] or ["related-table probe: entity not found in any related Gold table"]

    raw_section = ""                                                    # step 5 (Admin_IT only)
    terms = [v for k, v in (plan.args or {}).items() if k in TEXT_FILTER_TYPES and isinstance(v, str)]
    if user_role == "Admin_IT" and terms:
        rd = qt.raw_debug_lookup(terms[0], user_role=user_role)
        if rd["allowed"] and rd["hits"]:
            lines = "; ".join(f"{h['table']}.{h['column']}: {h['values'][:3]}" for h in rd["hits"][:4])
            raw_section = f" 🔧 RAW/UNVALIDATED (Admin debug, bronze镜像): {lines}. 仅供排查，不构成业务结论。"
            notes.append(f"RAW layer (Admin_IT): found in {[h['table'] for h in rd['hits']]}")
        elif rd["allowed"]:
            raw_section = " 🔧 RAW/UNVALIDATED (Admin debug): 原始层也没有该实体。"
            notes.append("RAW layer (Admin_IT): nothing found")

    # step 6 — diagnostic answer, never a bare "no records"
    searched = f"查询了 {plan.tool}.{plan.function}，条件 {plan.args}"
    reasons = []
    if any("BEFORE coverage" in n or "AFTER coverage" in n for n in notes):
        reasons.append("日期范围在数据覆盖之外")
    if related:
        reasons.append("实体存在于关联表中，但在该条件/日期下没有这类记录")
    elif candidates_msg:
        reasons.append("实体名称可能拼写不同")
    else:
        reasons.append("该实体在 Gold 层不存在")
    clarify = candidates_msg or "请确认实体名称和日期范围，或换一种说法。"

    result.summary = (f"{searched}：0 条记录。可能原因: {'；'.join(reasons)}。 "
                      + (" ".join(f"✔ {f}" for f in related) + " " if related else "")
                      + f"💡 {clarify}" + raw_section)
    result.confidence = "Medium"
    return result, notes