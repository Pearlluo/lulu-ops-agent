"""Project domain — project_job_summary / job_detail / site_assignment / project_bridge."""

from ._base import BaseTool


class ProjectTool(BaseTool):
    name = "project"

    def resolve_project_client(self, term, user_role="default"):
        """OPMS<->BMS bridge lookup (GitHub rates-pipeline logic): job-code prefix
        ('SH-25006...' -> JMS-Jobs -> JMS-Projects -> client) + manual map fallback."""
        t = self.esc(term)
        sql = ("SELECT job_code, job_title, project_name, client_code, client_name, "
               "opms_project_name, source FROM project_bridge "
               f"WHERE job_code ILIKE '%{t}%' OR job_title ILIKE '%{t}%' "
               f"OR project_name ILIKE '%{t}%' OR client_code ILIKE '%{t}%' "
               f"OR client_name ILIKE '%{t}%' OR opms_project_name ILIKE '%{t}%' "
               "ORDER BY source, job_code")
        return self._query("resolve_project_client", {"term": term}, sql, user_role,
                           summarise=lambda tr: (f"{tr.row_count} bridge match(es) for '{term}'. "
                                                 + "; ".join(f"{r['job_code'] or r['opms_project_name']} → "
                                                             f"{r['client_code'] or r['client_name'] or '?'}"
                                                             for r in tr.data[:5])) if tr.data
                           else f"No project/client bridge match for '{term}'.")

    def get_active_projects(self, client=None, user_role="default"):
        w = "is_active = true"
        if client:
            w += f" AND client_name ILIKE '%{self.esc(client)}%'"
        sql = ("SELECT project_name, client_name, job_count, active_job_count, project_start_date "
               f"FROM project_job_summary WHERE {w} ORDER BY job_count DESC")
        return self._query("get_active_projects", {"client": client}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} active projects"
                           + (f" for '{client}'" if client else "")
                           + (f". Largest: {tr.data[0]['project_name']} ({tr.data[0]['client_name']}) "
                              f"with {int(tr.data[0]['job_count'] or 0)} jobs." if tr.data else "."))

    def get_project_jobs(self, client=None, project=None, user_role="default"):
        w = []
        if client:
            w.append(f"client_name ILIKE '%{self.esc(client)}%'")
        if project:
            w.append(f"project_name ILIKE '%{self.esc(project)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = ("SELECT project_name, client_name, job_count, active_job_count "
               f"FROM project_job_summary{where} ORDER BY job_count DESC")

        def summ(tr):
            tot = sum(r["job_count"] or 0 for r in tr.data)
            act = sum(r["active_job_count"] or 0 for r in tr.data)
            return f"{int(tot)} jobs across {tr.row_count} project(s), {int(act)} active."
        return self._query("get_project_jobs", {"client": client, "project": project}, sql, user_role, summarise=summ)

    def get_job_detail(self, job_code=None, client=None, active_only=False, user_role="default"):
        w = []
        if job_code:
            w.append(f"(job_code ILIKE '%{self.esc(job_code)}%' OR job_title ILIKE '%{self.esc(job_code)}%')")
        if client:
            w.append(f"client_name ILIKE '%{self.esc(client)}%'")
        if active_only:
            w.append("is_active = true")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = ("SELECT job_code, job_title, job_status, project_name, client_name, work_location_name, "
               f"lead_first_name, lead_last_name, is_active FROM job_detail{where} ORDER BY job_code")
        return self._query("get_job_detail", {"job_code": job_code, "client": client, "active_only": active_only},
                           sql, user_role,
                           summarise=lambda tr: (f"{tr.data[0]['job_code']}: {tr.data[0]['job_title']} — "
                                                 f"{tr.data[0]['project_name']} / {tr.data[0]['client_name']}, "
                                                 f"status {tr.data[0]['job_status']}, lead "
                                                 f"{tr.data[0]['lead_first_name'] or '?'} {tr.data[0]['lead_last_name'] or ''}.")
                           if tr.row_count == 1 else f"{tr.row_count} job(s) match.")

    def get_site_assignments(self, site=None, worker=None, user_role="default"):
        w = []
        if site:
            w.append(f"site_name ILIKE '%{self.esc(site)}%'")
        if worker:
            w.append(f"(first_name || ' ' || last_name) ILIKE '%{self.esc(worker)}%'")
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = ("SELECT DISTINCT first_name, last_name, position_name, site_name, team_name "
               f"FROM site_assignment{where} ORDER BY last_name")
        return self._query("get_site_assignments", {"site": site, "worker": worker}, sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} site assignment(s)"
                           + (f" at '{site}'" if site else "") + (f" for '{worker}'" if worker else "") + ".")
