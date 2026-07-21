"""
sync_github_registry.py — refresh automation_registry.yaml `live:` blocks from GitHub.

For every automation in the registry it pulls (via the `gh` CLI, already authenticated):
  - repo metadata: description, default branch, last push
  - .github/workflows/*.yml: file names + parsed triggers (push / schedule / workflow_dispatch)
    + the Azure app each workflow deploys to (app-name in azure/webapps-deploy / functions-action)
  - latest workflow run: status, conclusion, when, url

`business:` blocks are never touched — they are hand-maintained knowledge.
Run:  python sync_github_registry.py        (whole registry, ~20s)
      python sync_github_registry.py --repo acme_weeklytimesheet_automation
"""

import argparse
import base64
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

AGENT_DIR = Path(__file__).resolve().parent
REGISTRY = AGENT_DIR / "automation_registry.yaml"


def gh(args, timeout=30, raw=False):
    """Run a gh CLI command; return parsed JSON (or raw text when raw=True), None on failure."""
    try:
        out = subprocess.run(["gh"] + args, capture_output=True, text=True,
                             timeout=timeout, encoding="utf-8", errors="replace")
        if out.returncode != 0:
            return None
        if raw:
            return out.stdout
        return json.loads(out.stdout) if out.stdout.strip() else None
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None


def parse_workflow_triggers(yml_text):
    """Summarise a GitHub Actions workflow's `on:` block as human-readable trigger strings."""
    try:
        wf = yaml.safe_load(yml_text)
    except yaml.YAMLError:
        return [], None
    if not isinstance(wf, dict):
        return [], None
    # YAML parses bare `on:` as boolean True key
    on = wf.get("on", wf.get(True, {}))
    triggers = []
    if isinstance(on, str):
        triggers.append(on)
    elif isinstance(on, list):
        triggers.extend(str(t) for t in on)
    elif isinstance(on, dict):
        for evt, cfg in on.items():
            if evt == "push" and isinstance(cfg, dict) and cfg.get("branches"):
                triggers.append(f"push:{','.join(cfg['branches'])}")
            elif evt == "schedule" and isinstance(cfg, list):
                crons = [c.get("cron", "?") for c in cfg if isinstance(c, dict)]
                triggers.append(f"schedule:{' | '.join(crons)}")
            else:
                triggers.append(str(evt))
    # which Azure app does it deploy to?
    app_name = None
    for job in (wf.get("jobs") or {}).values():
        for step in (job.get("steps") or []):
            uses = str(step.get("uses", ""))
            if "webapps-deploy" in uses or "functions-action" in uses:
                app_name = (step.get("with") or {}).get("app-name")
    return triggers, app_name


def sync_one(owner, key, entry):
    repo = entry["repo"]
    full = f"{owner}/{repo}"
    live = {}

    meta = gh(["api", f"repos/{full}", "--jq",
               '{description: .description, default_branch: .default_branch, pushed_at: .pushed_at}'])
    if meta is None:
        print(f"  ✗ {key}: repo {full} unreachable — keeping previous live block")
        return entry.get("live") or {}
    live["github_description"] = meta.get("description")
    live["default_branch"] = meta.get("default_branch")
    live["pushed_at"] = meta.get("pushed_at")

    files = gh(["api", f"repos/{full}/contents/.github/workflows",
                "--jq", "[.[] | {name: .name, path: .path}]"]) or []
    workflows = []
    for f in files:
        wf = {"file": f["name"]}
        text = gh(["api", f"repos/{full}/contents/{f['path']}",
                   "-H", "Accept: application/vnd.github.raw"], raw=True)
        if text:
            triggers, app = parse_workflow_triggers(text)
            wf["triggers"] = triggers
            if app:
                wf["deploys_to"] = app
        workflows.append(wf)
    live["workflows"] = workflows

    runs = gh(["run", "list", "-R", full, "--limit", "1",
               "--json", "displayTitle,workflowName,status,conclusion,updatedAt,url"])
    if runs:
        r = runs[0]
        live["latest_run"] = {
            "workflow": r.get("workflowName"),
            "title": r.get("displayTitle"),
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "updated_at": r.get("updatedAt"),
            "url": r.get("url"),
        }
    n_wf = len(workflows)
    concl = (live.get("latest_run") or {}).get("conclusion", "no runs")
    print(f"  ✓ {key}: {n_wf} workflow(s), latest run = {concl}")
    return live


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="sync only the automation whose repo matches this name")
    args = ap.parse_args()

    reg = yaml.safe_load(open(REGISTRY, encoding="utf-8"))
    owner = reg["github_owner"]
    print(f"Syncing GitHub registry for {owner} …")

    for key, entry in reg["automations"].items():
        if args.repo and entry["repo"] != args.repo:
            continue
        entry["live"] = sync_one(owner, key, entry)

    reg["last_synced"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        "# automation_registry.yaml — Lulu's knowledge of every GitHub automation/workflow project.\n"
        "#\n"
        "# `business:` and `logic:` blocks are HAND-MAINTAINED (logic extracted from actual source 2026-06-11).\n"
        "# `live:` blocks are MACHINE-REFRESHED by `python sync_github_registry.py` — do not hand-edit.\n"
        "# Consumed by tools/automation_tool.py (list / detail / find / runs).\n"
    )
    with open(REGISTRY, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(reg, f, allow_unicode=True, sort_keys=False, width=110)
    print(f"Saved {REGISTRY.name} (last_synced={reg['last_synced']})")


if __name__ == "__main__":
    main()
