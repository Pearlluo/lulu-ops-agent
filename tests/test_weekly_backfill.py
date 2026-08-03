"""weekly_timesheet backfill: OPMS entered hours with NO PPL-Rosters row yet
(the roster write-back flow lags 1-2 days) must still land in the lake as
'opms_no_bms_roster' rows, with project context from the OPMS roster feed —
otherwise the freshest days silently vanish (SH-26046 quote-vs-actual gap)."""
import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "data" / "Raw Data" / "API" / "build_silver_gold.py"


@pytest.fixture()
def bsg(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("build_silver_gold_test", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "GOLD", tmp_path)

    bms = {
        ("sys", "SYS-OpsSections"): [],
        ("sms", "SMS-Suppliers"): [{"id": 7, "Title": "MG - FullT"}],
        ("ppl", "PPL-Timesheets"): [],
        ("ppl", "PPL-People"): [{"OPMS": "896", "SupplierLookupId": 7}],
        # write-back row exists ONLY for 07-28; 07-30 hours are in OPMS but not here
        ("ppl", "PPL-Rosters"): [{
            "OPMS": "896", "Date_x0020_From": "2026-07-28", "Hours": 10,
            "First_x0020_Name": "JANE", "Last_x0020_Name": "DOE",
            "Position": "Analyst", "Project": "SH-26046 - SFT2602 Major",
            "WorkType": "DAY SHIFT", "SiteLookupId": None, "SupplierLookupId": 7,
        }],
    }
    opms = {
        "timesheet_entries": [
            {"id": "S1", "site_id": "10", "date": "2026-07-28",
             "last_modified_date": "2026-07-28T10:00:00Z",
             "entries": [{"employee": {"id": 896}, "value": 8.0}]},
            {"id": "S2", "site_id": "10", "date": "2026-07-30",
             "last_modified_date": "2026-07-30T10:00:00Z",
             "entries": [{"employee": {"id": 896}, "value": 8.5},
                         {"employee": {"id": 999}, "value": 12.0}]},
        ],
        "roster": [
            {"employee": {"id": 896, "first_name": "JANE", "last_name": "DOE"},
             "rostered_days": [{
                 "date": "2026-07-30", "position": {"name": "Analyst"},
                 "work_type": {"name": "NIGHT SHIFT"},
                 "resource_request_allocations": [
                     {"resource_request": {"project": "SH-26046 - SFT2602 Major"}}],
             }]},
            {"employee": {"id": 999, "first_name": "CRANE", "last_name": "UNIT"},
             "rostered_days": [{
                 "date": "2026-07-30", "position": {"name": "Z. CRANE 100T"},
                 "work_type": {"name": "DAY SHIFT"},
                 "resource_request_allocations": [],
             }]},
        ],
        "employee": [{"id": 896, "first_name": "JANE", "last_name": "DOE"}],
    }
    monkeypatch.setattr(mod, "read_bms", lambda m, l: bms.get((m, l), []))
    monkeypatch.setattr(mod, "read_opms", lambda n: opms.get(n, []))
    monkeypatch.setattr(mod, "_get_client_resolver",
                        lambda: ((lambda p: {"client_code": "C0060"} if p else None), None))
    return mod


def test_opms_only_day_is_backfilled(bsg):
    df = bsg.build_weekly_timesheet()
    back = df[df["work_date"] == "2026-07-30"]
    assert len(back) == 1                      # 999 is a Z. plant line -> excluded
    row = back.iloc[0]
    assert row["hours_source"] == "opms_no_bms_roster"
    assert row["actual_hours"] == 8.5
    assert row["project_name"] == "SH-26046 - SFT2602 Major"   # job-code LIKE match works
    assert row["client_name"] == "C0060"
    assert row["shift_type"] == "NS"
    assert row["supplier_name"] == "MG - FullT"
    assert row["last_name"] == "DOE"


def test_covered_day_not_duplicated(bsg):
    df = bsg.build_weekly_timesheet()
    day = df[df["work_date"] == "2026-07-28"]
    assert len(day) == 1
    assert day.iloc[0]["hours_source"] == "opms"   # OPMS actuals replace roster hours
    assert day.iloc[0]["actual_hours"] == 8.0
