"""
policy_engine.py — server-side tool authorisation (layer 2 of the security chain).

Single decision point: every surface that exposes tools (MCP server, future HTTP
gateway, app runners) asks THIS module two questions —
    visible_tools(role)          which tool definitions may this role even see?
    authorize(fn, role)          may this role call fn right now?
The AI model never participates in the decision; it only sees the already-filtered
catalogue, and a second authorize() check runs at dispatch time regardless of what
the client claims (defence against clients calling hidden tools by name).
"""
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parent
POLICY_PATH = AGENT_DIR / "tool_policies.yaml"

_policies_cache = None

WRITE_RISKS = {"export", "create", "update", "approve", "delete"}


def load_policies(force=False):
    global _policies_cache
    if _policies_cache is None or force:
        _policies_cache = yaml.safe_load(open(POLICY_PATH, encoding="utf-8"))
    return _policies_cache


def known_roles():
    return list(load_policies().get("roles", []))


def policy_for(fn_name):
    """Effective policy for one tool function (explicit entry merged over defaults)."""
    p = load_policies()
    merged = dict(p.get("defaults", {}))
    merged.update((p.get("tools") or {}).get(fn_name, {}))
    return merged


class AuthzResult:
    def __init__(self, allowed, reason="", policy=None):
        self.allowed = allowed
        self.reason = reason
        self.policy = policy or {}

    def __bool__(self):
        return self.allowed


def client_approved(client):
    """§6: unknown/undeclared AI clients are rejected before any tool logic."""
    approved = load_policies().get("approved_clients") or []
    return bool(client) and client in approved


def authorize(fn_name, role, groups=None, upn=None) -> AuthzResult:
    """May `role` (holding Entra `groups`, signed in as `upn`) call `fn_name`?
    Deny-with-reason, never raise.

    allowed_roles and allowed_groups compose as OR-of-grants within a tool entry:
    a caller passes if their role is listed OR one of their groups is listed —
    but a tool that lists ONLY groups requires a group hit.
    allowed_users is an ADDITIONAL mandatory gate (AND): when present, the
    caller's UPN must be listed no matter how privileged their role is — this
    is how write pilots stay restricted to named individuals (§25)."""
    role = role or "default"
    groups = set(g.lower() for g in (groups or []))
    pol = policy_for(fn_name)
    allowed_roles = pol.get("allowed_roles", "all")
    allowed_groups = [g.lower() for g in pol.get("allowed_groups", [])]
    allowed_users = [u.lower() for u in pol.get("allowed_users", [])]
    if pol.get("risk_level", "read") in WRITE_RISKS and fn_name not in (load_policies().get("tools") or {}):
        return AuthzResult(False, f"'{fn_name}' is write-risk but has no explicit policy entry", pol)
    role_ok = allowed_roles == "all" or role in allowed_roles
    group_ok = bool(allowed_groups) and bool(groups & set(allowed_groups))
    if not (role_ok or group_ok):
        need = f"one of roles {allowed_roles}" if allowed_roles != "all" else ""
        if allowed_groups:
            need += (" or " if need else "") + f"membership of {pol.get('allowed_groups')}"
        return AuthzResult(False, f"Tool '{fn_name}' requires {need}; caller role is '{role}'.", pol)
    if allowed_users and (upn or "").lower() not in allowed_users:
        return AuthzResult(False, f"Tool '{fn_name}' is user-gated; caller "
                                  f"'{upn or '(no verified user)'}' is not in allowed_users.", pol)
    return AuthzResult(True, "ok", pol)


def visible_tools(tool_definitions, role, groups=None, upn=None):
    """Filter a tool-definition list (claude_tool_definitions format) for a caller.
    Unauthorised tools are absent from discovery — not just blocked at call time."""
    return [t for t in tool_definitions if authorize(t["name"], role, groups, upn)]
