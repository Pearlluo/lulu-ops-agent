"""
Controlled-SQL validator + DuckDB executor for the Lulu Agent.

Turns the agent_registry.yaml semantic layer into HARD constraints. The agent's generated SQL
is parsed (sqlglot AST — not regex) and must pass ALL checks before it touches DuckDB:

  1. Gold-only        — every table ref is a registered Gold table (or read_parquet of a gold/ path).
                        bronze/silver/arbitrary files & file-reading funcs are rejected.
  2. allowed_fields   — every selected/filtered column is in that table's allowed_fields...
  3. no SELECT *      — bare star rejected (COUNT(*) allowed).
  4. no sensitive     — restricted fields rejected unless the user_role unlocks them.
  5. role gating      — DOB / rates / purchase amounts / ranking scores etc. per role.
  6. SELECT-only      — INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/COPY/PRAGMA/ATTACH rejected.
  7. LIMIT enforced   — auto-injected (default 100) if missing; clamped to MAX.
  8. execute last     — DuckDB runs only the validated/normalised SQL.

Public API:
    reg = load_registry()
    res = validate(sql, user_role="default", reg=reg)        -> ValidationResult
    rows, cols, res = run_query(sql, user_role, reg=reg)      -> validates then executes
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

AGENT_DIR = Path(__file__).resolve().parent
DATA_DIR = AGENT_DIR.parent
GOLD_DIR = DATA_DIR / "gold"
REGISTRY_PATH = AGENT_DIR / "agent_registry.yaml"

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

# file-reading funcs we allow (only for gold/ paths) vs hard-blocked
PARQUET_FUNCS = {"read_parquet", "parquet_scan"}
BLOCKED_FUNCS = {"read_csv", "read_csv_auto", "read_json", "read_json_auto", "read_text",
                 "glob", "read_blob", "read_ndjson", "read_ndjson_auto"}
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
                   exp.Command, exp.Pragma, exp.Set, exp.Use)


@dataclass
class ValidationResult:
    ok: bool
    sql: str = ""                 # normalised SQL (with enforced LIMIT) if ok
    tables: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def load_registry(path=REGISTRY_PATH):
    import yaml
    reg = yaml.safe_load(open(path, encoding="utf-8"))
    tables = {}
    for name, t in (reg.get("tables") or {}).items():
        allowed = set(t.get("allowed_fields") or [])
        restricted = {}
        for fname, meta in (t.get("restricted_fields") or {}).items():
            restricted[fname] = (meta or {}).get("role")
        tables[name] = {"allowed": allowed, "restricted": restricted}
    return {"tables": tables}


def _role_ok(user_role, needed_role):
    # Admin/superuser convenience: a user holding the exact role, or 'admin', passes.
    return user_role == needed_role or user_role in ("admin", "Admin")


def _gold_table_from_path(p):
    """Return gold table name if path is a gold/<name>.parquet, else None (and flag non-gold)."""
    p = p.replace("\\", "/")
    if "/bronze/" in p or "/silver/" in p or p.startswith("bronze/") or p.startswith("silver/"):
        return None
    m = re.search(r"(?:^|/)gold/([A-Za-z0-9_]+)\.parquet$", p)
    return m.group(1) if m else None


def validate(sql, user_role="default", reg=None):
    reg = reg or load_registry()
    errors = []

    # --- parse ---
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception as e:
        return ValidationResult(False, errors=[f"parse error: {e}"])
    if tree is None:
        return ValidationResult(False, errors=["empty statement"])

    # --- (6) SELECT-only ---
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return ValidationResult(False, errors=[f"forbidden statement type: {type(node).__name__}"])
    root = tree
    if isinstance(root, exp.Subquery):
        root = root.this
    if not isinstance(root, (exp.Select, exp.Union)):
        return ValidationResult(False, errors=["only SELECT statements are allowed"])

    # --- (1) resolve tables: bare identifiers must be registered gold tables;
    #          read_parquet must point at gold/; everything else rejected ---
    ref_tables = set()
    # table-valued / file functions
    for fn in tree.find_all(exp.Anonymous):
        fname = (fn.name or "").lower()
        if fname in BLOCKED_FUNCS:
            return ValidationResult(False, errors=[f"file-reading function not allowed: {fname}()"])
        if fname in PARQUET_FUNCS:
            arg = fn.expressions[0] if fn.expressions else None
            pathval = arg.this if isinstance(arg, exp.Literal) else (arg.name if arg else "")
            gt = _gold_table_from_path(str(pathval))
            if gt is None:
                return ValidationResult(False, errors=[f"only gold/ parquet may be read; rejected path: {pathval}"])
            ref_tables.add(gt)
    # bare table identifiers
    for tbl in tree.find_all(exp.Table):
        # skip ones that are actually function outputs (no .name)
        name = tbl.name
        if not name:
            continue
        # if this Table node wraps a parquet func it won't have a plain name; bare names land here
        if name.lower() in PARQUET_FUNCS:
            continue
        if name not in reg["tables"]:
            return ValidationResult(False, errors=[f"table not registered in Gold layer: '{name}'"])
        ref_tables.add(name)

    if not ref_tables:
        return ValidationResult(False, errors=["no Gold table referenced"])
    for t in ref_tables:
        if t not in reg["tables"]:
            return ValidationResult(False, errors=[f"table not in registry: '{t}'"])

    # union of allowed + restricted across referenced tables
    allowed_union = set()
    restricted_union = {}   # field -> needed_role
    for t in ref_tables:
        allowed_union |= reg["tables"][t]["allowed"]
        restricted_union.update(reg["tables"][t]["restricted"])

    # SELECT aliases are legal references in ORDER BY / GROUP BY
    aliases = {a.alias for a in tree.find_all(exp.Alias) if a.alias}

    # --- (3) no bare SELECT * (COUNT(*) is fine) ---
    for star in tree.find_all(exp.Star):
        parent = star.parent
        if not isinstance(parent, exp.Count):
            return ValidationResult(False, errors=["SELECT * is not allowed; list explicit allowed_fields"])

    # --- (2)(4)(5) column-level checks ---
    for col in tree.find_all(exp.Column):
        cname = col.name
        if not cname or cname in aliases:
            continue
        if cname in allowed_union:
            continue
        if cname in restricted_union:
            needed = restricted_union[cname]
            if _role_ok(user_role, needed):
                continue
            errors.append(f"field '{cname}' is restricted — requires role '{needed}' (you are '{user_role}')")
            continue
        errors.append(f"field '{cname}' is not an allowed field of {sorted(ref_tables)}")

    if errors:
        return ValidationResult(False, tables=sorted(ref_tables), errors=errors)

    # --- (7) enforce LIMIT (auto-inject / clamp) ---
    if isinstance(root, exp.Select):
        lim = root.args.get("limit")
        if lim is None:
            root.limit(DEFAULT_LIMIT, copy=False)
        else:
            try:
                n = int(lim.expression.this)
                if n > MAX_LIMIT:
                    root.limit(MAX_LIMIT, copy=False)
            except Exception:
                root.limit(DEFAULT_LIMIT, copy=False)

    return ValidationResult(True, sql=root.sql(dialect="duckdb"), tables=sorted(ref_tables))


def _connect(reg, gold_dir=GOLD_DIR):
    import duckdb
    con = duckdb.connect()
    # pin Perth so CURRENT_DATE in queries is business-correct even on UTC cloud hosts
    con.execute("SET TimeZone = 'Australia/Perth'")
    # register every gold table as a view so bare names resolve and only gold is reachable.
    # a single unreadable/column-less parquet (e.g. an empty pipeline output) must not take
    # down the whole connection — skip it and carry on so the rest of the app still works.
    for name in reg["tables"]:
        p = Path(gold_dir) / f"{name}.parquet"
        if not p.exists():
            continue
        try:
            con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{p.as_posix()}')")
        except Exception as ex:
            import sys
            print(f"[sql_validator] skipping unreadable gold table '{name}': {ex}", file=sys.stderr)
    return con


def run_query(sql, user_role="default", reg=None, gold_dir=GOLD_DIR):
    reg = reg or load_registry()
    res = validate(sql, user_role, reg)
    if not res.ok:
        return None, None, res
    con = _connect(reg, gold_dir)
    try:
        cur = con.execute(res.sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return rows, cols, res
    finally:
        con.close()


if __name__ == "__main__":
    import sys
    role = sys.argv[2] if len(sys.argv) > 2 else "default"
    rows, cols, res = run_query(sys.argv[1], role)
    if not res.ok:
        print("REJECTED:", res.errors)
    else:
        print("OK SQL:", res.sql)
        print(cols)
        for r in (rows or [])[:20]:
            print(r)
