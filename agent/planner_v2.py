"""
Planner V2 — from Tool Selector to PLANNER.

V1 picked ONE tool per question. V2 recognises META intents and plans a SEQUENCE of
tool calls whose results get synthesised into one executive answer:

  analyse_workforce_risk  = check_roster_risk + supplier_compliance_risk
                            + find_roster_gaps + expiry_forecast
  workforce_report        = find_active_workers + find_expired_tickets(count)
                            + find_deployable_workers + find_roster_gaps + get_supplier_summary
  site_readiness(site)    = get_site_assignments + site_compliance_report
                            + find_deployable_workers

Ordinary questions fall through to the V1 single-tool router unchanged.
No new data tools are added — V2 is purely a COMPOSITION layer (the admin's rule:
the next gain comes from combining tools, not adding them).
"""

import re
from dataclasses import dataclass, field

from planner import Planner, QueryPlan


@dataclass
class PlanStep:
    tool: str
    function: str
    args: dict = field(default_factory=dict)
    purpose: str = ""


@dataclass
class MetaPlan:
    question: str
    name: str = ""                       # meta-tool name, e.g. analyse_workforce_risk
    domain: str = ""
    steps: list = field(default_factory=list)     # [PlanStep]
    synthesis: str = ""                  # synthesizer key in the agent
    is_meta: bool = True


class PlannerV2:
    """Meta-intent recognition first; falls back to the V1 single-tool Planner."""

    def __init__(self):
        self.v1 = Planner()

    # ---------------- meta intents ----------------
    def _meta(self, question):
        q = question.lower()

        if any(p in q for p in ["workforce risk", "analyse risk", "analyze risk", "risk analysis",
                                "overall risk", "风险分析", "整体风险"]):
            return MetaPlan(question, name="analyse_workforce_risk", domain="Insight (meta)",
                            synthesis="workforce_risk",
                            steps=[
                                PlanStep("roster", "check_roster_risk", {"days_back": 30},
                                         "rostered workers holding expired certs"),
                                PlanStep("insight", "supplier_compliance_risk", {},
                                         "which suppliers concentrate the risk"),
                                PlanStep("roster", "find_roster_gaps", {"days": 90},
                                         "idle bench (capacity to backfill)"),
                                PlanStep("training", "expiry_forecast", {"months": 3},
                                         "how the problem grows next quarter"),
                            ])

        if any(p in q for p in ["workforce report", "monthly report", "operations report",
                                "workforce snapshot", "运营报告", "月报"]):
            return MetaPlan(question, name="workforce_report", domain="Insight (meta)",
                            synthesis="workforce_report",
                            steps=[
                                PlanStep("people", "find_active_workers", {}, "active headcount"),
                                PlanStep("training", "find_expired_tickets", {"count_only": True},
                                         "expired cert count"),
                                PlanStep("training", "find_expiring_tickets", {"days": 30},
                                         "expiring within 30 days"),
                                PlanStep("insight", "find_deployable_workers", {}, "deployable pool"),
                                PlanStep("roster", "find_roster_gaps", {"days": 90}, "bench size"),
                                PlanStep("people", "get_supplier_summary", {}, "supply base"),
                            ])

        m = re.search(r"is ([A-Za-z][A-Za-z0-9 _-]*?) ready", question, re.I)
        if m or "site readiness" in q:
            site = m.group(1).strip() if m else (re.search(r"site ([A-Za-z0-9 _-]+)", question, re.I).group(1).strip()
                                                 if re.search(r"site ([A-Za-z0-9 _-]+)", question, re.I) else "")
            return MetaPlan(question, name="site_readiness", domain="Insight (meta)",
                            synthesis="site_readiness",
                            steps=[
                                PlanStep("project", "get_site_assignments", {"site": site}, "who is assigned"),
                                PlanStep("insight", "site_compliance_report", {"site": site},
                                         "crew compliance holes"),
                                PlanStep("insight", "find_deployable_workers", {},
                                         "replacements available if crew is non-compliant"),
                            ])

        return None

    # ---------------- entry ----------------
    def plan(self, question, user_role="default"):
        meta = self._meta(question)
        if meta:
            return meta
        return self.v1.plan(question, user_role)     # QueryPlan (single tool) — is_meta absent


def plan_summary(plan):
    """Step-1 of the search-escalation chain: restate what the planner UNDERSTOOD
    (domain / tool / dates / entity) so a 0-row answer can be diagnosed, not shrugged at."""
    if isinstance(plan, MetaPlan):
        return {"kind": "meta", "name": plan.name, "domain": plan.domain,
                "steps": [f"{s.tool}.{s.function}" for s in plan.steps]}
    args = getattr(plan, "args", {}) or {}
    return {"kind": "single", "domain": getattr(plan, "domain", ""),
            "tool": getattr(plan, "tool", ""), "function": getattr(plan, "function", ""),
            "dates": {k: v for k, v in args.items() if k in ("date_from", "date_to", "month", "period", "year")},
            "entity": (getattr(plan, "resolved_terms", {}) or {}).get("entity"),
            "args": args,
            "needs_clarification": getattr(plan, "needs_clarification", False)}
