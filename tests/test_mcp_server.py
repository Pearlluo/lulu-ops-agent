"""MCP server surface tests — client approval gates everything, discovery is
role-filtered, dispatch re-checks authorisation, role injection is stripped."""
import asyncio

import pytest

import mcp_server
import claude_tool_definitions as ctd
import policy_engine as pe


@pytest.fixture
def as_caller(monkeypatch):
    """Configure the server-side caller: approved client + pinned role."""
    def _set(role="default", client="claude-code", user=None):
        monkeypatch.setenv("LULU_MCP_CLIENT", client)
        monkeypatch.setenv("LULU_MCP_ROLE", role)
        if user:
            monkeypatch.setenv("LULU_MCP_USER", user)
        else:
            monkeypatch.delenv("LULU_MCP_USER", raising=False)
        mcp_server._identity_cache = None                  # cache is per-process
    yield _set
    mcp_server._identity_cache = None


def test_admin_sees_every_registry_tool(as_caller):
    # Admin_IT WITHOUT a verified user sees everything except user-gated
    # write tools (allowed_users is an AND-gate on top of roles).
    as_caller(role="Admin_IT")
    tools = asyncio.run(mcp_server.list_tools())
    user_gated = {name for name, p in (pe.load_policies().get("tools") or {}).items()
                  if p.get("allowed_users")}
    assert user_gated, "expected at least one user-gated write tool"
    assert {t.name for t in tools} == {t["name"] for t in ctd.TOOL_DEFINITIONS} - user_gated


def test_write_pilot_visible_only_to_allowed_user(as_caller):
    as_caller(role="Admin_IT", user="admin@yourtenant.example")
    names = {t.name for t in asyncio.run(mcp_server.list_tools())}
    assert "submit_worker_rating" in names
    assert names == {t["name"] for t in ctd.TOOL_DEFINITIONS}


def test_default_discovery_is_filtered(as_caller):
    as_caller(role="default")
    names = {t.name for t in asyncio.run(mcp_server.list_tools())}
    assert "get_rate_card" not in names
    assert "search_employee" in names
    expected = {t["name"] for t in pe.visible_tools(ctd.TOOL_DEFINITIONS, "default")}
    assert names == expected


def test_est_avoided_cost_math():
    tokens, usd = mcp_server._est_avoided("x" * 4000)
    assert tokens == 1000
    assert usd == round(1000 / 1_000_000 * mcp_server.AVOIDED_INPUT_USD_PER_M, 6)


def test_unapproved_client_sees_nothing(as_caller):
    as_caller(role="Admin_IT", client="random-bot")
    assert asyncio.run(mcp_server.list_tools()) == []


def test_unapproved_client_cannot_call(as_caller):
    import json
    as_caller(role="Admin_IT", client="random-bot")
    out = json.loads(mcp_server._execute("search_employee", {"name": "DOE"}))
    assert out["ok"] is False and "not an approved client" in out["error"]


def test_mcp_schemas_are_objects(as_caller):
    as_caller(role="Admin_IT")
    for t in asyncio.run(mcp_server.list_tools()):
        assert t.inputSchema.get("type") == "object", t.name


def test_unknown_tool_rejected(as_caller):
    as_caller(role="Admin_IT")
    try:
        mcp_server._execute("not_a_real_tool", {})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
