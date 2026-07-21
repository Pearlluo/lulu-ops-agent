"""People domain — employee_profile / supplier_summary / licence_register / worker_ranking."""

from ._base import BaseTool


class PeopleTool(BaseTool):
    name = "people"

    def search_employee(self, name, user_role="default"):
        s = self.esc(name)
        sql = ("SELECT opms_employee_id, first_name, last_name, position_name, supplier_name, is_active "
               "FROM employee_profile "
               f"WHERE (first_name || ' ' || last_name) ILIKE '%{s}%'")
        return self._query("search_employee", {"name": name}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} worker(s) match '{name}'."
                           + (f" Best: {tr.data[0]['first_name']} {tr.data[0]['last_name']} (id {tr.data[0]['opms_employee_id']})." if tr.data else ""))

    def get_employee_profile(self, worker_id=None, name=None, user_role="default"):
        if worker_id is not None:
            w = f"opms_employee_id = {int(worker_id)}"
        elif name:
            w = f"(first_name || ' ' || last_name) ILIKE '%{self.esc(name)}%'"
        else:
            w = "1=0"
        sql = ("SELECT opms_employee_id, person_id, first_name, last_name, preferred_name, position_name, "
               "company_name, supplier_name, ops_section_name, arrangement_type, email_work, phone_work, "
               "home_airport_code, is_active FROM employee_profile WHERE " + w)
        return self._query("get_employee_profile", {"worker_id": worker_id, "name": name}, sql, user_role,
                           summarise=lambda tr: (f"{tr.data[0]['first_name']} {tr.data[0]['last_name']} — "
                                                 f"{tr.data[0]['position_name'] or 'position not tracked in BMS'}, "
                                                 f"supplier {tr.data[0]['supplier_name'] or 'n/a'}, active={tr.data[0]['is_active']}.") if tr.data else "No such worker.")

    def find_active_workers(self, position=None, user_role="default"):
        w = "is_active = true"
        if position:
            w += f" AND position_name ILIKE '%{self.esc(position)}%'"
        sql = ("SELECT opms_employee_id, first_name, last_name, position_name, supplier_name "
               f"FROM employee_profile WHERE {w}")
        return self._query("find_active_workers", {"position": position}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} active workers" + (f" matching position '{position}'." if position else "."))

    def find_inactive_workers(self, user_role="default"):
        sql = ("SELECT opms_employee_id, first_name, last_name, position_name, supplier_name "
               "FROM employee_profile WHERE is_active = false")
        return self._query("find_inactive_workers", {}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} inactive/terminated workers (BMS-tracked).")

    def get_supplier_summary(self, active_only=True, user_role="default"):
        w = " WHERE is_active = true" if active_only else ""
        sql = f"SELECT supplier_name, worker_count, phone, email FROM supplier_summary{w} ORDER BY worker_count DESC"
        return self._query("get_supplier_summary", {"active_only": active_only}, sql, user_role,
                           summarise=lambda tr: (f"{tr.row_count} suppliers. Top: {tr.data[0]['supplier_name']} "
                                                 f"({tr.data[0]['worker_count']} workers).") if tr.data else "No suppliers.")

    def get_worker_licences(self, worker_id=None, name=None, user_role="default"):
        if worker_id is not None:
            w = f"opms_employee_id = {int(worker_id)}"
        elif name:
            w = f"(first_name || ' ' || last_name) ILIKE '%{self.esc(name)}%'"
        else:
            w = "1=1"
        sql = f"SELECT first_name, last_name, licence, licence_items FROM licence_register WHERE {w}"
        return self._query("get_worker_licences", {"worker_id": worker_id, "name": name}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} licence record(s).")

    def get_worker_ranking(self, top_n=10, user_role="default"):
        # scores are HR_Manager-gated; validator will reject for default role
        sql = (f"SELECT first_name, last_name, mob_score, site_score FROM worker_ranking "
               f"ORDER BY mob_score DESC LIMIT {int(top_n)}")
        return self._query("get_worker_ranking", {"top_n": top_n}, sql, user_role,
                           summarise=lambda tr: f"Top {tr.row_count} workers by mobilisation score.")
