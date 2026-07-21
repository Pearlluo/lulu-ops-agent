"""Time domain — weekly_timesheet (daily actuals) + timesheet_summary (site x month aggregate)."""

from ._base import BaseTool


def _matrix_markdown(rows, days, max_rows=40):
    """Render matrix rows as a markdown table in the weekly-report layout.
    NS columns are dropped when the whole week has no night shift (keeps it readable)."""
    if not rows:
        return "(no rows)"
    has_ns = any((r.get(f"{d.strftime('%a %d/%m')} NS") or 0) for r in rows for d in days)
    head = ["Position", "Name", "Site", "Job"]
    day_keys = []
    for d in days:
        lab = d.strftime("%a %d/%m")
        head.append(lab + (" DS" if has_ns else ""))
        day_keys.append(lab + " DS")
        if has_ns:
            head.append(lab + " NS")
            day_keys.append(lab + " NS")
    head += (["DS", "NS", "Total"] if has_ns else ["Total"])
    lines = ["| " + " | ".join(head) + " |",
             "|" + "---|" * len(head)]
    for r in rows[:max_rows]:
        cells = [str(r.get("position_name") or ""),
                 f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip(),
                 str(r.get("site_name") or ""),
                 str(r.get("project_name") or "").split(" - ")[0]]   # job code, like the Excel's JOB ID
        for k in day_keys:
            v = r.get(k)
            cells.append(f"{v:.2f}".rstrip("0").rstrip(".") if v else "")
        if has_ns:
            cells += [f"{r.get('total_ds') or 0:.2f}".rstrip("0").rstrip("."),
                      f"{r.get('total_ns') or 0:.2f}".rstrip("0").rstrip(".")]
        cells.append(f"{r.get('total_hours') or 0:.2f}".rstrip("0").rstrip("."))
        lines.append("| " + " | ".join(cells) + " |")
    if len(rows) > max_rows:
        lines.append(f"| … | ({len(rows) - max_rows} more rows) | | " + " |" * (len(head) - 3))
    return "\n".join(lines)


