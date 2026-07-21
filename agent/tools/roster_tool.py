"""Roster domain — roster_summary (+ cross-table risk check via training_compliance)."""

from ._base import BaseTool


class RosterTool(BaseTool):
    name = "roster"

    def get_roster_summary(self, period=None, project=None, worker_id=None,
                           date_from=None, date_to=None, user_role="default"):
        w = []
        if period:
            w.append(f"roster_date LIKE '{self.esc(period)}%'")
        if date_from:
            w.append(f"roster_date >= '{self.esc(date_from)}'")
        if date_to:
            w.append(f"roster_date <= '{self.esc(date_to)}'")
        if project:
            w.append(f"project_name ILIKE '%{self.esc(project)}%'")
        if worker_id is not None:
            w.append(f"opms_employee_id = {int(worker_id)}")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = ("SELECT DISTINCT first_name, last_name, roster_date, project_name, position_name "
               f"FROM roster_summary{where} ORDER BY roster_date")
        span = period or (f"{date_from}..{date_to}" if date_from or date_to else "")
        return self._query("get_roster_summary",
                           {"period": period, "project": project, "worker_id": worker_id,
                            "date_from": date_from, "date_to": date_to},
                           sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} roster entries"
                           + (f" in {span}" if span else "") + (f" on '{project}'" if project else "") + ".")

    def find_roster_gaps(self, days=90, user_role="default"):
        sql = ("SELECT e.opms_employee_id, e.first_name, e.last_name, e.position_name "
               "FROM employee_profile e WHERE e.is_active = true AND e.opms_employee_id NOT IN ("
               "SELECT r.opms_employee_id FROM roster_summary r WHERE r.opms_employee_id IS NOT NULL "
               f"AND CAST(r.roster_date AS DATE) >= CURRENT_DATE - INTERVAL '{int(days)}' DAY)")
        return self._query("find_roster_gaps", {"days": days}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} active workers have NO roster in the last {days} days (bench/availability list).")

    def check_roster_risk(self, days_back=30, project=None, user_role="default"):
        """Cross-table intelligence: recently/currently rostered workers who hold EXPIRED certs."""
        w = [f"CAST(r.roster_date AS DATE) >= CURRENT_DATE - INTERVAL '{int(days_back)}' DAY",
             "t.is_expired = true"]
        if project:
            w.append(f"r.project_name ILIKE '%{self.esc(project)}%'")
        sql = ("SELECT DISTINCT r.first_name, r.last_name, r.project_name, t.competency_name, t.expiry_date "
               "FROM roster_summary r JOIN training_compliance t "
               "ON r.opms_employee_id = t.opms_employee_id "
               "WHERE " + " AND ".join(w) + " ORDER BY r.last_name")
        return self._query("check_roster_risk", {"days_back": days_back, "project": project}, sql, user_role,
                           summarise=lambda tr: f"RISK: {tr.row_count} rostered-worker/expired-cert combinations "
                           f"in the last {days_back} days" + (f" on '{project}'" if project else "")
                           + (f". e.g. {tr.data[0]['first_name']} {tr.data[0]['last_name']} rostered with expired "
                              f"'{tr.data[0]['competency_name']}'." if tr.data else " — no risk found."))
