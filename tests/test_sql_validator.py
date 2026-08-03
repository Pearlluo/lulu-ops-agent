"""SQL security-chain tests. Validation-only cases run anywhere;
execution cases need the Gold lake and are skipped in CI."""
import pytest

from sql_validator import load_registry, validate, run_query
from tests.conftest import requires_gold

reg = load_registry()


# ---------- validation only (offline) ----------

def test_legal_query_passes():
    r = validate(
        "SELECT first_name, last_name, position_name FROM employee_profile WHERE is_active = true",
        reg=reg,
    )
    assert r.ok, r.errors


def test_sensitive_field_rejected_for_default_role():
    r = validate("SELECT first_name, date_of_birth FROM employee_profile", reg=reg)
    assert not r.ok
    assert any("date_of_birth" in e for e in r.errors)


def test_sensitive_field_allowed_for_hr_manager():
    r = validate(
        "SELECT first_name, date_of_birth FROM employee_profile",
        user_role="HR_Manager",
        reg=reg,
    )
    assert r.ok, r.errors


def test_unknown_field_rejected():
    r = validate("SELECT first_name, salary FROM employee_profile", reg=reg)
    assert not r.ok
    assert any("salary" in e for e in r.errors)


def test_bronze_parquet_path_rejected():
    r = validate(
        "SELECT first_name FROM read_parquet('bronze/opms/employee.parquet')", reg=reg
    )
    assert not r.ok


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM employee_profile",
        "UPDATE employee_profile SET first_name = 'x'",
        "DROP TABLE employee_profile",
        "INSERT INTO employee_profile VALUES (1)",
    ],
)
def test_dml_ddl_rejected(sql):
    r = validate(sql, reg=reg)
    assert not r.ok


def test_unregistered_table_rejected():
    r = validate("SELECT * FROM information_schema.tables", reg=reg)
    assert not r.ok


# ---------- execution (needs Gold lake) ----------

@requires_gold
def test_legal_query_executes():
    rows, cols, r = run_query(
        "SELECT first_name, last_name, position_name FROM employee_profile WHERE is_active = true"
    )
    assert r.ok and rows


@requires_gold
def test_count_aggregate_executes():
    rows, cols, r = run_query(
        "SELECT count(*) AS n FROM training_compliance WHERE is_expired = true"
    )
    assert r.ok and rows and rows[0][0] > 0
