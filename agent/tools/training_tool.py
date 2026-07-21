"""Training/compliance domain — training_compliance (deployment eligibility lives here)."""

from ._base import BaseTool


class TrainingTool(BaseTool):
    name = "training"

    def find_expired_tickets(self, count_only=False, user_role="default"):
        if count_only:
            sql = "SELECT COUNT(*) AS expired_certs FROM training_compliance WHERE is_expired = true"
            return self._query("find_expired_tickets", {"count_only": True}, sql, user_role,
                               summarise=lambda tr: f"There are {tr.data[0]['expired_certs']:,} expired certificates." if tr.data else "none")
        sql = ("SELECT first_name, last_name, competency_name, expiry_date FROM training_compliance "
               "WHERE is_expired = true ORDER BY expiry_date DESC")
        return self._query("find_expired_tickets", {}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} expired certificates (capped at limit); most recent lapse "
                           f"{tr.data[0]['first_name']} {tr.data[0]['last_name']} — {tr.data[0]['competency_name']}." if tr.data else "none")

    def find_expiring_tickets(self, days=30, user_role="default"):
        sql = ("SELECT first_name, last_name, competency_name, expiry_date, days_to_expiry "
               "FROM training_compliance WHERE is_expired = false "
               f"AND days_to_expiry BETWEEN 0 AND {int(days)} ORDER BY days_to_expiry")
        return self._query("find_expiring_tickets", {"days": days}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} certificates expire within {days} days."
                           + (f" Most urgent: {tr.data[0]['first_name']} {tr.data[0]['last_name']} — "
                              f"{tr.data[0]['competency_name']} in {int(tr.data[0]['days_to_expiry'])} days." if tr.data else ""))

    def check_worker_compliance(self, worker_id=None, worker_name=None, competency=None, user_role="default"):
        w = []
        if worker_id is not None:
            w.append(f"opms_employee_id = {int(worker_id)}")
        if worker_name:
            w.append(f"(first_name || ' ' || last_name) ILIKE '%{self.esc(worker_name)}%'")
        if competency:
            w.append(f"competency_name ILIKE '%{self.esc(competency)}%'")
        if not w:
            w = ["1=0"]
        sql = ("SELECT first_name, last_name, competency_name, status, expiry_date, days_to_expiry, is_expired "
               "FROM training_compliance WHERE " + " AND ".join(w))

        def verdict(tr):
            if not tr.data:
                return "No matching cert on record — treat as NOT COMPLIANT for this competency."
            ok = any(not r["is_expired"] for r in tr.data)
            r = tr.data[0]
            return (f"{'COMPLIANT' if ok else 'NOT COMPLIANT'}: {r['first_name']} {r['last_name']} — "
                    f"{r['competency_name']} (status {r['status']}, expiry {r['expiry_date']}).")
        return self._query("check_worker_compliance",
                           {"worker_id": worker_id, "worker_name": worker_name, "competency": competency},
                           sql, user_role, summarise=verdict)

    def find_not_eligible_workers(self, user_role="default"):
        sql = ("SELECT first_name, last_name, COUNT(*) AS expired_certs FROM training_compliance "
               "WHERE is_expired = true GROUP BY first_name, last_name ORDER BY expired_certs DESC")
        return self._query("find_not_eligible_workers", {}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} workers hold expired certs (not eligible for those competencies)."
                           + (f" Worst: {tr.data[0]['first_name']} {tr.data[0]['last_name']} ({tr.data[0]['expired_certs']} expired)." if tr.data else ""))

    def expiry_forecast(self, months=6, user_role="default"):
        sql = ("SELECT substring(expiry_date, 1, 7) AS month, COUNT(*) AS certs_expiring "
               "FROM training_compliance WHERE is_expired = false "
               f"AND days_to_expiry BETWEEN 0 AND {int(months) * 30} "
               "GROUP BY month ORDER BY month")
        return self._query("expiry_forecast", {"months": months}, sql, user_role,
                           summarise=lambda tr: f"Expiry forecast for the next {months} months: "
                           + ", ".join(f"{r['month']}: {r['certs_expiring']}" for r in tr.data[:6]) + ".")

    def compliance_by_group(self, user_role="default"):
        sql = ("SELECT group_name, COUNT(*) AS total_certs, "
               "SUM(CASE WHEN is_expired THEN 1 ELSE 0 END) AS expired "
               "FROM training_compliance GROUP BY group_name ORDER BY expired DESC")
        return self._query("compliance_by_group", {}, sql, user_role,
                           summarise=lambda tr: f"Compliance by competency group — worst: "
                           f"{tr.data[0]['group_name'] or 'ungrouped'} ({tr.data[0]['expired']}/{tr.data[0]['total_certs']} expired)." if tr.data else "none")
