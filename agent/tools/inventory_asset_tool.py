"""Inventory & asset domain — inventory_summary / asset_register / hardware_register."""

from ._base import BaseTool


class InventoryAssetTool(BaseTool):
    name = "inventory_asset"

    def search_assets(self, term=None, status=None, user_role="default"):
        w = []
        if term:
            s = self.esc(term)
            w.append(f"(asset_name ILIKE '%{s}%' OR asset_id ILIKE '%{s}%' OR model ILIKE '%{s}%')")
        if status:
            w.append(f"status ILIKE '%{self.esc(status)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = ("SELECT asset_id, asset_name, status, model, manufacturer_name, location_name, asset_group_name "
               f"FROM asset_register{where} ORDER BY asset_id")
        return self._query("search_assets", {"term": term, "status": status}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} asset(s) match"
                           + (f". e.g. {tr.data[0]['asset_id']} {tr.data[0]['asset_name']} — "
                              f"{tr.data[0]['status']} at {tr.data[0]['location_name'] or 'unknown location'}." if tr.data else "."))

    def assets_by_status(self, user_role="default"):
        sql = ("SELECT status, COUNT(*) AS asset_count FROM asset_register "
               "GROUP BY status ORDER BY asset_count DESC")
        return self._query("assets_by_status", {}, sql, user_role,
                           summarise=lambda tr: "Assets by status: "
                           + ", ".join(f"{r['status'] or 'unset'}: {r['asset_count']}" for r in tr.data[:5]) + ".")

    def get_inventory_summary(self, item=None, location=None, user_role="default"):
        w = []
        if item:
            w.append(f"item_name ILIKE '%{self.esc(item)}%'")
        if location:
            w.append(f"location_name ILIKE '%{self.esc(location)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = ("SELECT item_name, stock, location_name, category_name "
               f"FROM inventory_summary{where} ORDER BY stock DESC")
        return self._query("get_inventory_summary", {"item": item, "location": location}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} inventory line(s)"
                           + (f". Highest stock: {tr.data[0]['item_name']} ({tr.data[0]['stock'] or 0:.0f} units)." if tr.data else "."))

    def find_low_stock(self, threshold=5, user_role="default"):
        sql = ("SELECT item_name, stock, location_name FROM inventory_summary "
               f"WHERE stock IS NULL OR stock <= {int(threshold)} ORDER BY stock NULLS FIRST")
        return self._query("find_low_stock", {"threshold": threshold}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} items at or below {threshold} units (incl. out-of-stock).")

    def get_ppe_signouts(self, person=None, item=None, location=None, job=None,
                         month=None, user_role="default"):
        """PPE sign-out lines (who took what, from which store, for which job)."""
        w = ["txn_kind = 'sign_out'"]
        if person:
            w.append(f"person_name ILIKE '%{self.esc(person)}%'")
        if item:
            w.append(f"(item_name ILIKE '%{self.esc(item)}%' OR item_code ILIKE '%{self.esc(item)}%')")
        if location:
            w.append(f"from_location ILIKE '%{self.esc(location)}%'")
        if job:
            w.append(f"(job_code ILIKE '%{self.esc(job)}%' OR project_name ILIKE '%{self.esc(job)}%')")
        if month:
            w.append(f"txn_month = '{self.esc(month)}'")
        sql = ("SELECT txn_date, item_name, units, from_location, person_name, job_code, project_name "
               f"FROM ppe_transactions WHERE {' AND '.join(w)} ORDER BY txn_date DESC LIMIT 100")
        return self._query("get_ppe_signouts",
                           {"person": person, "item": item, "location": location, "job": job, "month": month},
                           sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} PPE sign-out line(s)"
                           + (f". Latest: {tr.data[0]['txn_date']} {tr.data[0]['person_name']} — "
                              f"{tr.data[0]['units']:.0f} x {tr.data[0]['item_name']} "
                              f"from {tr.data[0]['from_location'] or 'unknown store'}." if tr.data else "."))

    def get_ppe_monthly_usage(self, item=None, location=None, project=None,
                              months=6, user_role="default"):
        """Monthly PPE sign-out totals (units), optionally by item/store/project."""
        w = ["txn_kind = 'sign_out'"]
        if item:
            w.append(f"(item_name ILIKE '%{self.esc(item)}%' OR item_code ILIKE '%{self.esc(item)}%')")
        if location:
            w.append(f"from_location ILIKE '%{self.esc(location)}%'")
        if project:
            w.append(f"project_name ILIKE '%{self.esc(project)}%'")
        sql = ("SELECT txn_month, SUM(units) AS total_units, COUNT(*) AS sign_out_lines, "
               "COUNT(DISTINCT person_name) AS distinct_people "
               f"FROM ppe_transactions WHERE {' AND '.join(w)} "
               f"GROUP BY txn_month ORDER BY txn_month DESC LIMIT {int(months)}")
        return self._query("get_ppe_monthly_usage",
                           {"item": item, "location": location, "project": project, "months": months},
                           sql, user_role,
                           summarise=lambda tr: "Monthly PPE usage: "
                           + ", ".join(f"{r['txn_month']}: {r['total_units'] or 0:.0f} units"
                                       for r in tr.data[:6]) + "." if tr.data else "No sign-outs found.")

    def hardware_stock(self, term=None, user_role="default"):
        w = f" WHERE (hardware_name ILIKE '%{self.esc(term)}%' OR code ILIKE '%{self.esc(term)}%')" if term else ""
        sql = ("SELECT hardware_name, code, status, in_stock, category_name "
               f"FROM hardware_register{w} ORDER BY in_stock DESC")
        return self._query("hardware_stock", {"term": term}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} hardware item(s)"
                           + (f". Top stock: {tr.data[0]['hardware_name']} ({tr.data[0]['in_stock'] or 0:.0f})." if tr.data else "."))
