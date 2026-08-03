"""Identity resolution tests (§3/§5): users.yaml override > Entra group map >
env pin > least-privilege default. Offline — Graph calls are monkeypatched."""
import pytest

import identity


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("LULU_MCP_USER", "LULU_MCP_ROLE"):
        monkeypatch.delenv(k, raising=False)


def test_no_user_no_env_is_least_privilege(monkeypatch):
    monkeypatch.setattr(identity, "entra_groups", lambda upn: [])
    ident = identity.resolve_identity()
    assert ident["role"] == "default" and ident["source"] == "default"


def test_env_role_pin_still_works(monkeypatch):
    monkeypatch.setenv("LULU_MCP_ROLE", "Finance")
    monkeypatch.setattr(identity, "entra_groups", lambda upn: [])
    assert identity.resolve_identity()["role"] == "Finance"


def test_users_yaml_overrides_groups(monkeypatch, tmp_path):
    users = tmp_path / "users.yaml"
    users.write_text("someone@yourtenant.example: {role: HR_Manager}\n", encoding="utf-8")
    monkeypatch.setattr(identity, "USERS_PATH", users)
    monkeypatch.setattr(identity, "entra_groups", lambda upn: ["Finance-Team"])
    ident = identity.resolve_identity("someone@yourtenant.example")
    assert ident["role"] == "HR_Manager" and ident["source"] == "users.yaml"
    assert "Finance-Team" in ident["groups"]


def test_entra_group_maps_to_role(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "USERS_PATH", tmp_path / "absent.yaml")
    monkeypatch.setattr(identity, "entra_groups", lambda upn: ["Something", "ORG-Finance"])
    ident = identity.resolve_identity("new.person@yourtenant.example")
    assert ident["role"] == "Finance" and ident["source"] == "entra_groups"


def test_group_object_id_maps_to_role(monkeypatch, tmp_path):
    """JWT groups claims carry object IDs, not names — the map must hit on GUIDs."""
    monkeypatch.setattr(identity, "USERS_PATH", tmp_path / "absent.yaml")
    monkeypatch.setattr(identity, "entra_groups",
                        lambda upn: ["00000000-0000-4000-a000-000000000004"])
    ident = identity.resolve_identity("new.person@yourtenant.example")
    assert ident["role"] == "Finance" and ident["source"] == "entra_groups"


def test_unknown_user_without_groups_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "USERS_PATH", tmp_path / "absent.yaml")
    monkeypatch.setattr(identity, "entra_groups", lambda upn: [])
    assert identity.resolve_identity("stranger@example.com")["role"] == "default"


def test_group_grant_composes_with_policy(monkeypatch):
    """A tool listing allowed_groups admits a caller by group even when the
    role alone would be denied (§5 combined authorisation)."""
    import policy_engine as pe
    monkeypatch.setitem(pe.load_policies()["tools"], "get_rate_card",
                        {"allowed_roles": ["Finance"], "allowed_groups": ["Rate-Readers"]})
    try:
        assert not pe.authorize("get_rate_card", "default", [])
        assert pe.authorize("get_rate_card", "default", ["Rate-Readers"])
    finally:
        pe.load_policies(force=True)                      # restore from disk
