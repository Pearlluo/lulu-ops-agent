"""Quote hours tool: Hours-Remaining-parity math (qty x manning shift hours),
newest-manned-version pick, job id normalisation, no-rates output.
Offline — the blob index loader is stubbed."""
from tools.quote_tool import QuoteTool, _normalise_job_id


def _payload():
    return {
        "shift_types": [{"desc": "DS12.5", "hrs": 12.5, "type": "DS"},
                        {"desc": "NS12", "hrs": 12, "type": "NS"}],
        "staff_roles": [{"id": "r1", "role": "Supervisor", "qty": 1, "dsRate": 999},
                        {"id": "r2", "role": "Trades Assistant", "qty": 4}],
        "new_roles": [],
        "manning": {"r1": {"2026-08-01": "DS12.5", "2026-08-02": "DS12.5", "2026-08-03": "OFF"},
                    "r2": {"2026-08-01": "DS12.5", "2026-08-02": "NS12"}},
        "manning_dates": ["2026-08-01", "2026-08-02", "2026-08-03"],
        "data_input": {"start_date": "2026-08-01", "end_date": "2026-08-03"},
    }


def _stub(monkeypatch, index):
    monkeypatch.setattr(QuoteTool, "_load_index", lambda self: index)


def test_job_id_normalisation():
    assert _normalise_job_id("26046") == "SH-26046"
    assert _normalise_job_id(" sh-26046 ") == "SH-26046"


def test_quote_hours_math(monkeypatch):
    _stub(monkeypatch, {"SH-26046": [
        {"uploaded_at": "2026-07-24", "job_title": "SFT2602 Major",
         "client_business_name": "Ironbridge", "payload": _payload()}]})
    res = QuoteTool().get_quote_hours("26046")
    assert res.ok
    by_role = {r["role"]: r for r in res.data}
    # Supervisor: 2 DS days x 12.5 x qty 1 = 25
    assert by_role["Supervisor"]["total_hours"] == 25.0
    # TA: (12.5 DS + 12 NS) x qty 4 = 98, split 50 DS / 48 NS
    assert by_role["Trades Assistant"]["ds_hours"] == 50.0
    assert by_role["Trades Assistant"]["ns_hours"] == 48.0
    assert "123.0 quoted hours" in res.summary
    # rates must never leak into the output
    import json
    assert "dsRate" not in json.dumps(res.data) and "999" not in json.dumps(res.data)


def test_picks_newest_version_with_manning(monkeypatch):
    empty = {"payload": {"manning": {}, "staff_roles": [], "shift_types": []},
             "uploaded_at": "2026-07-25", "job_title": "newer but empty"}
    _stub(monkeypatch, {"SH-26046": [
        {"uploaded_at": "2026-07-24", "job_title": "manned", "payload": _payload()},
        empty]})
    res = QuoteTool().get_quote_hours("SH-26046")
    assert res.ok and "manned" in res.summary and "2 saved version" in res.summary


def test_unknown_job_lists_available(monkeypatch):
    _stub(monkeypatch, {"SH-11111": [{"uploaded_at": "x", "payload": _payload()}]})
    res = QuoteTool().get_quote_hours("SH-99999")
    assert not res.ok and any("SH-11111" in c for c in res.caveats)
