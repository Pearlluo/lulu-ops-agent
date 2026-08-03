"""Governed automation trigger: allowlist, dry-run default, overlap refusal,
start + read-back verification. Offline — ARM/MSI HTTP is monkeypatched."""
import policy_engine as pe
from tools.trigger_tool import TriggerTool

ADMIN = "admin@yourtenant.example"


def test_trigger_is_user_gated():
    assert not pe.authorize("trigger_automation", "Admin_IT", [])
    assert not pe.authorize("trigger_automation", "Admin_IT", [], "director1@yourtenant.example")
    assert pe.authorize("trigger_automation", "Admin_IT", [], ADMIN)


class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _wire(monkeypatch, last_status="Succeeded"):
    import requests
    state = {"started": False}

    def fake_get(url, **kw):
        if "IDENTITY" in url:
            return _FakeResp({"access_token": "arm"})
        if url.endswith("/executions"):
            execs = [{"name": "lulu-refresh-old",
                      "properties": {"status": last_status, "startTime": "2026-07-30T18:00:00"}}]
            if state["started"]:
                execs.append({"name": "lulu-refresh-new",
                              "properties": {"status": "Running", "startTime": "2026-07-31T04:00:00"}})
            return _FakeResp({"value": execs})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kw):
        assert url.endswith("/start")
        state["started"] = True
        return _FakeResp({"name": "lulu-refresh-new"})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setenv("IDENTITY_ENDPOINT", "https://IDENTITY.local/msi")
    monkeypatch.setenv("IDENTITY_HEADER", "h")
    return state


def test_unknown_automation_refused(monkeypatch):
    state = _wire(monkeypatch)
    res = TriggerTool().trigger_automation("rm_rf_everything", reason="t", dry_run=False)
    assert not res.ok and state["started"] is False
    assert any("registered triggers" in c for c in res.caveats)


def test_dry_run_default_starts_nothing(monkeypatch):
    state = _wire(monkeypatch)
    res = TriggerTool().trigger_automation("lulu_refresh", reason="t")
    assert res.ok and state["started"] is False
    assert "No changes have been made" in res.summary
    assert res.data[0]["last_execution"]["status"] == "Succeeded"


def test_execute_starts_and_verifies(monkeypatch):
    state = _wire(monkeypatch)
    res = TriggerTool().trigger_automation("lulu_refresh", reason="t", dry_run=False)
    assert res.ok and state["started"] is True
    assert res.data[0]["verified_running"] is True
    assert "lulu-refresh-new" in res.summary


def test_refuses_overlapping_run(monkeypatch):
    state = _wire(monkeypatch, last_status="Running")
    res = TriggerTool().trigger_automation("lulu_refresh", reason="t", dry_run=False)
    assert not res.ok and state["started"] is False
    assert any("ALREADY RUNNING" in c for c in res.caveats)
