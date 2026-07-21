"""
LuluAgent Smart V1 — controlled query tool.

The ONLY path to data: SQL goes through sql_validator.validate() (hard gate: Gold-only,
allowed_fields, role gating, SELECT-only, LIMIT) and then DuckDB. Nothing else executes.
"""

from dataclasses import dataclass, field
from pathlib import Path

from sql_validator import load_registry, validate, run_query, GOLD_DIR


@dataclass
class QueryResult:
    ok: bool
    sql: str = ""
    rows: list = field(default_factory=list)
    cols: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    row_count: int = 0


class QueryTool:
    def __init__(self, gold_dir=GOLD_DIR):
        self.gold_dir = Path(gold_dir)
        self.registry = load_registry()

    def validate_only(self, sql, user_role="default"):
        return validate(sql, user_role, self.registry)

    def run(self, sql, user_role="default") -> QueryResult:
        rows, cols, res = run_query(sql, user_role, self.registry, self.gold_dir)
        if not res.ok:
            return QueryResult(False, sql=sql, errors=res.errors, tables=res.tables)
        return QueryResult(True, sql=res.sql, rows=rows or [], cols=cols or [],
                           tables=res.tables, row_count=len(rows or []))

    def resolve_person(self, name, user_role="default"):
        """Disambiguation helper: return candidate workers matching a name."""
        safe = name.replace("'", "''")
        r = self.run(
            "SELECT opms_employee_id, first_name, last_name, position_name "
            "FROM employee_profile "
            f"WHERE (first_name || ' ' || last_name) ILIKE '%{safe}%' LIMIT 10",
            user_role)
        return r.rows if r.ok else []

    # ------------------------------------------------------------------
    # RAW debug lookup (Silver flat = 1:1 Bronze mirror) — Admin_IT ONLY.
    #
    # The Gold validator can't validate non-Gold tables BY DESIGN, so this path has
    # its own equivalent hard gate instead of free SQL:
    #   * role check: Admin_IT only (everyone else gets a refusal, never data)
    #   * template-only SQL: fixed SELECT-DISTINCT-ILIKE shape, no caller SQL accepted
    #   * whitelisted (table, column) pairs only, forced LIMIT
    #   * every call audit-logged to logs/raw_debug_access.jsonl
    # Results are RAW / UNVALIDATED — for debugging WHERE data lives, never for
    # business recommendations.
    # ------------------------------------------------------------------
    RAW_SCAN_TARGETS = [          # (silver-flat table, column) — name-ish columns only
        ("sp__PPL-Rosters", "Project"), ("sp__PPL-Rosters", "Title"),
        ("sp__PPL-People", "Title"),
        ("sp__JMS-Projects", "Title"), ("sp__JMS-Projects", "ATitle"),
        ("sp__JMS-Clients", "Title"),
        ("sp__SYS-OpsSections", "Title"),
        ("sp__SMS-Suppliers", "Title"),
        ("sp__PPL-Timesheets", "Title"),
    ]

    def raw_debug_lookup(self, term, user_role="default", limit=8):
        """Scan Silver-flat (Bronze mirror) for an entity name. Admin_IT only."""
        if user_role != "Admin_IT":
            return {"allowed": False, "hits": [],
                    "note": "RAW layer access requires the Admin_IT role."}
        import datetime
        import duckdb
        import json
        safe = str(term).replace("'", "''")
        flat = self.gold_dir.parent / "silver" / "flat"
        con = duckdb.connect()
        hits = []
        for table, col in self.RAW_SCAN_TARGETS:
            p = flat / f"{table}.parquet"
            if not p.exists():
                continue
            try:
                rows = con.execute(
                    f"SELECT DISTINCT \"{col}\" FROM '{p}' "
                    f"WHERE \"{col}\" ILIKE '%{safe}%' LIMIT {int(limit)}").fetchall()
                if rows:
                    hits.append({"layer": "silver_flat (bronze mirror)", "table": table,
                                 "column": col, "values": [r[0] for r in rows]})
            except Exception:
                continue
        log = self.gold_dir.parent / "agent" / "logs" / "raw_debug_access.jsonl"
        log.parent.mkdir(exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                "role": user_role, "term": term,
                                "tables_hit": [h["table"] for h in hits]},
                               ensure_ascii=False) + "\n")
        return {"allowed": True, "hits": hits,
                "note": "RAW / UNVALIDATED — silver-flat mirror of Bronze; debug only, "
                        "not for business decisions."}
