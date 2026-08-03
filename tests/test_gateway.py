"""HTTP gateway auth-layer tests — claim mapping, per-request identity
override, and fail-closed behaviour. Offline (JWT validation monkeypatched
where a real Entra token would be needed)."""
import mcp_server
import mcp_http_gateway as gw


def test_identity_from_claims_merges_token_groups(monkeypatch):
    monkeypatch.setattr(gw, "resolve_identity",
                        lambda upn: {"upn": upn, "role": "default",
                                     "groups": ["Existing"], "source": "users.yaml"})
    ident, client = gw.identity_from_claims({
        "upn": "someone@yourtenant.example",
        "groups": ["Project-2317"], "azp": "00000000-0000-4000-a000-000000000007"})
    assert ident["upn"] == "someone@yourtenant.example"
    assert set(ident["groups"]) == {"Existing", "Project-2317"}
    assert client == "entra:00000000-0000-4000-a000-000000000007"
    assert ident["source"].startswith("entra-token+")


def test_request_context_overrides_process_identity():
    ident = {"upn": "x@yourtenant.example", "role": "Finance", "groups": [], "source": "test"}
    mcp_server.set_request_context(ident, "entra:test-client")
    try:
        assert mcp_server._caller() == ident
        assert mcp_server._client() == "entra:test-client"
    finally:
        mcp_server.set_request_context(None, None)


def test_unapproved_entra_client_denied(monkeypatch):
    import json
    ident = {"upn": "x@yourtenant.example", "role": "Admin_IT", "groups": [], "source": "test"}
    mcp_server.set_request_context(ident, "entra:not-approved-appid")
    try:
        out = json.loads(mcp_server._execute("search_employee", {"name": "DOE"}))
        assert out["ok"] is False and "approved" in out["error"]
    finally:
        mcp_server.set_request_context(None, None)


def test_protected_resource_metadata(monkeypatch):
    monkeypatch.setattr(gw, "TENANT", "tenant-x")
    md = gw.protected_resource_metadata("https://gw.example")
    assert md["resource"] == "https://gw.example/mcp"
    assert f"api://{gw.AUDIENCE}/user_impersonation" in md["scopes_supported"]


def test_401_carries_discovery_header():
    resp = gw._unauthorized("missing bearer token",
                            {"host": "gw.example", "x-forwarded-proto": "https"})
    assert resp.status_code == 401
    www = resp.headers["www-authenticate"]
    assert www.startswith("Bearer resource_metadata=")
    assert "https://gw.example/.well-known/oauth-protected-resource/mcp" in www


def test_authorize_proxy_drops_resource_param(monkeypatch):
    """Entra rejects RFC 8707 `resource` with api:// scopes (AADSTS 9010010) —
    the proxy must strip it and keep everything else intact."""
    monkeypatch.setattr(gw, "TENANT", "tenant-x")
    url = gw.entra_authorize_redirect(
        "response_type=code&client_id=abc&redirect_uri=https%3A%2F%2Fclaude.ai%2Fcb"
        "&state=s1&code_challenge=xyz&code_challenge_method=S256"
        "&resource=https%3A%2F%2Fgw.example%2Fmcp&scope=openid")
    assert url.startswith("https://login.microsoftonline.com/tenant-x/oauth2/v2.0/authorize?")
    assert "resource=" not in url
    assert "state=s1" in url and "code_challenge=xyz" in url and "client_id=abc" in url


def test_authorize_proxy_fills_client_and_scope():
    url = gw.entra_authorize_redirect("response_type=code&state=s2")
    assert f"client_id={gw.CLAUDE_CLIENT_ID}" in url
    assert "user_impersonation" in url


def test_token_proxy_drops_resource(monkeypatch):
    seen = {}

    class FakeResp:
        status_code = 200
        text = '{"access_token":"t"}'

    def fake_post(url, data=None, headers=None, timeout=None):
        seen["url"], seen["data"] = url, data
        return FakeResp()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(gw, "TENANT", "tenant-x")
    status, text = gw.entra_token_exchange(
        b"grant_type=authorization_code&code=c1&resource=https%3A%2F%2Fgw%2Fmcp"
        b"&code_verifier=v1&client_id=abc")
    assert status == 200 and "access_token" in text
    assert "resource" not in seen["data"]
    assert "code_verifier=v1" in seen["data"] and "tenant-x" in seen["url"]


def test_fake_dcr_returns_dedicated_client_and_echoes_metadata():
    body = (b'{"client_name":"Claude","redirect_uris":'
            b'["https://claude.ai/api/mcp/auth_callback"],"response_types":["code"]}')
    reg = gw.register_client(body)
    assert reg["client_id"] == gw.CLAUDE_CLIENT_ID
    assert reg["token_endpoint_auth_method"] == "none"
    assert reg["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]
    assert reg["client_name"] == "Claude"
    assert "client_id_issued_at" in reg
    junk = gw.register_client(b"not json")          # malformed body must not 500
    assert junk["client_id"] == gw.CLAUDE_CLIENT_ID


def test_as_metadata_points_at_gateway_endpoints():
    md = gw.authorization_server_metadata("https://gw.example")
    assert md["issuer"] == "https://gw.example"
    assert md["authorization_endpoint"] == "https://gw.example/oauth/authorize"
    assert md["token_endpoint"] == "https://gw.example/oauth/token"
    assert md["registration_endpoint"] == "https://gw.example/oauth/register"
    assert "S256" in md["code_challenge_methods_supported"]


def test_prm_authorization_server_is_gateway():
    md = gw.protected_resource_metadata("https://gw.example")
    assert md["authorization_servers"] == ["https://gw.example"]


def test_gateway_validates_issuer(monkeypatch):
    monkeypatch.setattr(gw, "TENANT", "tenant-x")
    monkeypatch.setattr(gw, "VALID_ISSUERS", ["https://sts.windows.net/tenant-x/"])

    class FakeKey:
        key = "k"
    monkeypatch.setattr(gw, "_jwks", lambda: type("J", (), {
        "get_signing_key_from_jwt": lambda self, t: FakeKey()})())
    monkeypatch.setattr(gw.pyjwt, "decode",
                        lambda *a, **k: {"iss": "https://evil.example/", "tid": "tenant-x"})
    try:
        gw.validate_token("fake")
        raise AssertionError("expected InvalidIssuerError")
    except gw.pyjwt.InvalidIssuerError:
        pass
