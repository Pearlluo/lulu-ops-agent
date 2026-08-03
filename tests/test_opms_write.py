"""Governed write capability (§25/§26): user-gating, dry-run default,
confirmation challenge, range refusal, source-of-truth write to PPL-RankingTX
plus idempotent OPMS average push, dual post-write verification, and
server-side caller injection. Offline — Graph and OPMS HTTP are monkeypatched."""
import json

import policy_engine as pe
from tools.opms_write_tool import (OpmsWriteTool, RANKING_FIELD,
                                   LIST_RANKING_TX, LIST_PEOPLE, LIST_JOBS)

ADMIN = "admin@yourtenant.example"


# ---------------- policy: allowed_users is an AND-gate ----------------

def test_write_tool_denied_without_upn_even_for_admin():
    assert not pe.authorize("submit_worker_rating", "Admin_IT", [])


def test_write_tool_denied_for_other_admin_user():
    assert not pe.authorize("submit_worker_rating", "Admin_IT", [], "someone.else@yourtenant.example")


def test_write_tool_allowed_for_admin_only():
    assert pe.authorize("submit_worker_rating", "Admin_IT", [], ADMIN)
    assert pe.authorize("submit_worker_rating", "Admin_IT", [], ADMIN.upper())  # case-insensitive


def test_write_tool_denied_for_admin_with_wrong_role():
    # AND semantics: user listed but role not granted -> still denied
    assert not pe.authorize("submit_worker_rating", "default", [], ADMIN)


def test_write_tool_invisible_to_other_users():
    defs = [{"name": "submit_worker_rating"}, {"name": "search_employee"}]
    visible = [t["name"] for t in pe.visible_tools(defs, "Admin_IT", [], "director1@yourtenant.example")]
    assert "submit_worker_rating" not in visible and "search_employee" in visible


# ---------------- mcp dispatch: confirmation + reason + caller injection ----------------

def _as_admin(mcp_server):
    ident = {"upn": ADMIN, "role": "Admin_IT", "groups": [], "source": "test"}
    mcp_server.set_request_context(ident, "claude-code")


def test_execute_challenges_without_confirm():
    import mcp_server
    _as_admin(mcp_server)
    try:
        out = json.loads(mcp_server._execute(
            "submit_worker_rating", {"employee_id": 896, "job_code": "SH-26036", "rating": 5}))
        assert out["ok"] is False and "_confirm" in out["error"]
    finally:
        mcp_server.set_request_context(None, None)


def test_execute_requires_reason():
    import mcp_server
    _as_admin(mcp_server)
    try:
        out = json.loads(mcp_server._execute(
            "submit_worker_rating",
            {"employee_id": 896, "job_code": "SH-26036", "rating": 5, "_confirm": True}))
        assert out["ok"] is False and "reason" in out["error"]
    finally:
        mcp_server.set_request_context(None, None)


def test_caller_upn_is_server_injected_never_client_supplied(monkeypatch):
    import mcp_server
    from tools._base import ToolResult

    captured = {}

    class Stub:
        def submit_worker_rating(self, **kw):
            captured.update(kw)
            r = ToolResult(tool="opms_write", function="submit_worker_rating", args={})
            r.ok = True
            return r

    monkeypatch.setattr(mcp_server, "_tools", {"opms_write": Stub()})
    _as_admin(mcp_server)
    try:
        mcp_server._execute("submit_worker_rating",
                            {"employee_id": 896, "job_code": "SH-26036", "rating": 5,
                             "reason": "t", "_confirm": True,
                             "_caller_upn": "attacker@evil.example"})
    finally:
        mcp_server.set_request_context(None, None)
    assert captured["_caller_upn"] == ADMIN


# ---------------- tool logic: dual write + verification ----------------

