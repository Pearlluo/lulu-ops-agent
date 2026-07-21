"""cockpit/action_runner.py — the Repair Control Layer runner (P2).

Runs ONLY what actions.yaml whitelists. The command/args for a script action are fixed in the
YAML; the LLM and the issue stream never supply a command. Two kinds of action:

  * script action (`runs:`)  -> subprocess the named script in its `cwd` with the EXACT argv from
                                 the YAML (dry_run_args for preview, execute_args for the real run).
  * handler action (`handler:`) -> a named internal python function in _HANDLERS (read-only helpers).

SAFETY GATE: a write action (writes:true) that requires approval is REFUSED in execute mode unless
the caller passes approved=True. dry-run/preview is always allowed. No DuckDB. No shell=True.

Standalone smoke test:  python action_runner.py
"""
import re
import csv
import sys
import json
import subprocess
from pathlib import Path

try:
    import yaml
except Exception:                       # pragma: no cover
    yaml = None

COCKPIT_DIR = Path(__file__).resolve().parent
AGENT_DIR = COCKPIT_DIR.parent                       # data/agent
API_DIR = AGENT_DIR.parent / "Raw Data" / "API"      # data/Raw Data/API
EXPORT_DIR = AGENT_DIR / "logs" / "exports"
_CWD = {"api": API_DIR, "agent": AGENT_DIR}
_RUN_TIMEOUT = 300                                    # seconds; check_links live audit ~1 min


def load_actions():
    p = COCKPIT_DIR / "actions.yaml"
    if not p.exists() or yaml is None:
        return {}
    try:
        return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("actions", {}) or {}
    except Exception:
        return {}


def get_action(key):
    return load_actions().get(key)


def _tail(s, n=4000):
    s = s or ""
    return s if len(s) <= n else "…(truncated)…\n" + s[-n:]


def _parse_link_repair(stdout):
    """Pull fix counts out of check_links.py output: '[repair] KEY: would fix N, skipped M'."""
    fixed = skipped = 0
    for m in re.finditer(r"(?:would fix|fixed)\s+(\d+),\s+skipped\s+(\d+)", stdout or ""):
        fixed += int(m.group(1))
        skipped += int(m.group(2))
    return {"fixed": fixed, "skipped": skipped}


# ---------------- internal read-only handlers ----------------
def _h_rebuild_issue_registry(action, issue):
    """Re-read the issue stream + re-enrich from Gold (clears the gold cache first)."""
    sys.path.insert(0, str(COCKPIT_DIR))
    import issue_registry as ir
    try:
        ir._GOLD_CACHE.clear()
    except Exception:
        pass
    v = ir.build()
    return {"ok": True, "changed": {"issues": v.get("alert_count", 0),
                                    "entities": len(v.get("entities", {}))},
            "output": f"Issue registry rebuilt: {v.get('alert_count', 0)} open issues, "
                      f"{len(v.get('entities', {}))} entities."}


def _h_validate_affected_records(action, issue):
    """Re-count the issue's affected records from its live Gold source, vs the reported count."""
    issue = issue or {}
    reported = issue.get("affected_count")
    src = (issue.get("evidence_source") or "").strip()
    live = None
    detail = ""
    try:
        if src.startswith("gold:"):
            import pandas as pd
            sys.path.insert(0, str(COCKPIT_DIR))
            import issue_registry as ir
            df = ir._read_gold(src.split(":", 1)[1])
            iid = issue.get("id")
            if df is not None:
                if iid == "iss-0001" and "is_expired" in df.columns:
                    live = int((df["is_expired"] == True).sum())
                elif iid == "iss-0002" and "is_expiring_soon" in df.columns:
                    e = df[df["is_expiring_soon"] == True]
                    if "days_to_expiry" in e.columns:
                        e = e[e["days_to_expiry"] <= 7]
                    live = int(len(e))
                elif iid == "iss-0010" and "roster_date" in df.columns:
                    live = int(df["roster_date"].isna().sum())
                else:
                    live = int(len(df))
                detail = f"live source {src} -> {live:,} rows"
        else:
            ev = issue.get("evidence_items") or []
            live = len(ev)
            detail = f"mock issue — {live} evidence rows on record (no live source)"
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
    match = (reported is not None and live is not None and int(reported) == int(live))
    return {"ok": True, "changed": {"reported": reported, "live": live, "match": match},
            "output": f"Reported affected = {reported}; {detail}. "
                      + ("✓ matches live source." if match else "⚠ differs from live source — investigate.")}


