"""Security-layer tests: tool-level authorisation (policy_engine), value-level
redaction (output_guard), and their wiring into the MCP server. Offline."""
import json

import policy_engine as pe
import output_guard as og
import claude_tool_definitions as ctd
import mcp_server


# ---------------- policy engine ----------------

def test_default_role_cannot_see_finance_tools():
    names = {t["name"] for t in pe.visible_tools(ctd.TOOL_DEFINITIONS, "default")}
    for hidden in ("get_rate_card", "get_client_revenue", "get_purchase_summary",
                   "get_outstanding_invoices", "get_project_revenue"):
        assert hidden not in names


def test_finance_role_sees_finance_tools():
    names = {t["name"] for t in pe.visible_tools(ctd.TOOL_DEFINITIONS, "Finance")}
    assert "get_rate_card" in names


def test_worker_ranking_is_hr_gated():
    assert not pe.authorize("get_worker_ranking", "default")
    assert not pe.authorize("get_worker_ranking", "Finance")
    assert pe.authorize("get_worker_ranking", "HR_Manager")
    assert pe.authorize("get_worker_ranking", "Admin_IT")


def test_operational_tools_open_to_all():
    for fn in ("search_employee", "get_roster_summary", "search_company_knowledge"):
        assert pe.authorize(fn, "default")


def test_deny_carries_reason():
    d = pe.authorize("get_rate_card", "default")
    assert not d and "requires" in d.reason


def test_policy_roles_match_agent_registry():
    import yaml
    reg = yaml.safe_load(open(pe.AGENT_DIR / "agent_registry.yaml", encoding="utf-8"))
    assert set(pe.known_roles()) == set(reg["roles"].keys())


# ---------------- output guard ----------------

def test_rate_fields_redacted_for_default():
    payload = {"data": [{"position": "HD Fitter", "day_rate": 120.5, "night_rate": 140.0}]}
    out, actions = og.sanitize(payload, "default")
    assert out["data"][0]["day_rate"] == "[REDACTED:day_rate]"
    assert out["data"][0]["position"] == "HD Fitter"
    assert any(a["action"] == "redact_field" for a in actions)


def test_rate_fields_kept_for_finance():
    payload = {"data": [{"day_rate": 120.5}]}
    out, actions = og.sanitize(payload, "Finance")
    assert out["data"][0]["day_rate"] == 120.5
    assert not actions


def test_nobody_fields_redacted_even_for_admin():
    out, _ = og.sanitize({"tax_file_number": "123456789"}, "Admin_IT")
    assert out["tax_file_number"] == "[REDACTED:tax_file_number]"


def test_secrets_scrubbed_for_every_role():
    text = "conn is AccountKey=abcdefghijklmnopqrstuvwxyz0123456789ABCD and more"
    for role in ("default", "Finance", "Admin_IT"):
        out, actions = og.sanitize({"summary": text}, role)
        assert "AccountKey=" not in out["summary"]
        assert "[REDACTED:secret]" in out["summary"]


def test_sanitize_json_roundtrip():
    s = json.dumps({"data": [{"day_rate": 99}], "summary": "x"})
    out, actions = og.sanitize_json(s, "default")
    assert json.loads(out)["data"][0]["day_rate"] == "[REDACTED:day_rate]"


# ---------------- MCP wiring ----------------

def _as_default_caller(monkeypatch):
    monkeypatch.setenv("LULU_MCP_CLIENT", "claude-code")
    monkeypatch.setenv("LULU_MCP_ROLE", "default")
    monkeypatch.delenv("LULU_MCP_USER", raising=False)
    mcp_server._identity_cache = None


def test_mcp_denies_hidden_tool_with_structured_error(monkeypatch):
    _as_default_caller(monkeypatch)
    out = json.loads(mcp_server._execute("get_rate_card", {}))
    assert out["ok"] is False and "requires" in out["error"]
    mcp_server._identity_cache = None


def test_mcp_role_injection_stripped(monkeypatch):
    _as_default_caller(monkeypatch)
    # client tries to smuggle an elevated role — must still be denied
    out = json.loads(mcp_server._execute("get_rate_card", {"user_role": "Finance"}))
    assert out["ok"] is False
    mcp_server._identity_cache = None
