"""Live OPMS read tool: winning-sheet dedup (lake parity), worker/date filter,
cursor pagination, and the snapshot caveat on the lake timesheet tool.
Offline — OPMS HTTP is monkeypatched."""
from tools.opms_live_tool import OpmsLiveTool


class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _sheet(sid, date, site, modified, entries):
    return {"id": sid, "date": f"{date}T00:00:00", "site_id": site,
            "last_modified_date": modified,
            "entries": [{"employee": {"id": e}, "value": v} for e, v in entries]}


def _wire(monkeypatch, pages):
    """pages: list of {'data': [...sheets], 'next_cursor': ...}."""
    import requests
    calls = {"n": 0, "params": []}

    def fake_post(url, **kw):
        return _FakeResp({"access_token": "t"})

    def fake_get(url, **kw):
        calls["params"].append(dict(kw.get("params") or {}))
        page = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return _FakeResp(page)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setenv("OPMS_CLIENT_ID", "x")
    monkeypatch.setenv("OPMS_CLIENT_SECRET", "y")
    return calls


def test_winning_sheet_dedup_matches_lake_rule(monkeypatch):
    # duplicate sheets same day+site: only the most-recently-modified counts;
    # a second site the same day still adds up.
    _wire(monkeypatch, [{"data": [
        _sheet("s1", "2026-07-28", "10", "2026-07-28T10:00:00", [(896, 8.0)]),
        _sheet("s2", "2026-07-28", "10", "2026-07-28T12:00:00", [(896, 6.0)]),   # winner
        _sheet("s3", "2026-07-28", "20", "2026-07-28T11:00:00", [(896, 2.0)]),
    ], "next_cursor": None}])
    res = OpmsLiveTool().get_live_worker_hours(896, "2026-07-27", "2026-08-02")
    assert res.ok and res.data == [{"work_date": "2026-07-28", "hours": 8.0}]  # 6 + 2


def test_filters_other_workers_and_out_of_range_days(monkeypatch):
    _wire(monkeypatch, [{"data": [
        _sheet("s1", "2026-07-28", "10", "m1", [(896, 5.0), (291, 9.0)]),
        _sheet("s2", "2026-07-20", "10", "m1", [(896, 7.0)]),                   # before range
    ], "next_cursor": None}])
    res = OpmsLiveTool().get_live_worker_hours(896, "2026-07-27", "2026-08-02")
    assert res.data == [{"work_date": "2026-07-28", "hours": 5.0}]


def test_pagination_follows_cursor_and_timesheets_key(monkeypatch):
    # second page uses the {"timesheets": [...]} response shape OPMS also returns
    calls = _wire(monkeypatch, [
        {"data": [_sheet("s1", "2026-07-27", "10", "m1", [(896, 4.0)])], "next_cursor": "abc"},
        {"timesheets": [_sheet("s2", "2026-07-28", "10", "m1", [(896, 3.0)])], "next_cursor": None},
    ])
    res = OpmsLiveTool().get_live_worker_hours(896, "2026-07-27", "2026-08-02")
    assert [r["hours"] for r in res.data] == [4.0, 3.0]
    assert calls["n"] == 2 and calls["params"][1].get("after") == "abc"


def test_bad_worker_id_refused(monkeypatch):
    _wire(monkeypatch, [{"data": [], "next_cursor": None}])
    res = OpmsLiveTool().get_live_worker_hours("not-an-id")
    assert not res.ok and any("integer" in c for c in res.caveats)


def test_weekly_timesheet_carries_snapshot_caveat(monkeypatch):
    # no lake in CI — stub the query layer; we only verify the caveat injection
    from tools.timesheet_tool import TimesheetTool
    from tools._base import ToolResult
    assert "NIGHTLY" in TimesheetTool.SNAPSHOT_CAVEAT
    stub = ToolResult(tool="timesheet", function="get_weekly_timesheet", args={})
    monkeypatch.setattr(TimesheetTool, "_query", lambda self, *a, **k: stub)
    res = TimesheetTool().get_weekly_timesheet(date_from="2026-07-27", date_to="2026-07-28",
                                               worker_name="DOE")
    assert any("nightly lake snapshot" in c for c in res.caveats)
