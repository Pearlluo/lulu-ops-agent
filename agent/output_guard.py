"""
output_guard.py — value-level output filtering (layer 3 of the security chain).

Runs AFTER a tool executes and BEFORE the result reaches any AI client. System
prompts asking the model to "not reveal sensitive data" are advice; this is
enforcement. Two passes:

  1. restricted fields — field values the caller's role may not see become
     "[REDACTED:<field>]" (roles per tool_policies.yaml output_policy;
     an empty role list means nobody sees the value over this channel)
  2. secret patterns — connection strings, API keys, bearer/JWT tokens are
     scrubbed for EVERY role, in every string, including free text pulled
     from documents (a doc that embeds a key must not leak it through RAG)

Returns (sanitized_payload, actions) — actions feed the audit log so every
redaction is attributable. Fail-safe: a guard crash redacts nothing silently —
it raises, because silently passing unfiltered output is the worse failure.
"""
import json
import re

from policy_engine import load_policies

_compiled = None


def _rules():
    global _compiled
    if _compiled is None:
        op = load_policies().get("output_policy", {})
        _compiled = (
            [re.compile(p) for p in op.get("redact_always_patterns", [])],
            {k.lower(): v for k, v in (op.get("restricted_fields") or {}).items()},
        )
    return _compiled


def _scrub_secrets(text, patterns, actions):
    for rx in patterns:
        if rx.search(text):
            text = rx.sub("[REDACTED:secret]", text)
            actions.append({"action": "scrub_secret", "pattern": rx.pattern[:40]})
    return text


def sanitize(payload, role):
    """payload: dict (parsed ToolResult JSON) or str. Returns (payload, actions)."""
    patterns, fields = _rules()
    role = role or "default"
    actions = []

    if isinstance(payload, str):
        return _scrub_secrets(payload, patterns, actions), actions

    def walk(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                allowed = fields.get(str(k).lower())
                if allowed is not None and role not in allowed:
                    out[k] = f"[REDACTED:{k}]"
                    actions.append({"action": "redact_field", "field": k})
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            return _scrub_secrets(node, patterns, actions)
        return node

    return walk(payload), actions


def sanitize_json(json_str, role):
    """Convenience for ToolResult.to_json() strings; keeps them as JSON strings."""
    try:
        obj = json.loads(json_str)
    except json.JSONDecodeError:
        cleaned, actions = sanitize(json_str, role)
        return cleaned, actions
    obj, actions = sanitize(obj, role)
    return json.dumps(obj, ensure_ascii=False, default=str), actions
