"""FDS (Form Distribution System) domain — the fds_* gold tables, one per FDS SharePoint list."""

from pathlib import Path

import yaml

from ._base import BaseTool

_FDS_TABLES = None


def fds_tables():
    """{table: [allowed fields]} for every registered fds_* table (registry is the source of truth)."""
    global _FDS_TABLES
    if _FDS_TABLES is None:
        reg = yaml.safe_load((Path(__file__).resolve().parents[1] / "agent_registry.yaml")
                             .read_text(encoding="utf-8"))
        _FDS_TABLES = {t: (cfg.get("allowed_fields") or [])
                       for t, cfg in (reg.get("tables") or {}).items() if t.startswith("fds_")}
    return _FDS_TABLES


class FdsTool(BaseTool):
    name = "fds"

    def list_fds_lists(self, user_role="default"):
        """Catalogue of every FDS list available for querying (no SQL needed)."""
        from ._base import ToolResult
        rows = [{"table": t, "fields": ", ".join(f for f in fl if f not in ("id",))}
                for t, fl in sorted(fds_tables().items())]
        tr = ToolResult(tool=self.name, function="list_fds_lists", args={}, ok=True,
                        data=rows, row_count=len(rows), confidence="High")
        tr.summary = (f"{len(rows)} FDS lists are queryable: "
                      + ", ".join(r["table"].replace("fds_", "") for r in rows) + ".")
        return tr

    def search_fds(self, list_name, keyword=None, limit=50, user_role="default"):
        """Rows from ONE FDS list, optionally filtered by a keyword across its text fields."""
        key = "fds_" + str(list_name or "").lower().replace("fds_", "").replace("fds-", "") \
                                             .replace(" ", "_").replace("-", "_")
        tables = fds_tables()
        table = key if key in tables else next(
            (t for t in tables if key.replace("fds_", "") in t), None)
        if not table:
            from ._base import ToolResult
            tr = ToolResult(tool=self.name, function="search_fds",
                            args={"list_name": list_name, "keyword": keyword})
            tr.summary = (f"No FDS list matches '{list_name}'. Available: "
                          + ", ".join(t.replace("fds_", "") for t in sorted(tables)) + ".")
            tr.confidence = "Medium"
            return tr
        cols = tables[table]
        w = ""
        if keyword:
            s = self.esc(keyword)
            like = [f"CAST({c} AS VARCHAR) ILIKE '%{s}%'" for c in cols
                    if not c.endswith("LookupId") and c not in ("id",)][:14]
            w = " WHERE (" + " OR ".join(like) + ")"
        sql = (f"SELECT {', '.join(cols)} FROM {table}{w} "
               f"ORDER BY Modified DESC LIMIT {max(1, min(int(limit or 50), 200))}")
        return self._query("search_fds", {"list_name": list_name, "keyword": keyword},
                           sql, user_role,
                           summarise=lambda tr: f"{tr.row_count} row(s) from {table}"
                           + (f" matching '{keyword}'." if keyword else "."))
