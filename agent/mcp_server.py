"""
Lulu MCP server — exposes the business tool registry over the Model Context
Protocol (stdio), so approved MCP clients (Claude Code, Claude Desktop, other
approved agents) can query the Gold lake through the full governance chain:

    MCP client -> client approval (§6) -> identity resolution (§3, Entra)
               -> [L2] policy_engine (role+group tool visibility & authz)
               -> tools/* -> [L1] sql_validator -> DuckDB -> Gold
               -> [L3] output_guard (field redaction + secret scrubbing)
               -> audit log (§11) -> response

Security posture:
  * Only tools in claude_tool_definitions.SPEC exist. No SQL tool, no file access.
  * Identity: LULU_MCP_USER (UPN) resolved server-side via users.yaml + Entra
    transitive groups (identity.py). Clients can never supply role — any
    user_role in arguments is stripped. LULU_MCP_ROLE stays as legacy pin.
  * Client approval: LULU_MCP_CLIENT must be in tool_policies approved_clients,
    else every discovery/call is refused (fail closed).
  * Discovery is filtered per caller; dispatch re-checks authorisation anyway.
  * requires_confirmation tools (future write/approve actions) demand an
    explicit `_confirm: true` argument — a bare call returns a confirmation
    challenge instead of executing (§8).
  * Every call appends to logs/mcp_audit.jsonl: session, user, groups, client,
    tool, args keys, allowed/denied, rows, redactions, latency.

Run standalone:      python data/agent/mcp_server.py
Claude Code:         repo root .mcp.json registers it automatically.
"""
import contextvars
import json
import os
import sys
import time
import uuid
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_DIR))

import anyio                                   # noqa: E402
import mcp.types as types                      # noqa: E402
from mcp.server.lowlevel import Server         # noqa: E402

from tools import build_tools                  # noqa: E402
from claude_tool_definitions import TOOL_DEFINITIONS, DISPATCH   # noqa: E402
from policy_engine import authorize, visible_tools, client_approved  # noqa: E402
from output_guard import sanitize_json                           # noqa: E402
from identity import resolve_identity                            # noqa: E402

AUDIT_PATH = AGENT_DIR / "logs" / "mcp_audit.jsonl"
SESSION_ID = uuid.uuid4().hex[:12]

app = Server("lulu")
_tools = None
_identity_cache = None

# Per-request identity/client override — set by the HTTP gateway (one validated
# Entra token per request, stateless transport). When unset, falls back to the
# process-level env resolution used by the stdio server.
_request_identity = contextvars.ContextVar("lulu_request_identity", default=None)
_request_client = contextvars.ContextVar("lulu_request_client", default=None)


def set_request_context(identity, client):
    _request_identity.set(identity)
    _request_client.set(client)


def _client():
    rc = _request_client.get()
    if rc is not None:
        return rc
    return os.getenv("LULU_MCP_CLIENT", "")


def _caller():
    """Per-request identity (gateway) or process identity (stdio, one caller)."""
    ri = _request_identity.get()
    if ri is not None:
        return ri
    global _identity_cache
    if _identity_cache is None:
        _identity_cache = resolve_identity()
    return _identity_cache


def _tool_instances():
    global _tools
    if _tools is None:
        _tools = build_tools()
    return _tools


def _audit(record):
    try:
        AUDIT_PATH.parent.mkdir(exist_ok=True)
        ident = _caller()
        record.setdefault("session", SESSION_ID)
        record.setdefault("client", _client())
        record.setdefault("user", ident["upn"])
        record.setdefault("role", ident["role"])
        record.setdefault("groups", ident["groups"])
        record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass                                    # auditing must never break the call itself


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    if not client_approved(_client()):
        _audit({"event": "discovery_denied", "reason": f"client '{_client()}' not approved"})
        return []
    ident = _caller()
    return [
        types.Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["input_schema"],
        )
        for t in visible_tools(TOOL_DEFINITIONS, ident["role"], ident["groups"], ident.get("upn"))
    ]


# ---- avoided-API-cost estimate (business metric, not billing) ---------------
# When callers reach LuLu through their Claude seat (remote MCP connector), the
# reasoning tokens land on their subscription, not on a per-token API bill.
# We record what the TOOL PAYLOAD alone would have cost as API input tokens —
# a deliberately conservative floor (excludes conversation history, system
# prompt, and the model's output). chars/4 ≈ tokens; rate configurable.
AVOIDED_INPUT_USD_PER_M = float(os.getenv("LULU_AVOIDED_INPUT_USD_PER_M", "3.0"))


def _est_avoided(payload: str):
    tokens = int(len(payload) / 4)
    return tokens, round(tokens / 1_000_000 * AVOIDED_INPUT_USD_PER_M, 6)


def _deny(rec, reason, t0):
    rec.update(allowed=False, reason=reason, latency_ms=round((time.time() - t0) * 1000, 1))
    _audit(rec)
    return json.dumps({"ok": False, "error": reason})


def _execute(name: str, arguments: dict) -> str:
    t0 = time.time()
    rec = {"tool": name, "args": sorted((arguments or {}).keys())}

    if not client_approved(_client()):
        return _deny(rec, f"MCP client '{_client() or '(undeclared)'}' is not an approved client.", t0)

    ident = _caller()
    decision = authorize(name, ident["role"], ident["groups"], ident.get("upn"))
    if not decision:
        return _deny(rec, decision.reason, t0)

    # user_role and _caller_upn are server-resolved — client-supplied values are dropped
    args = {k: v for k, v in (arguments or {}).items() if k not in ("user_role", "_caller_upn")}
    if decision.policy.get("injects_caller"):
        args["_caller_upn"] = ident.get("upn")
    if decision.policy.get("requires_confirmation") and not args.pop("_confirm", False):
        return _deny(rec, f"Tool '{name}' is a {decision.policy.get('risk_level')}-risk action and "
                          "requires explicit confirmation: re-call with `_confirm: true` "
                          "after the user has approved it.", t0)
    if decision.policy.get("requires_reason") and not args.get("reason"):
        return _deny(rec, f"Tool '{name}' requires a `reason` argument for the audit trail.", t0)

    tool_key = DISPATCH.get(name)
    if tool_key is None:
        _audit({**rec, "allowed": False, "reason": "unknown tool"})
        raise ValueError(f"Unknown tool: {name}")

    fn = getattr(_tool_instances()[tool_key], name)
    result = fn(**args, user_role=ident["role"])
    payload, redactions = sanitize_json(result.to_json(max_rows=50), ident["role"])

    est_tokens, est_usd = _est_avoided(payload)
    rec.update(allowed=True, ok=result.ok, rows=result.row_count,
               redactions=redactions, audit_level=decision.policy.get("audit_level", "standard"),
               payload_tokens_est=est_tokens, est_api_cost_avoided_usd=est_usd,
               latency_ms=round((time.time() - t0) * 1000, 1))
    _audit(rec)
    return payload


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    payload = await anyio.to_thread.run_sync(_execute, name, arguments)
    return [types.TextContent(type="text", text=payload)]


async def _run():
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    anyio.run(_run)
