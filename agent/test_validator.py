"""10 validation tests for sql_validator.py. Run: python test_validator.py"""
from sql_validator import validate, run_query, load_registry

reg = load_registry()
PASS = "PASS"; FAIL = "FAIL"
results = []


def check(n, desc, cond):
    results.append((cond, n, desc))
    print(f"  [{PASS if cond else FAIL}] {n}. {desc}")


print("== SQL validator tests ==")

# 1. legal SQL validates AND executes
r = validate("SELECT first_name, last_name, position_name FROM employee_profile WHERE is_active = true", reg=reg)
check(1, "legal query passes validation", r.ok)
rows, cols, rr = run_query("SELECT first_name, last_name, position_name FROM employee_profile WHERE is_active = true")
check(1.1, "legal query executes on DuckDB and returns rows", rr.ok and rows is not None and len(rows) > 0)

# 2. legal aggregate (COUNT(*) allowed despite no SELECT-* rule)
rows, cols, r = run_query("SELECT count(*) AS n FROM training_compliance WHERE is_expired = true")
check(2, "COUNT(*) aggregate allowed + executes", r.ok and rows is not None and rows[0][0] > 0)

# 3. sensitive field rejected for default role
r = validate("SELECT first_name, date_of_birth FROM employee_profile", reg=reg)
check(3, "sensitive field (date_of_birth) rejected for default role", (not r.ok) and any("date_of_birth" in e for e in r.errors))

# 4. same sensitive field ALLOWED with HR_Manager role
r = validate("SELECT first_name, date_of_birth FROM employee_profile", user_role="HR_Manager", reg=reg)
check(4, "date_of_birth allowed with HR_Manager role", r.ok)

# 5. non-existent / non-allowed field rejected
r = validate("SELECT first_name, salary FROM employee_profile", reg=reg)
check(5, "unknown field (salary) rejected", (not r.ok) and any("salary" in e for e in r.errors))

# 6. bronze access via read_parquet path rejected
r = validate("SELECT first_name FROM read_parquet('bronze/opms/employee.parquet')", reg=reg)
check(6, "read_parquet of bronze/ path rejected", not r.ok)

# 7. silver access via read_parquet rejected
r = validate("SELECT item_name FROM read_parquet('silver/flat/sp__INV-Stores.parquet')", reg=reg)
check(7, "read_parquet of silver/ path rejected", not r.ok)

# 8. unregistered bare table rejected
r = validate("SELECT id FROM opms_employee", reg=reg)
check(8, "unregistered table name rejected", (not r.ok) and any("not registered" in e or "not in registry" in e for e in r.errors))

# 9. SELECT * rejected
r = validate("SELECT * FROM employee_profile", reg=reg)
check(9, "SELECT * rejected", (not r.ok) and any("SELECT *" in e for e in r.errors))

# 10. DML rejected (DELETE / UPDATE / DROP)
r1 = validate("DELETE FROM employee_profile WHERE opms_employee_id = 1", reg=reg)
r2 = validate("UPDATE employee_profile SET is_active = false", reg=reg)
r3 = validate("DROP TABLE employee_profile", reg=reg)
check(10, "DELETE/UPDATE/DROP all rejected", (not r1.ok) and (not r2.ok) and (not r3.ok))

# bonus A: LIMIT auto-injected when missing
r = validate("SELECT first_name FROM employee_profile", reg=reg)
check("A", "LIMIT auto-injected when missing", r.ok and "LIMIT 100" in r.sql.upper().replace("\n", " "))

# bonus B: commercial rate gated by Finance role
rd = validate("SELECT rate_title, day_shift_rate FROM rate_card", reg=reg)
rf = validate("SELECT rate_title, day_shift_rate FROM rate_card", user_role="Finance", reg=reg)
check("B", "rate hidden for default, shown for Finance", (not rd.ok) and rf.ok)

print()
ok = sum(1 for c, *_ in results if c)
print(f"== {ok}/{len(results)} checks passed ==")
if ok != len(results):
    print("FAILURES:")
    for c, n, d in results:
        if not c:
            print(f"  {n}: {d}")
