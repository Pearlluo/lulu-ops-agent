"""
tools/_base.py — the ONE data-access chokepoint for every business tool.

    business tool -> controlled SQL -> sql_validator -> query_tool -> DuckDB -> Gold

Tools never open DuckDB or parquet themselves; they call self._query(), which routes
through QueryTool (which hard-validates first). Every tool function returns a ToolResult:
structured JSON + human summary + confidence + data caveats.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import yaml                                    # noqa: E402
from query_tool import QueryTool               # noqa: E402  (QueryTool validates via sql_validator)

# data-reality caveats per Gold table (from live extraction)
TABLE_CAVEATS = {
    "employee_profile": "Position/supplier/ops-section only populate for the ~446 workers also tracked in BMS (of 2,065).",
    "audit_activity": "Change history only goes back to 2025-01-08 (OPMS CDC retention).",
    "roster_summary": "Roster data extracted from 2024-01-01 onward (OPMS + BMS combined).",
    "timesheet_summary": "Monthly aggregates from OPMS timesheets (data begins 2024-04). Hours are tracked per SITE, not per project.",
    "purchase_summary": "Invoice amounts are Finance-role gated; default role sees counts/dates only.",
    "training_compliance": "Compliance is derived: a cert is non-compliant when is_expired=true (no separate is_compliant flag).",
    "rate_card": "Day/night rates are commercial — Finance role required to see values.",
    "site_assignment": "Site assignments come from OPMS sites/employees (current mapping, not history).",
}

_shared_query_tool = None
_definitions = None


def get_query_tool():
    global _shared_query_tool
    if _shared_query_tool is None:
        _shared_query_tool = QueryTool()
    return _shared_query_tool


def get_definitions():
    global _definitions
    if _definitions is None:
        _definitions = yaml.safe_load(open(AGENT_DIR / "business_definitions.yaml", encoding="utf-8"))
    return _definitions


@dataclass
class ToolResult:
    tool: str
    function: str
    args: dict = field(default_factory=dict)
    ok: bool = False
    data: list = field(default_factory=list)          # rows as dicts (structured JSON)
    row_count: int = 0
    summary: str = ""                                  # human-readable business answer
    confidence: str = "Low"                            # High / Medium / Low
    caveats: list = field(default_factory=list)
    sql: str = ""
    validator_errors: list = field(default_factory=list)
    tables: list = field(default_factory=list)

    def to_json(self, max_rows=50):
        d = {"tool": self.tool, "function": self.function, "args": self.args, "ok": self.ok,
             "row_count": self.row_count, "data": self.data[:max_rows], "summary": self.summary,
             "confidence": self.confidence, "caveats": self.caveats, "sql": self.sql,
             "validator_errors": self.validator_errors}
        return json.dumps(d, ensure_ascii=False, default=str)


class BaseTool:
    name = "base"

    def __init__(self):
        self.qt = get_query_tool()
        self.defs = get_definitions()

    @staticmethod
    def esc(v):
        return str(v).replace("'", "''")

    def _query(self, function, args, sql, user_role="default",
               summarise=None, approx=False) -> ToolResult:
        """The only path to data. Validates (sql_validator) then executes (DuckDB)."""
        res = self.qt.run(sql, user_role)
        tr = ToolResult(tool=self.name, function=function, args=args, sql=sql)
        if not res.ok:
            tr.validator_errors = res.errors
            tr.summary = "Blocked by validator: " + "; ".join(res.errors)
            tr.confidence = "Low"
            return tr
        tr.ok = True
        tr.sql = res.sql
        tr.tables = res.tables
        tr.row_count = res.row_count
        tr.data = [dict(zip(res.cols, r)) for r in res.rows]
        tr.caveats = [TABLE_CAVEATS[t] for t in res.tables if t in TABLE_CAVEATS]
        if summarise:
            tr.summary = summarise(tr)
        else:
            tr.summary = f"{tr.row_count} rows from {', '.join(res.tables)}."
        if tr.row_count == 0:
            tr.summary = "No matching records found. " + tr.summary if summarise else "No matching records found."
            tr.confidence = "Medium"
        else:
            tr.confidence = "Medium" if approx else "High"
        return tr