class TimesheetTool(BaseTool):
    name = "timesheet"

    def get_weekly_timesheet(self, date_from=None, date_to=None, worker_id=None, worker_name=None,
                             project=None, site=None, group_by=None, user_role="default"):
        """Daily ACTUAL hours (GitHub weekly-timesheet automation logic, computed in the lake):
        OPMS actuals replace roster hours when matched, minus sign-out/sign-in gap deductions.
        group_by='matrix' -> the company's weekly-report layout (per person, Mon..Sun DS/NS
        columns + totals) — the PREFERRED shape for '所有人的timesheet / everyone's timesheet'.
        group_by='worker' -> one row per person (totals only). group_by='day' -> daily totals.
        Default (None) = day-entry detail rows."""
        if group_by == "matrix" and not (date_from and date_to):
            import datetime as _dt
            from lulu_time import perth_today
            _monday = perth_today() - _dt.timedelta(days=perth_today().weekday() + 7)
            date_from, date_to = _monday.isoformat(), (_monday + _dt.timedelta(days=6)).isoformat()
        w = []
        if date_from:
            w.append(f"work_date >= '{self.esc(date_from)}'")
        if date_to:
            w.append(f"work_date <= '{self.esc(date_to)}'")
        if worker_id is not None:
            w.append(f"opms_employee_id = {int(worker_id)}")
        if worker_name:
            w.append(f"(first_name || ' ' || last_name) ILIKE '%{self.esc(worker_name)}%'")
        if project:
            w.append(f"project_name ILIKE '%{self.esc(project)}%'")
        if site:
            w.append(f"site_name ILIKE '%{self.esc(site)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        rng = f"{date_from or 'start'} → {date_to or 'latest'}"
        args = {"date_from": date_from, "date_to": date_to, "worker_id": worker_id,
                "worker_name": worker_name, "project": project, "site": site, "group_by": group_by}

        if group_by == "matrix":
            # the company's weekly-report layout: one row per person, Mon..Sun DS/NS columns,
            # then DS/NS totals + grand total (same shape as the automation's Excel report)
            import datetime as dt
            d0 = dt.date.fromisoformat(date_from)
            d1 = min(dt.date.fromisoformat(date_to), d0 + dt.timedelta(days=6))   # cap at 7 columns
            days = [(d0 + dt.timedelta(days=i)) for i in range((d1 - d0).days + 1)]
            day_cols = []
            for d in days:
                lab = d.strftime("%a %d/%m")
                for s in ("DS", "NS"):
                    day_cols.append(f"SUM(CASE WHEN work_date = '{d.isoformat()}' AND shift_type = '{s}' "
                                    f"THEN actual_hours END) AS \"{lab} {s}\"")
            sql = ("SELECT position_name, first_name, last_name, site_name, project_name, "
                   + ", ".join(day_cols) + ", "
                   "SUM(CASE WHEN shift_type = 'DS' THEN actual_hours ELSE 0 END) AS total_ds, "
                   "SUM(CASE WHEN shift_type = 'NS' THEN actual_hours ELSE 0 END) AS total_ns, "
                   "SUM(actual_hours) AS total_hours "
                   f"FROM weekly_timesheet{where} "
                   "GROUP BY position_name, first_name, last_name, site_name, project_name "
                   "ORDER BY position_name, last_name, first_name")

            def _sum_m(tr):
                total = sum(r["total_hours"] or 0 for r in tr.data)
                md = _matrix_markdown(tr.data, days)
                return (f"Weekly timesheet {rng}: {tr.row_count} person-rows, {total:,.1f} actual hours. "
                        f"(weekly-report layout: per person, day columns DS/NS, totals)\n\n{md}")
            return self._query("get_weekly_timesheet", args, sql, user_role, summarise=_sum_m)

        if group_by == "worker":
            sql = ("SELECT opms_employee_id, first_name, last_name, position_name, "
                   "MAX(supplier_name) AS supplier_name, "
                   "COUNT(*) AS days_worked, SUM(roster_hours) AS roster_hours, "
                   "SUM(gap_hours) AS gap_hours, SUM(actual_hours) AS actual_hours "
                   f"FROM weekly_timesheet{where} "
                   "GROUP BY opms_employee_id, first_name, last_name, position_name "
                   "ORDER BY actual_hours DESC")

            def _sum_w(tr):
                total = sum(r["actual_hours"] or 0 for r in tr.data)
                top = (f" Top: {tr.data[0]['first_name']} {tr.data[0]['last_name']} "
                       f"{tr.data[0]['actual_hours']:,.1f}h." if tr.data else "")
                return (f"Weekly timesheet {rng}: {tr.row_count} workers, {total:,.1f} actual hours "
                        f"(one row per person: days worked + roster/gap/actual totals).{top}")
            return self._query("get_weekly_timesheet", args, sql, user_role, summarise=_sum_w)

        if group_by == "day":
            sql = ("SELECT work_date, COUNT(DISTINCT opms_employee_id) AS workers, "
                   "SUM(actual_hours) AS actual_hours "
                   f"FROM weekly_timesheet{where} GROUP BY work_date ORDER BY work_date")
            return self._query("get_weekly_timesheet", args, sql, user_role,
                               summarise=lambda tr: f"Daily totals {rng}: "
                               + ", ".join(f"{r['work_date']}: {r['actual_hours']:,.0f}h"
                                           for r in tr.data[:7]) + ".")

        sql = ("SELECT opms_employee_id, first_name, last_name, position_name, supplier_name, "
               "project_name, site_name, "
               "work_date, shift_type, roster_hours, gap_hours, actual_hours, hours_source "
               f"FROM weekly_timesheet{where} ORDER BY work_date, last_name, first_name")

        def _sum(tr):
            total = sum(r["actual_hours"] or 0 for r in tr.data)
            gaps = sum(r["gap_hours"] or 0 for r in tr.data)
            ppl = len({r["opms_employee_id"] for r in tr.data})
            capped = (" Result was CAPPED by the row limit — totals cover the returned rows only; "
                      "use group_by='worker' for per-person weekly totals, or narrow the filters."
                      if tr.row_count >= 100 else "")
            return (f"Timesheet {rng}: {tr.row_count} day-entries, {ppl} workers, "
                    f"{total:,.1f} actual hours" + (f" ({gaps:,.1f}h gap deducted)" if gaps else "")
                    + ". actual = OPMS-matched hours minus gap deductions (weekly automation logic)." + capped)

        return self._query("get_weekly_timesheet", args, sql, user_role, summarise=_sum)

    def get_worker_hours(self, worker_id=None, worker_name=None, month=None, year=None, user_role="default"):
        w = []
        if worker_id is not None:
            w.append(f"opms_employee_id = {int(worker_id)}")
        if worker_name:
            w.append(f"(first_name || ' ' || last_name) ILIKE '%{self.esc(worker_name)}%'")
        if month:
            w.append(f"month = '{self.esc(month)}'")
        elif year:
            w.append(f"month LIKE '{self.esc(year)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = f"SELECT SUM(total_hours) AS total_hours, SUM(entry_count) AS entries FROM timesheet_summary{where}"
        return self._query("get_worker_hours",
                           {"worker_id": worker_id, "worker_name": worker_name, "month": month, "year": year},
                           sql, user_role,
                           summarise=lambda tr: (f"Total hours: {tr.data[0]['total_hours'] or 0:,.0f} "
                                                 f"across {tr.data[0]['entries'] or 0:,} entries.") if tr.data else "no hours")

    def get_site_hours(self, site=None, year=None, user_role="default"):
        w = []
        if site:
            w.append(f"site_name ILIKE '%{self.esc(site)}%'")
        if year:
            w.append(f"month LIKE '{self.esc(year)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = ("SELECT site_name, SUM(total_hours) AS total_hours FROM timesheet_summary"
               f"{where} GROUP BY site_name ORDER BY total_hours DESC")
        return self._query("get_site_hours", {"site": site, "year": year}, sql, user_role,
                           summarise=lambda tr: "Hours by site — top: "
                           + ", ".join(f"{r['site_name']}: {r['total_hours']:,.0f}h" for r in tr.data[:3]) + "." if tr.data else "no hours")

    def get_project_hours(self, project=None, user_role="default"):
        """Hours per project come from rostered hours (BMS) — timesheets are per site, not project."""
        w = f" WHERE project_name ILIKE '%{self.esc(project)}%'" if project else " WHERE project_name IS NOT NULL"
        sql = ("SELECT project_name, SUM(hours) AS rostered_hours FROM roster_summary"
               f"{w} GROUP BY project_name ORDER BY rostered_hours DESC")
        return self._query("get_project_hours", {"project": project}, sql, user_role, approx=True,
                           summarise=lambda tr: "Rostered hours by project (proxy — timesheets track sites, not projects): "
                           + ", ".join(f"{r['project_name']}: {r['rostered_hours'] or 0:,.0f}h" for r in tr.data[:3]) + "." if tr.data else "none")

    def get_timesheet_summary(self, year=None, by="site", user_role="default"):
        dim = "site_name" if by == "site" else "month"
        w = f" WHERE month LIKE '{self.esc(year)}%'" if year else ""
        sql = (f"SELECT {dim}, SUM(total_hours) AS total_hours, SUM(entry_count) AS entries "
               f"FROM timesheet_summary{w} GROUP BY {dim} ORDER BY total_hours DESC")
        return self._query("get_timesheet_summary", {"year": year, "by": by}, sql, user_role,
                           summarise=lambda tr: f"Timesheet hours by {by}" + (f" in {year}" if year else "")
                           + " — top: " + ", ".join(f"{r[dim]}: {r['total_hours']:,.0f}h" for r in tr.data[:3]) + "." if tr.data else "none")

    def top_workers_by_hours(self, year=None, top_n=10, user_role="default"):
        w = f" WHERE month LIKE '{self.esc(year)}%'" if year else ""
        sql = ("SELECT first_name, last_name, SUM(total_hours) AS total_hours FROM timesheet_summary"
               f"{w} GROUP BY first_name, last_name ORDER BY total_hours DESC LIMIT {int(top_n)}")
        return self._query("top_workers_by_hours", {"year": year, "top_n": top_n}, sql, user_role,
                           summarise=lambda tr: f"Top {tr.row_count} workers by hours"
                           + (f" ({tr.data[0]['first_name']} {tr.data[0]['last_name']} leads with "
                              f"{tr.data[0]['total_hours']:,.0f}h)." if tr.data else "."))
