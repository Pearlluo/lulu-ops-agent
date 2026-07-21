"""Automation domain — Lulu's knowledge of the GitHub automation/workflow estate.

Unlike the 9 data tools this one does NOT query Gold: its source is
automation_registry.yaml (business knowledge hand-maintained + live GitHub state
refreshed by sync_github_registry.py). get_automation_runs can additionally ask
GitHub live via the `gh` CLI and falls back to the cached registry when offline.
"""

import json
import subprocess
from pathlib import Path

import yaml

from ._base import ToolResult

AGENT_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = AGENT_DIR / "automation_registry.yaml"

_registry_cache = None


def load_registry(force=False):
    global _registry_cache
    if _registry_cache is None or force:
        _registry_cache = yaml.safe_load(open(REGISTRY_PATH, encoding="utf-8"))
    return _registry_cache


class AutomationTool:
    """Answers: what automations exist, which system does X, how does it deploy, did it run OK."""

    name = "automation"

    def __init__(self):
        self.reg = load_registry()

    # ---------- helpers ----------
    def _entries(self):
        return self.reg.get("automations", {})

    def _match(self, name):
        """Fuzzy-match an automation by key / repo / display name / keyword."""
        if not name:
            return None, None
        n = str(name).lower().strip()
        for key, e in self._entries().items():
            if n in (key.lower(), e["repo"].lower(), e["display_name"].lower()):
                return key, e
        for key, e in self._entries().items():
            hay = " ".join([key, e["repo"], e["display_name"]] +
                           [str(k) for k in e["business"].get("keywords", [])]).lower()
            if n in hay:
                return key, e
        return None, None

    @staticmethod
    def _card(key, e, include_live=True):
        b = e["business"]
        row = {
            "key": key,
            "name": e["display_name"],
            "repo": e["repo"],
            "category": b.get("category"),
            "purpose": (b.get("purpose") or b.get("description") or "").strip(),
            "deployment": b.get("deployment"),
            "apis": b.get("apis", []),
            "related_systems": b.get("related_systems", []),
        }
        live = e.get("live") or {}
        if include_live and live:
            wfs = live.get("workflows", [])
            row["github_workflows"] = [
                {"file": w.get("file"), "triggers": w.get("triggers", []),
                 "deploys_to": w.get("deploys_to")} for w in wfs]
            if live.get("latest_run"):
                lr = live["latest_run"]
                row["latest_deploy"] = {"conclusion": lr.get("conclusion"),
                                        "when": lr.get("updated_at"), "url": lr.get("url")}
            row["last_pushed"] = live.get("pushed_at")
        return row

    def _result(self, function, args, rows, summary, confidence="High"):
        tr = ToolResult(tool=self.name, function=function, args=args, ok=True,
                        data=rows, row_count=len(rows), summary=summary, confidence=confidence)
        tr.caveats = [f"Source: automation_registry.yaml (GitHub {self.reg.get('github_owner')}), "
                      f"last synced {self.reg.get('last_synced') or 'never'} — run sync_github_registry.py to refresh."]
        return tr

    # ---------- tool functions ----------
    def list_automations(self, category=None, user_role="default"):
        rows = []
        for key, e in self._entries().items():
            if category and category.lower() not in str(e["business"].get("category", "")).lower():
                continue
            rows.append(self._card(key, e, include_live=False))
        return self._result("list_automations", {"category": category}, rows,
                            f"{len(rows)} GitHub automation project(s)"
                            + (f" in category '{category}'." if category else " across the Acme estate."))

    def get_automation_detail(self, name, user_role="default"):
        key, e = self._match(name)
        if not e:
            return self._no_match("get_automation_detail", {"name": name}, name)
        card = self._card(key, e)
        b = e["business"]
        card["stack"] = b.get("stack", [])
        card["azure_services"] = b.get("azure_services", [])
        card["planned_tool_candidates"] = b.get("tool_candidates", [])
        if e.get("logic"):
            card["logic"] = e["logic"]      # exact matching rules/formulas from the repo's source
        lr = (e.get("live") or {}).get("latest_run") or {}
        return self._result("get_automation_detail", {"name": name}, [card],
                            f"{e['display_name']} ({e['repo']}): {card['purpose']} "
                            f"Deploys via {b.get('deployment')}; latest GitHub deploy: "
                            f"{lr.get('conclusion', 'unknown')} at {lr.get('updated_at', '?')}.")

    def find_automation(self, keyword, user_role="default"):
        """Which system/automation handles X? Searches purpose, keywords, related systems, APIs, logic."""
        kw = str(keyword or "").lower().strip()
        rows = []
        for key, e in self._entries().items():
            b = e["business"]
            hay = " ".join([key, e["repo"], e["display_name"], str(b.get("category", "")),
                            str(b.get("purpose", "")), str(b.get("description", "")),
                            str(e.get("logic", ""))]
                           + [str(x) for x in b.get("keywords", [])]
                           + [str(x) for x in b.get("related_systems", [])]
                           + [str(x) for x in b.get("apis", [])]).lower()
            if kw and kw in hay:
                rows.append(self._card(key, e, include_live=False))
        if not rows:
            return self._no_match("find_automation", {"keyword": keyword}, keyword)
        names = ", ".join(r["name"] for r in rows)
        return self._result("find_automation", {"keyword": keyword}, rows,
                            f"{len(rows)} automation(s) relate to '{keyword}': {names}.")

    def get_automation_runs(self, name=None, limit=3, user_role="default"):
        """Latest GitHub Actions runs (live via gh CLI; falls back to cached registry state)."""
        targets = []
        if name:
            key, e = self._match(name)
            if not e:
                return self._no_match("get_automation_runs", {"name": name}, name)
            targets.append((key, e))
        else:
            targets = list(self._entries().items())

        owner = self.reg.get("github_owner")
        rows, live_ok = [], True
        for key, e in targets:
            runs = self._gh_runs(f"{owner}/{e['repo']}", limit if name else 1)
            if runs is None:
                live_ok = False
                lr = (e.get("live") or {}).get("latest_run")
                runs = [lr] if lr else []
            for r in runs:
                rows.append({"automation": e["display_name"], "repo": e["repo"],
                             "workflow": r.get("workflow") or r.get("workflowName"),
                             "status": r.get("status"), "conclusion": r.get("conclusion"),
                             "when": r.get("updated_at") or r.get("updatedAt"), "url": r.get("url")})
        failed = [r for r in rows if r.get("conclusion") not in ("success", None)]
        summary = (f"{len(rows)} workflow run(s)"
                   + (f" for {targets[0][1]['display_name']}" if name else " (latest per automation)")
                   + (f"; {len(failed)} not successful: " + ", ".join(r["automation"] for r in failed)
                      if failed else "; all successful.")
                   + ("" if live_ok else " [LIVE GitHub unreachable — showing cached state from last sync]"))
        return self._result("get_automation_runs", {"name": name, "limit": limit}, rows, summary,
                            confidence="High" if live_ok else "Medium")

    # ---------- internals ----------
    @staticmethod
    def _gh_runs(full_repo, limit):
        try:
            out = subprocess.run(
                ["gh", "run", "list", "-R", full_repo, "--limit", str(limit),
                 "--json", "workflowName,status,conclusion,updatedAt,url"],
                capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace")
            if out.returncode != 0:
                return None
            return json.loads(out.stdout) if out.stdout.strip() else []
        except Exception:
            return None

    def _no_match(self, function, args, term):
        known = ", ".join(e["display_name"] for e in self._entries().values())
        tr = ToolResult(tool=self.name, function=function, args=args, ok=True, data=[],
                        row_count=0, confidence="Medium",
                        summary=f"No automation matches '{term}'. Known automations: {known}.")
        return tr
