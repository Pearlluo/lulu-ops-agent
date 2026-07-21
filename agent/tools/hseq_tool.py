"""HSEQ & audit domain — hseq_register / audit_activity."""

from ._base import BaseTool


class HseqTool(BaseTool):
    name = "hseq"

    def get_hseq_register(self, open_only=False, overdue_only=False, priority=None, user_role="default"):
        w = []
        if overdue_only:
            w.append("is_overdue = true")
        elif open_only:
            w.append("is_open = true")
        if priority:
            w.append(f"action_priority ILIKE '%{self.esc(priority)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = ("SELECT issue_title, issue_type, issue_status, action_priority, action_due_date, is_open, is_overdue "
               f"FROM hseq_register{where} ORDER BY action_due_date")
        return self._query("get_hseq_register",
                           {"open_only": open_only, "overdue_only": overdue_only, "priority": priority},
                           sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} HSEQ action(s)"
                           + (" (overdue)" if overdue_only else " (open)" if open_only else "")
                           + (f". e.g. '{tr.data[0]['issue_title']}' ({tr.data[0]['action_priority']}, "
                              f"due {tr.data[0]['action_due_date']})." if tr.data else "."))

    def get_audit_issues(self, worker_id=None, event_type=None, limit=100, user_role="default"):
        w = []
        if worker_id is not None:
            w.append(f"opms_employee_id = {int(worker_id)}")
        if event_type:
            w.append(f"event_type ILIKE '%{self.esc(event_type)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = ("SELECT created_at, event_type, first_name, last_name "
               f"FROM audit_activity{where} ORDER BY created_at DESC LIMIT {int(limit)}")
        return self._query("get_audit_issues", {"worker_id": worker_id, "event_type": event_type},
                           sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} change event(s)"
                           + (f"; latest {tr.data[0]['created_at']}: {tr.data[0]['event_type']}." if tr.data else "."))

    def audit_event_breakdown(self, user_role="default"):
        sql = ("SELECT event_type, COUNT(*) AS events FROM audit_activity "
               "GROUP BY event_type ORDER BY events DESC")
        return self._query("audit_event_breakdown", {}, sql, user_role,
                           summarise=lambda tr: "Audit events by type — top: "
                           + ", ".join(f"{r['event_type']}: {r['events']:,}" for r in tr.data[:3]) + ".")