def _h_export_affected_records(action, issue):
    """Write the issue's evidence rows to a CSV under logs/exports/ (a report file, not production)."""
    issue = issue or {}
    ev = issue.get("evidence_items") or []
    iid = issue.get("id", "issue")
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = EXPORT_DIR / f"{iid}_affected.csv"
    if not ev:
        out.write_text("(no evidence rows recorded for this issue)\n", encoding="utf-8")
        return {"ok": True, "changed": {"rows": 0, "path": str(out)},
                "output": f"No evidence rows; wrote placeholder {out.name}."}
    cols = []
    for r in ev:
        for k in (r.keys() if isinstance(r, dict) else []):
            if k not in cols:
                cols.append(k)
    try:
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in ev:
                w.writerow({k: r.get(k, "") for k in cols} if isinstance(r, dict) else {})
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
    return {"ok": True, "changed": {"rows": len(ev), "path": str(out)},
            "output": f"Exported {len(ev)} affected record(s) -> logs/exports/{out.name}"}


def _h_manual_task(action, issue):
    """manual_only / draft actions: produce a manual task description (logged by the caller)."""
    issue = issue or {}
    return {"ok": True, "manual": True,
            "output": "Manual task created: " + (issue.get("suggested_fix")
                                                 or action.get("description", "handle manually"))}


def _h_not_implemented(action, issue):
    return {"ok": False, "not_wired": True,
            "output": "This repair is defined but not wired to execution yet — handle manually for now."}


_HANDLERS = {
    "rebuild_issue_registry": _h_rebuild_issue_registry,
    "validate_affected_records": _h_validate_affected_records,
    "export_affected_records": _h_export_affected_records,
    "manual_task": _h_manual_task,
    "not_implemented": _h_not_implemented,
}


def run_action(action_key, mode="execute", issue=None, approved=False):
    """Run a whitelisted action. Returns a result dict; never raises.

    Result: {ok, action_key, label, mode, writes, output, changed?, returncode?, error?, refused?}
    """
    a = get_action(action_key)
    base = {"action_key": action_key, "mode": mode}
    if not a:
        return {**base, "ok": False, "error": f"unknown action '{action_key}' (not in whitelist)"}
    base["label"] = a.get("label", action_key)
    writes = bool(a.get("writes"))
    base["writes"] = writes

    modes = a.get("modes", ["execute"])
    if mode not in modes:
        return {**base, "ok": False, "error": f"mode '{mode}' not allowed for this action ({modes})"}

    # ---- the safety gate: a write action that needs approval cannot EXECUTE without approval ----
    if mode == "execute" and writes and a.get("requires_approval") and not approved:
        return {**base, "ok": False, "refused": True,
                "error": "Approval required before executing a write action. Run a dry-run, then approve."}

    # ---- handler actions (internal, read-only) ----
    if a.get("handler"):
        fn = _HANDLERS.get(a["handler"])
        if not fn:
            return {**base, "ok": False, "error": f"no handler '{a['handler']}'"}
        try:
            r = fn(a, issue)
        except Exception as ex:
            return {**base, "ok": False, "error": f"{type(ex).__name__}: {ex}"}
        return {**base, **r, "ok": r.get("ok", True)}

    # ---- script actions (subprocess, fixed argv from the YAML) ----
    runs = a.get("runs")
    if not runs:
        return {**base, "ok": False, "error": "action has neither runs: nor handler:"}
    cwd = _CWD.get(a.get("cwd", "agent"))
    script = cwd / runs
    if not script.exists():
        return {**base, "ok": False, "error": f"script not found: {script}"}
    if mode == "dry_run":
        args = a.get("dry_run_args", [])
    else:
        args = a.get("execute_args", [])
    cmd = [sys.executable, str(script)] + list(args)
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=_RUN_TIMEOUT, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {**base, "ok": False, "error": f"timed out after {_RUN_TIMEOUT}s"}
    except Exception as ex:
        return {**base, "ok": False, "error": f"{type(ex).__name__}: {ex}"}
    out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
    res = {**base, "ok": p.returncode == 0, "returncode": p.returncode,
           "cmd": " ".join([Path(cmd[1]).name] + list(args)), "output": _tail(out)}
    if runs == "check_links.py" and ("--repair" in args):
        res["changed"] = _parse_link_repair(out)
    # data_quality_sentinel exits 1 on FAIL — that's a finding, not a runner failure
    if runs == "data_quality_sentinel.py":
        res["ok"] = p.returncode in (0, 1)
    return res


if __name__ == "__main__":
    print("== actions loaded ==")
    for k, v in load_actions().items():
        print(f"  {k:26} safety={v.get('safety'):14} writes={v.get('writes')!s:5} "
              f"{'runs=' + v.get('runs', '') if v.get('runs') else 'handler=' + v.get('handler', '')}")
    print("\n== safety gate: execute repair_folder_links WITHOUT approval ==")
    print(json.dumps(run_action("repair_folder_links", mode="execute", approved=False), ensure_ascii=False, indent=2))
    print("\n== handler: rebuild_issue_registry ==")
    print(json.dumps(run_action("rebuild_issue_registry", mode="execute"), ensure_ascii=False, indent=2))