class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _wire(monkeypatch, store):
    """Fake Graph + OPMS. store: tx_rows (RankingTX), opms_value, flags."""
    import requests

    def fake_post(url, **kw):
        if "login.microsoftonline.com" in url:
            return _FakeResp({"access_token": "g"})
        if "auth.opms.com.au" in url:
            return _FakeResp({"access_token": "t"})
        if LIST_RANKING_TX in url:
            fields = kw["json"]["fields"]
            store["created_fields"] = fields
            store["tx_rows"].append({"id": "901", "fields": {"RankingData": fields["RankingData"]}})
            return _FakeResp({"id": "901", "fields": fields})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, **kw):
        if "api.opms.com.au/employee/" in url:
            return _FakeResp({"first_name": "DOE", "last_name": "JANE",
                              RANKING_FIELD: store["opms_value"]})
        if "/items/901" in url:
            return _FakeResp({"id": "901",
                              "fields": {"RankingData": store["created_fields"]["RankingData"]}})
        if LIST_PEOPLE in url and "WorkEmail" in url:
            if store.get("sup_found", True):
                return _FakeResp({"value": [{"id": "77", "fields": {"Title": "DOE JANE"}}]})
            return _FakeResp({"value": []})
        if LIST_PEOPLE in url:
            return _FakeResp({"value": [{"id": "291",
                                         "fields": {"Title": "DOE JANE", "OPMS": 896.0}}]})
        if LIST_JOBS in url:
            if store.get("job_exists", True):
                return _FakeResp({"value": [{"id": "333",
                                             "fields": {"JobID": "SH-26036", "Title": "SFT2601 Major"}}]})
            return _FakeResp({"value": []})
        if LIST_RANKING_TX in url:
            return _FakeResp({"value": list(store["tx_rows"])})
        raise AssertionError(f"unexpected GET {url}")

    def fake_patch(url, **kw):
        store["opms_value"] = kw["json"][RANKING_FIELD]
        store["opms_patched"] = True
        return _FakeResp({})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "patch", fake_patch)
    for k in ("OPMS_CLIENT_ID", "OPMS_CLIENT_SECRET", "SHAREPOINT_TENANT_ID",
              "SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET"):
        monkeypatch.setenv(k, "x")


def _store(**over):
    base = {"tx_rows": [{"id": "1", "fields": {"RankingData": "3"}},
                        {"id": "2", "fields": {"RankingData": "4,5"}}],
            "opms_value": "4.00", "opms_patched": False}
    base.update(over)
    return base


def test_dry_run_default_changes_nothing(monkeypatch):
    store = _store()
    _wire(monkeypatch, store)
    res = OpmsWriteTool().submit_worker_rating(896, "SH-26036", 4, reason="test",
                                               _caller_upn=ADMIN)
    assert res.ok and store["opms_patched"] is False and len(store["tx_rows"]) == 2
    assert "No changes have been made" in res.summary
    # prior scores 3,4,5 plus new 4 -> projected average 4.00
    assert res.data[0]["projected_opms_average"] == "4.00"
    assert res.data[0]["prior_score_count"] == 3


def test_real_write_creates_tx_and_pushes_recomputed_average(monkeypatch):
    store = _store()
    _wire(monkeypatch, store)
    res = OpmsWriteTool().submit_worker_rating(896, "SH-26036", 5, comments="solid",
                                               reason="test", dry_run=False,
                                               _caller_upn=ADMIN)
    assert res.ok and store["opms_patched"] is True
    f = store["created_fields"]
    assert f["PersonLookupId"] == 291 and f["JobLookupId"] == 333
    assert f["RankingData"] == "5" and f["Comments"] == "solid"
    assert f["SupervisorLookupId"] == 77          # resolved from the injected caller UPN
    # scores 3,4,5 + new 5 -> 4.25 pushed to OPMS
    assert store["opms_value"] == "4.25"
    assert res.data[0]["verified"] is True and "PASSED" in res.summary


def test_supervisor_left_blank_when_caller_unmatched(monkeypatch):
    store = _store(sup_found=False)
    _wire(monkeypatch, store)
    res = OpmsWriteTool().submit_worker_rating(896, "SH-26036", 5, reason="test",
                                               dry_run=False, _caller_upn="ghost@x")
    assert res.ok and "SupervisorLookupId" not in store["created_fields"]
    assert any("Supervisor left blank" in c for c in res.caveats)


def test_out_of_range_and_fractional_ratings_refused(monkeypatch):
    store = _store()
    _wire(monkeypatch, store)
    tool = OpmsWriteTool()
    for bad in (0, 6, 4.5):
        res = tool.submit_worker_rating(896, "SH-26036", bad, reason="t", dry_run=False)
        assert not res.ok and any("range" in c or "whole number" in c for c in res.caveats)
    assert store["opms_patched"] is False and len(store["tx_rows"]) == 2


def test_unknown_job_code_refused(monkeypatch):
    store = _store(job_exists=False)
    _wire(monkeypatch, store)
    res = OpmsWriteTool().submit_worker_rating(896, "XX-99999", 5, reason="t", dry_run=False)
    assert not res.ok and any("JMS-Jobs" in c for c in res.caveats)
    assert store["opms_patched"] is False and len(store["tx_rows"]) == 2
