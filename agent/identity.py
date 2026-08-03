"""
identity.py — Entra-based identity resolution (§3/§5 of the platform spec).

Single seam for "who is calling and what may they hold": every surface (MCP
server today, HTTP gateway later, Streamlit auth already) resolves identity
through resolve_identity(). The AI model and the MCP client never supply the
role — they supply at most a user principal, and the server derives the rest.

Resolution order (first hit wins for role; groups always merged in):
  1. users.yaml explicit role (local override — today's Streamlit auth source)
  2. Microsoft Entra transitive group membership via Graph (app credentials),
     mapped through tool_policies.yaml identity.group_role_map
  3. default (least privilege)

Graph needs GroupMember.Read.All / Directory.Read.All app permission; when the
app registration lacks it (or offline), group lookup degrades to [] silently —
the platform stays least-privilege instead of failing open.
"""
import os
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parent
USERS_PATH = AGENT_DIR / "users.yaml"


def _users():
    try:
        return yaml.safe_load(open(USERS_PATH, encoding="utf-8")) or {}
    except Exception:
        return {}


def _graph_token():
    import requests
    tenant = os.getenv("SHAREPOINT_TENANT_ID")
    cid = os.getenv("SHAREPOINT_CLIENT_ID")
    sec = os.getenv("SHAREPOINT_CLIENT_SECRET")
    if not (tenant and cid and sec):
        return None
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"client_id": cid, "client_secret": sec,
              "scope": "https://graph.microsoft.com/.default",
              "grant_type": "client_credentials"}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def entra_groups(upn):
    """Transitive Entra group display-names AND object IDs for a UPN. [] on any
    failure (missing permission, offline, unknown user) — never raises.
    IDs are included because display names are not unique in the tenant (two
    ORG-Directors groups exist) — the role map pins privileged roles to IDs."""
    try:
        import requests
        tok = _graph_token()
        if not tok:
            return []
        out, url = [], (f"https://graph.microsoft.com/v1.0/users/{upn}"
                        "/transitiveMemberOf/microsoft.graph.group?$select=id,displayName&$top=999")
        while url:
            r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=20)
            if r.status_code != 200:
                return []
            j = r.json()
            for g in j.get("value", []):
                out += [v for v in (g.get("displayName"), g.get("id")) if v]
            url = j.get("@odata.nextLink")
        return out
    except Exception:
        return []


def _identity_config():
    from policy_engine import load_policies
    return load_policies().get("identity", {}) or {}


def _role_from_groups(groups):
    cfg = _identity_config()
    gmap = {k.lower(): v for k, v in (cfg.get("group_role_map") or {}).items()}
    precedence = cfg.get("role_precedence", ["Admin_IT", "Finance", "HR_Manager", "default"])
    hits = {gmap[g.lower()] for g in groups if g.lower() in gmap}
    for role in precedence:
        if role in hits:
            return role
    return None


def resolve_identity(user=None):
    """-> {upn, role, groups, source}. user: email/UPN; falls back to env.

    Precedence: users.yaml role > Entra group-mapped role > LULU_MCP_ROLE env
    (legacy pin, kept for single-user stdio setups) > 'default'."""
    upn = (user or os.getenv("LULU_MCP_USER") or "").strip().lower() or None
    groups = entra_groups(upn) if upn else []

    if upn:
        rec = _users().get(upn)
        if isinstance(rec, dict) and rec.get("role"):
            return {"upn": upn, "role": rec["role"], "groups": groups, "source": "users.yaml"}
        role = _role_from_groups(groups)
        if role:
            return {"upn": upn, "role": role, "groups": groups, "source": "entra_groups"}

    env_role = os.getenv("LULU_MCP_ROLE")
    if env_role:
        return {"upn": upn, "role": env_role, "groups": groups, "source": "env"}
    return {"upn": upn, "role": "default", "groups": groups, "source": "default"}
