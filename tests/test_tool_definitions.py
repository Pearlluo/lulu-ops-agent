"""Consistency checks between the tool SPEC, the wire-format definitions
sent to LLM providers, and the DISPATCH routing map. Fully offline."""
import claude_tool_definitions as ctd


def test_spec_not_empty():
    assert len(ctd.SPEC) > 0


def test_tool_names_unique():
    names = [t["name"] for t in ctd.TOOL_DEFINITIONS]
    assert len(names) == len(set(names)), "duplicate tool names"


def test_every_definition_has_schema():
    for t in ctd.TOOL_DEFINITIONS:
        assert t.get("name"), t
        assert t.get("description"), f"{t['name']} missing description"
        schema = t.get("input_schema")
        assert isinstance(schema, dict) and schema.get("type") == "object", (
            f"{t['name']} input_schema malformed"
        )


def test_every_tool_is_dispatchable():
    dispatch = ctd.DISPATCH
    for t in ctd.TOOL_DEFINITIONS:
        assert t["name"] in dispatch, f"{t['name']} has no DISPATCH entry"


def test_no_orphan_dispatch_entries():
    names = {t["name"] for t in ctd.TOOL_DEFINITIONS}
    orphans = [k for k in ctd.DISPATCH if k not in names]
    assert not orphans, f"DISPATCH entries without definitions: {orphans}"
