"""Finance domain — purchase_summary / rate_card. Amounts & rates require the Finance role
(the validator enforces this; the tool degrades gracefully for default users)."""

from ._base import BaseTool


class FinanceTool(BaseTool):
    name = "finance"

    def get_purchase_summary(self, supplier=None, user_role="default"):
        w = f" WHERE supplier_name ILIKE '%{self.esc(supplier)}%'" if supplier else ""
        if user_role in ("Finance", "admin"):
            sql = ("SELECT supplier_name, COUNT(*) AS purchase_count, SUM(line_count) AS total_lines, "
                   f"SUM(computed_total) AS total_spend FROM purchase_summary{w} "
                   "GROUP BY supplier_name ORDER BY total_spend DESC")
            return self._query("get_purchase_summary", {"supplier": supplier}, sql, user_role,
                               summarise=lambda tr: "Spend by supplier — top: "
                               + (f"{tr.data[0]['supplier_name']} ${tr.data[0]['total_spend'] or 0:,.2f} "
                                  f"({int(tr.data[0]['purchase_count'])} purchases)." if tr.data else "none"))
        sql = ("SELECT supplier_name, COUNT(*) AS purchase_count, SUM(line_count) AS total_lines "
               f"FROM purchase_summary{w} GROUP BY supplier_name ORDER BY purchase_count DESC")
        return self._query("get_purchase_summary", {"supplier": supplier}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} supplier(s) with purchases "
                           "(amounts require the Finance role)."
                           + (f" Top by volume: {tr.data[0]['supplier_name']} "
                              f"({int(tr.data[0]['purchase_count'])} purchases)." if tr.data else ""))

    def get_client_revenue(self, client=None, year=None, user_role="default"):
        """Revenue invoiced per client (Xero ACCREC). Amounts need the Finance role."""
        w = []
        if client:
            w.append(f"contact_name ILIKE '%{self.esc(client)}%'")
        if year:
            w.append(f"month LIKE '{self.esc(year)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        if user_role in ("Finance", "admin"):
            sql = ("SELECT contact_name, SUM(invoices) AS invoices, SUM(invoiced) AS invoiced, "
                   "SUM(paid) AS paid, SUM(outstanding) AS outstanding "
                   f"FROM revenue_summary{where} GROUP BY contact_name ORDER BY invoiced DESC")
            return self._query("get_client_revenue", {"client": client, "year": year}, sql, user_role,
                               summarise=lambda tr: f"{tr.row_count} client(s)."
                               + (f" Top: {tr.data[0]['contact_name']} A${tr.data[0]['invoiced'] or 0:,.0f} invoiced"
                                  f" (A${tr.data[0]['outstanding'] or 0:,.0f} outstanding)."
                                  " Xero data ends ~2026-04." if tr.data else ""))
        sql = ("SELECT contact_name, SUM(invoices) AS invoices "
               f"FROM revenue_summary{where} GROUP BY contact_name ORDER BY invoices DESC")
        return self._query("get_client_revenue", {"client": client, "year": year}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} client(s) with invoices "
                           "(revenue amounts require the Finance role).")

    def get_outstanding_invoices(self, client=None, user_role="default"):
        """Unpaid sales invoices (accounts receivable)."""
        w = ["invoice_type = 'ACCREC'", "status = 'AUTHORISED'"]
        if user_role in ("Finance", "admin"):
            w.append("amount_due > 0")
        if client:
            w.append(f"contact_name ILIKE '%{self.esc(client)}%'")
        where = " WHERE " + " AND ".join(w)
        if user_role in ("Finance", "admin"):
            sql = ("SELECT invoice_number, contact_name, job_code, invoice_date, due_date, "
                   "total, amount_due "
                   f"FROM invoice_register{where} ORDER BY due_date")
            return self._query("get_outstanding_invoices", {"client": client}, sql, user_role,
                               summarise=lambda tr: f"{tr.row_count} outstanding invoice(s), "
                               f"A${sum(r['amount_due'] or 0 for r in tr.data):,.0f} due. "
                               "Xero data ends ~2026-04.")
        sql = ("SELECT invoice_number, contact_name, job_code, invoice_date, due_date "
               f"FROM invoice_register{where} ORDER BY due_date")
        return self._query("get_outstanding_invoices", {"client": client}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} authorised invoice(s) "
                           "(amounts require the Finance role).")

    def get_project_revenue(self, job_code, user_role="default"):
        """Revenue invoiced against one job code (mined from invoice references)."""
        j = self.esc(job_code)
        if user_role in ("Finance", "admin"):
            sql = ("SELECT job_code, contact_name, COUNT(*) AS invoices, SUM(total) AS invoiced, "
                   "SUM(amount_due) AS outstanding FROM invoice_register "
                   "WHERE invoice_type = 'ACCREC' AND status IN ('AUTHORISED', 'PAID') "
                   f"AND job_code ILIKE '%{j}%' GROUP BY job_code, contact_name ORDER BY invoiced DESC")
            return self._query("get_project_revenue", {"job_code": job_code}, sql, user_role,
                               summarise=lambda tr: (f"Job {tr.data[0]['job_code']}: "
                                                     f"A${tr.data[0]['invoiced'] or 0:,.0f} invoiced over "
                                                     f"{int(tr.data[0]['invoices'])} invoice(s)."
                                                     if tr.data else f"No invoices reference job '{job_code}'."))
        sql = ("SELECT job_code, contact_name, COUNT(*) AS invoices FROM invoice_register "
               f"WHERE invoice_type = 'ACCREC' AND job_code ILIKE '%{j}%' "
               "GROUP BY job_code, contact_name")
        return self._query("get_project_revenue", {"job_code": job_code}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} job match(es) "
                           "(amounts require the Finance role).")

    def get_rate_card(self, project=None, position=None, user_role="default"):
        w = []
        if project:
            w.append(f"project_name ILIKE '%{self.esc(project)}%'")
        if position:
            w.append(f"rate_title ILIKE '%{self.esc(position)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        if user_role in ("Finance", "admin"):
            sql = ("SELECT rate_title, project_name, day_shift_rate, night_shift_rate "
                   f"FROM rate_card{where} ORDER BY project_name, rate_title")
            return self._query("get_rate_card", {"project": project, "position": position}, sql, user_role,
                               summarise=lambda tr: f"{tr.row_count} rate line(s)"
                               + (f". e.g. {tr.data[0]['rate_title']} on {tr.data[0]['project_name']}: "
                                  f"day ${tr.data[0]['day_shift_rate'] or 0:,.2f}." if tr.data else "."))
        sql = (f"SELECT rate_title, project_name, project_code FROM rate_card{where} "
               "ORDER BY project_name, rate_title")
        return self._query("get_rate_card", {"project": project, "position": position}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} rate card line(s) exist "
                           "(rate values require the Finance role).")
