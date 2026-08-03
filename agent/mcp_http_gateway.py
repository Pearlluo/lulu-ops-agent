"""
LuLu MCP HTTP Gateway — remote, multi-user MCP with unforgeable identity.

    AI client -> HTTPS (Bearer = Entra access token for api://<gateway-app>)
             -> JWT validated (signature via tenant JWKS, audience, expiry)
             -> identity = token UPN resolved through identity.py
                          (users.yaml role > Entra groups > default)
                + groups claim from the token merged in
             -> per-request context (STATELESS transport: no session reuse,
                no identity bleed between users)
             -> same 3-layer chain as stdio: policy -> validator -> guard
             -> audit (user, client app id, tool, redactions)

Getting a token (az CLI is pre-authorized on the gateway app):
    az account get-access-token --resource api://00000000-0000-4000-a000-000000000001

Env:
    LULU_GATEWAY_TENANT     Entra tenant id (falls back to SHAREPOINT_TENANT_ID)
    LULU_GATEWAY_AUDIENCE   gateway app id (default 00000000-…)
    PORT                    listen port (default 8080)

Claude Desktop / claude.ai custom-connector OAuth (MCP spec auth flow):
    401 responses carry WWW-Authenticate pointing at RFC 9728 protected-resource
    metadata, which names Entra as the authorization server. Claude discovers
    Entra via OIDC metadata, signs the user in with PKCE using the dedicated
    public client app (no secret), and retries with the Bearer token. Entra
    does not support Dynamic Client Registration, so the connector must be
    configured with that client id (see runbooks/mcp_test_runbook.md).
"""
import json
import os
import sys
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_DIR))

import jwt as pyjwt                                       # noqa: E402
from jwt import PyJWKClient                               # noqa: E402
from starlette.responses import JSONResponse              # noqa: E402

import mcp_server                                         # noqa: E402
from identity import resolve_identity                     # noqa: E402

TENANT = os.getenv("LULU_GATEWAY_TENANT", os.getenv("SHAREPOINT_TENANT_ID", ""))
AUDIENCE = os.getenv("LULU_GATEWAY_AUDIENCE", "00000000-0000-4000-a000-000000000001")
VALID_AUDIENCES = [AUDIENCE, f"api://{AUDIENCE}"]
VALID_ISSUERS = [f"https://sts.windows.net/{TENANT}/",                 # v1 tokens (az CLI)
                 f"https://login.microsoftonline.com/{TENANT}/v2.0"]   # v2 tokens

_jwks_client = None

# ---- OAuth discovery + Entra proxy ------------------------------------------
# MCP clients (Claude Desktop/claude.ai) follow RFC 9728/8414 discovery and send
# an RFC 8707 `resource` parameter. Entra rejects `resource` alongside api://
# scopes (AADSTS 9010010), so the gateway fronts Entra as the authorization
# server: /oauth/authorize and /oauth/token pass requests through UNCHANGED
# except for dropping `resource`. /oauth/register fakes Dynamic Client
# Registration by always answering with the dedicated public client app, so
# connectors need no manually-entered client id. Tokens are minted by Entra —
# the gateway never sees credentials and validation below is unchanged.
WELL_KNOWN_PRM = "/.well-known/oauth-protected-resource"
WELL_KNOWN_AS = "/.well-known/oauth-authorization-server"
CLAUDE_CLIENT_ID = os.getenv("LULU_OAUTH_CLIENT_ID", "00000000-0000-4000-a000-000000000002")
SCOPES = [f"api://{AUDIENCE}/user_impersonation", "openid", "profile", "offline_access"]
CORS_HEADERS = {"Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type, mcp-protocol-version"}


def _public_base(headers):
    """Public origin of this gateway, derived from the (proxied) request."""
    proto = headers.get("x-forwarded-proto", "https")
    host = headers.get("host", "localhost")
    return f"{proto}://{host}"


def protected_resource_metadata(base):
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": SCOPES,
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata(base):
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "scopes_supported": SCOPES,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def entra_authorize_redirect(query_string):
    """Rebuild the authorize URL for Entra, dropping params it rejects."""
    from urllib.parse import parse_qsl, urlencode
    params = [(k, v) for k, v in parse_qsl(query_string, keep_blank_values=True)
              if k != "resource"]
    keys = {k for k, _ in params}
    if "client_id" not in keys:
        params.append(("client_id", CLAUDE_CLIENT_ID))
    if "scope" not in keys:
        params.append(("scope", " ".join(SCOPES)))
    return (f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
            f"?{urlencode(params)}")


def entra_token_exchange(form_body):
    """Forward a token request to Entra minus `resource`; -> (status, json_text)."""
    from urllib.parse import parse_qsl, urlencode
    import requests
    params = [(k, v) for k, v in parse_qsl(form_body.decode("utf-8"), keep_blank_values=True)
              if k != "resource"]
    if "client_id" not in {k for k, _ in params}:
        params.append(("client_id", CLAUDE_CLIENT_ID))
    r = requests.post(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
                      data=urlencode(params),
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      timeout=30)
    return r.status_code, r.text


def register_client(body):
    """Fake DCR (RFC 7591): every registration resolves to the dedicated app.
    The response must ECHO the client's submitted metadata (redirect_uris etc.)
    — clients validate that; returning only client_id makes them abort."""
    try:
        requested = json.loads(body or b"{}")
        if not isinstance(requested, dict):
            requested = {}
    except Exception:
        requested = {}
    resp = dict(requested)
    resp.update({
        "client_id": CLAUDE_CLIENT_ID,
        "client_id_issued_at": int(time.time()),
        "token_endpoint_auth_method": "none",
        "grant_types": requested.get("grant_types") or ["authorization_code", "refresh_token"],
        "response_types": requested.get("response_types") or ["code"],
    })
    return resp


