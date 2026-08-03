"""Capability registry + project_hours_status canonical-logic tests."""
import pytest

from capabilities import load_capability_registry
from tests.conftest import requires_gold

REQUIRED_FIELDS = ("display_name", "business_purpose", "business_owner",
                   "implementation", "authoritative_source", "canonical_logic",
                   "data_classification", "risk_level", "v_version",
                   "effective_date", "retirement_status")

UNIFIED_KEYS = ("facts", "warnings", "exceptions", "possible_causes",
                "recommended_actions", "required_approvals", "data_freshness",
                "sources", "confidence", "insufficient_data")


def test_registry_parses_with_required_fields():
    reg = load_capability_registry(force=True)
    caps = reg.get("capabilities") or {}
    assert caps, "no capabilities registered"
    for name, c in caps.items():
        for f in REQUIRED_FIELDS:
            assert c.get(f) is not None, f"capability {name} missing {f}"


def test_exposed_tools_exist_in_spec():
    import claude_tool_definitions as ctd
    spec_fns = {fn for _t, fn, _d, _p in ctd.SPEC}
    reg = load_capability_registry()
    for name, c in reg["capabilities"].items():
        exposed = c.get("exposed_as_tool")
        exposed = exposed if isinstance(exposed, list) else [exposed]
        for fn in exposed:
            assert fn in spec_fns, f"{name} exposes unknown tool '{fn}'"


def test_invalid_job_ref_rejected_offline():
    from capabilities.project_hours_status import _REF_RX
    assert not _REF_RX.match("SH-26046'; DROP TABLE x--")
    assert _REF_RX.match("SH-26046")


@requires_gold
def test_sh26046_unified_shape_and_status_discipline():
    from capabilities.project_hours_status import compute

    r = compute("SH-26046")
    for k in UNIFIED_KEYS:
        assert k in r, f"missing unified key {k}"
    assert r["facts"], "no facts returned"
    # status discipline: quote & invoiced must be declared insufficient, not faked
    joined = " ".join(r["insufficient_data"])
    assert "quote" in joined.lower() and "invoiced" in joined.lower()
    assert r["data_freshness"].get("roster_max_date")


@requires_gold
def test_unknown_job_graceful():
    from capabilities.project_hours_status import compute

    r = compute("SH-99999")
    assert r["exceptions"] and r["confidence"] == "low"


@requires_gold
def test_tool_wrapper_returns_toolresult():
    from tools.capability_tool import CapabilityTool

    res = CapabilityTool().project_hours_status("SH-26046")
    assert res.ok and res.row_count == 1
    assert res.caveats, "caveats (warnings + insufficient_data) must surface"
