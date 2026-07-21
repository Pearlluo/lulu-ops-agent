"""Cross-domain intelligence — the 'agent capability' layer. Combines Gold tables
(via the approved opms_employee_id join key) to make judgements, not just lookups.
Every query still goes through the same safety chain (validator -> DuckDB -> Gold)."""

from ._base import BaseTool, ToolResult


# the admin's Plan B (2026-06-11): deployable = FIELD workers only.
# Counted: Driver, Operator, Scaffolder, Boilermaker, Trade Assistant, Rigger.
# Excluded: Finance / Admin / Planner / Management (office staff) and unrecorded positions.
FIELD_POSITIONS = ["driver", "operator", "scaffold", "boilermaker", "trade assistant", "rigger"]


class InsightTool(BaseTool):
    name = "insight"

    def resolve_entity(self, term, user_role="default"):
        """Search Layer (实体归一): map a fuzzy human name ('Acmegroup', 'MG', 'Carter')
        to the canonical Gold entity. No SQL — matches against the live Gold vocabulary."""
        from entity_resolver import resolve, suggest
        res = resolve(term)
        tr = ToolResult(tool=self.name, function="resolve_entity", args={"term": term}, ok=True)
        if res["match"]:
            m = res["match"]
            tr.data = [m] + res.get("candidates", [])
            tr.row_count = len(tr.data)
            tr.summary = (f"'{term}' resolved to {m['type']} '{m['value']}' "
                          f"({res['status']}, score {m['score']}). Use this exact value as the filter.")
            tr.confidence = "High"
        elif res["candidates"]:
            tr.data = res["candidates"]
            tr.row_count = len(tr.data)
            opts = " / ".join(f"{c['value']} ({c['type']})" for c in res["candidates"][:4])
            tr.summary = f"'{term}' is ambiguous — candidates: {opts}. Ask the user which one they mean."
            tr.confidence = "Medium"
        else:
            sug = suggest(term, limit=4)
            tr.data = sug
            tr.row_count = len(sug)
            tr.summary = (f"No entity matches '{term}'."
                          + (" Closest: " + " / ".join(f"{c['value']} ({c['type']})" for c in sug)
                             if sug else " Nothing similar exists in Gold."))
            tr.confidence = "Medium" if sug else "Low"
        return tr

    def find_deployable_workers(self, user_role="default"):
        """Plan B: FIELD worker + active + zero expired certs + not currently/future rostered."""
        pos = " OR ".join(f"e.position_name ILIKE '%{p}%'" for p in FIELD_POSITIONS)
        sql = ("SELECT e.opms_employee_id, e.first_name, e.last_name, e.position_name, e.supplier_name "
               "FROM employee_profile e WHERE e.is_active = true "
               f"AND ({pos}) "
               "AND e.opms_employee_id NOT IN (SELECT t.opms_employee_id FROM training_compliance t "
               "WHERE t.is_expired = true AND t.opms_employee_id IS NOT NULL) "
               "AND e.opms_employee_id NOT IN (SELECT r.opms_employee_id FROM roster_summary r "
               "WHERE r.opms_employee_id IS NOT NULL AND CAST(r.roster_date AS DATE) >= CURRENT_DATE)")
        return self._query("find_deployable_workers", {}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} FIELD workers are DEPLOYABLE now "
                           "(driver/operator/scaffolder/boilermaker/trade assistant/rigger; active, "
                           "fully compliant, not rostered). Office roles (finance/admin/planner/management) "
                           "and unrecorded positions are excluded per the Plan-B definition.")

    def site_compliance_report(self, site, user_role="default"):
        """Crew of a site joined to their expired certs — site-level deployment risk."""
        s = self.esc(site)
        sql = ("SELECT s.first_name, s.last_name, s.position_name, COUNT(*) AS expired_certs "
               "FROM site_assignment s JOIN training_compliance t "
               "ON s.opms_employee_id = t.opms_employee_id "
               f"WHERE s.site_name ILIKE '%{s}%' AND t.is_expired = true "
               "GROUP BY s.first_name, s.last_name, s.position_name ORDER BY expired_certs DESC")
        return self._query("site_compliance_report", {"site": site}, sql, user_role,
                           summarise=lambda tr: f"Site '{site}': {tr.row_count} crew member(s) hold expired certs"
                           + (f"; worst {tr.data[0]['first_name']} {tr.data[0]['last_name']} "
                              f"({tr.data[0]['expired_certs']} expired)." if tr.data else " — crew fully compliant."))

    def supplier_compliance_risk(self, user_role="default"):
        """Expired certs aggregated per labour supplier — who supplies non-compliant workers."""
        sql = ("SELECT e.supplier_name, COUNT(DISTINCT e.opms_employee_id) AS workers_with_expired, "
               "COUNT(*) AS expired_certs "
               "FROM employee_profile e JOIN training_compliance t "
               "ON e.opms_employee_id = t.opms_employee_id "
               "WHERE t.is_expired = true AND e.supplier_name IS NOT NULL "
               "GROUP BY e.supplier_name ORDER BY expired_certs DESC")
        return self._query("supplier_compliance_risk", {}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} suppliers have workers with expired certs"
                           + (f". Highest risk: {tr.data[0]['supplier_name']} "
                              f"({tr.data[0]['workers_with_expired']} workers, "
                              f"{tr.data[0]['expired_certs']} expired certs)." if tr.data else "."))

    def worker_360(self, worker_id, user_role="default"):
        """Everything about one worker: profile + cert status + recent roster + hours + licences.
        Multiple controlled queries (each validated) merged into one structured answer."""
        wid = int(worker_id)
        parts, caveats, sqls = {}, [], []

        def run(label, sql):
            r = self._query(f"worker_360.{label}", {"worker_id": wid}, sql, user_role)
            caveats.extend(c for c in r.caveats if c not in caveats)
            sqls.append(r.sql)
            return r.data if r.ok else []

        parts["profile"] = run("profile",
            "SELECT first_name, last_name, position_name, supplier_name, is_active, email_work "
            f"FROM employee_profile WHERE opms_employee_id = {wid}")
        parts["cert_status"] = run("certs",
            "SELECT SUM(CASE WHEN is_expired THEN 1 ELSE 0 END) AS expired, "
            "SUM(CASE WHEN NOT is_expired THEN 1 ELSE 0 END) AS valid, "
            "SUM(CASE WHEN is_expiring_soon THEN 1 ELSE 0 END) AS expiring_90d "
            f"FROM training_compliance WHERE opms_employee_id = {wid}")
        parts["recent_roster"] = run("roster",
            "SELECT roster_date, project_name, position_name FROM roster_summary "
            f"WHERE opms_employee_id = {wid} ORDER BY roster_date DESC LIMIT 5")
        parts["hours"] = run("hours",
            "SELECT SUM(total_hours) AS total_hours FROM timesheet_summary "
            f"WHERE opms_employee_id = {wid}")
        parts["licences"] = run("licences",
            f"SELECT licence FROM licence_register WHERE opms_employee_id = {wid}")

        tr = ToolResult(tool=self.name, function="worker_360", args={"worker_id": wid})
        tr.ok = True
        tr.data = [parts]
        tr.row_count = 1 if parts["profile"] else 0
        tr.caveats = caveats
        tr.sql = " ;; ".join(sqls)
        if not parts["profile"]:
            tr.summary = f"No worker found with id {wid}."
            tr.confidence = "Medium"
            return tr
        p = parts["profile"][0]
        c = parts["cert_status"][0] if parts["cert_status"] else {}
        hrs = parts["hours"][0]["total_hours"] if parts["hours"] else None
        tr.summary = (f"{p['first_name']} {p['last_name']} — {p['position_name'] or 'position n/a'}, "
                      f"supplier {p['supplier_name'] or 'n/a'}, active={p['is_active']}. "
                      f"Certs: {int(c.get('valid') or 0)} valid / {int(c.get('expired') or 0)} expired / "
                      f"{int(c.get('expiring_90d') or 0)} expiring within 90d. "
                      f"Total recorded hours: {hrs or 0:,.0f}. "
                      f"Licences: {len(parts['licences'])}. Recent roster entries: {len(parts['recent_roster'])}.")
        tr.confidence = "High"
        return tr