def _unauthorized(detail, headers):
    """401 with the RFC 9728 discovery pointer (MCP spec auth flow)."""
    return JSONResponse(
        {"error": detail}, status_code=401,
        headers={"WWW-Authenticate":
                 f'Bearer resource_metadata="{_public_base(headers)}{WELL_KNOWN_PRM}/mcp"'})


def _jwks():
    global _jwks_client
    if _jwks_client is None:
        # discovery works for both v1/v2 signing keys
        _jwks_client = PyJWKClient(
            f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys",
            cache_keys=True)
    return _jwks_client


def validate_token(token):
    """-> claims dict; raises on any validation failure (fail closed)."""
    key = _jwks().get_signing_key_from_jwt(token).key
    claims = pyjwt.decode(token, key, algorithms=["RS256"],
                          audience=VALID_AUDIENCES,
                          options={"verify_iss": False})   # issuer checked manually (v1/v2)
    if claims.get("iss") not in VALID_ISSUERS:
        raise pyjwt.InvalidIssuerError(f"issuer {claims.get('iss')} not accepted")
    if claims.get("tid") != TENANT:
        raise pyjwt.InvalidTokenError("wrong tenant")
    return claims


def identity_from_claims(claims):
    """Merge token identity with the platform identity chain."""
    upn = (claims.get("upn") or claims.get("preferred_username")
           or claims.get("unique_name") or "").lower()
    ident = resolve_identity(upn) if upn else {"upn": None, "role": "default",
                                               "groups": [], "source": "token-no-upn"}
    token_groups = claims.get("groups") or []
    ident["groups"] = sorted(set(ident["groups"]) | set(token_groups))
    ident["source"] = f"entra-token+{ident['source']}"
    client = f"entra:{claims.get('azp') or claims.get('appid') or 'unknown'}"
    return ident, client


class AuthMiddleware:
    """Pure-ASGI middleware (no task boundary): validates Bearer, sets the
    per-request identity/client contextvars BEFORE the MCP app runs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope["path"] == "/healthz"
                or scope["path"].startswith((WELL_KNOWN_PRM, WELL_KNOWN_AS, "/oauth/"))
                or scope.get("method") == "OPTIONS"):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return await _unauthorized("missing bearer token", headers)(scope, receive, send)
        try:
            claims = validate_token(auth.split(None, 1)[1])
        except Exception as e:
            return await _unauthorized(f"token rejected: {type(e).__name__}",
                                       headers)(scope, receive, send)
        ident, client = identity_from_claims(claims)
        mcp_server.set_request_context(ident, client)
        return await self.app(scope, receive, send)


class GatewayApp:
    """Pure-ASGI dispatcher: /healthz open, /mcp (with or without trailing
    slash) straight into the stateless MCP session manager — no redirects,
    which would break Authorization handling in some clients."""

    def __init__(self):
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        self.manager = StreamableHTTPSessionManager(app=mcp_server.app,
                                                    json_response=True, stateless=True)
        self._run_ctx = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    self._run_ctx = self.manager.run()
                    await self._run_ctx.__aenter__()
                    try:                                   # warm gold mirror (cloud only)
                        from blob_gold import pull_gold
                        pull_gold(force=True)
                    except Exception:
                        pass
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    if self._run_ctx is not None:
                        await self._run_ctx.__aexit__(None, None, None)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if method == "OPTIONS":                    # CORS preflight for OAuth endpoints
            from starlette.responses import Response
            return await Response(status_code=204, headers=CORS_HEADERS)(scope, receive, send)
        if path.startswith(WELL_KNOWN_PRM):        # public: OAuth discovery (RFC 9728)
            return await JSONResponse(protected_resource_metadata(_public_base(headers)),
                                      headers=CORS_HEADERS)(scope, receive, send)
        if path.startswith(WELL_KNOWN_AS) or path.startswith("/.well-known/openid-configuration"):
            return await JSONResponse(authorization_server_metadata(_public_base(headers)),
                                      headers=CORS_HEADERS)(scope, receive, send)
        if path == "/oauth/authorize":             # browser redirect -> Entra (resource dropped)
            from starlette.responses import RedirectResponse
            url = entra_authorize_redirect(scope.get("query_string", b"").decode("utf-8"))
            return await RedirectResponse(url, status_code=302)(scope, receive, send)
        if path == "/oauth/token" and method == "POST":
            import anyio
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
            status, text = await anyio.to_thread.run_sync(entra_token_exchange, body)
            from starlette.responses import Response
            return await Response(text, status_code=status, media_type="application/json",
                                  headers=CORS_HEADERS)(scope, receive, send)
        if path == "/oauth/register" and method == "POST":
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
            return await JSONResponse(register_client(body), status_code=201,
                                      headers=CORS_HEADERS)(scope, receive, send)
        if path == "/healthz":
            ok = bool(list((AGENT_DIR.parent / "gold").glob("*.parquet")))
            return await JSONResponse({"ok": True, "service": "lulu-mcp-gateway",
                                       "tables": ok})(scope, receive, send)
        if path == "/mcp" or path.startswith("/mcp/"):
            try:                                   # gold hot-reload: cheap stat when throttled,
                import anyio                       # re-sync only when the backend version moved
                from blob_gold import pull_gold_if_newer
                await anyio.to_thread.run_sync(pull_gold_if_newer)
            except Exception:
                pass                               # freshness must never break the call
            return await self.manager.handle_request(scope, receive, send)
        return await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)


def build_app():
    return AuthMiddleware(GatewayApp())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
