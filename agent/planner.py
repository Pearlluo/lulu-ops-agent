"""
LuluAgent Smart V1 — tool-first semantic planner.

The planner no longer thinks in SQL. It thinks in BUSINESS TOOLS:
  question -> domain -> semantic layer (business_definitions.yaml) -> pick a tool FUNCTION + args.
SQL exists only inside the tools, and every tool query passes sql_validator before DuckDB.

Output: QueryPlan(tool, function, args) — or a clarification when the question is ambiguous.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parent


@dataclass
class QueryPlan:
    question: str
    domain: str = ""
    tool: str = ""
    function: str = ""
    args: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    resolved_terms: dict = field(default_factory=dict)
    needs_clarification: bool = False
    clarification: str = ""


class Planner:
    def __init__(self, agent_dir=AGENT_DIR):
        self.defs = yaml.safe_load(open(agent_dir / "business_definitions.yaml", encoding="utf-8"))

    # ---------- semantic-layer phrase matching ----------
    def _hits(self, section, q):
        out = {}
        for name, t in (self.defs.get(section) or {}).items():
            for p in t.get("phrases", []):
                if p.lower() in q:
                    out[name] = t
                    break
        return out

    # ---------- entity/value extraction ----------
    @staticmethod
    def _filters(question):
        f = {}
        m = re.search(r"\bworker\s+(\d+)\b", question, re.I)
        if m:
            f["worker_id"] = int(m.group(1))
        m = re.search(r"\b(20\d\d)-(\d\d)\b", question)
        if m:
            f["month"] = m.group(0)
        elif re.search(r"\b(20\d\d)\b", question):
            f["year"] = re.search(r"\b(20\d\d)\b", question).group(1)
        m = re.search(r"\b([A-Z]{2,})\s+([A-Z]{2,})\b", question)
        if m and m.group(0) not in ("OPMS", "BMS"):
            f["worker_name"] = m.group(0)
        m = re.search(r"compliant for ([A-Za-z ]+?)\??$", question, re.I)
        if m:
            f["competency"] = m.group(1).strip()
        m = re.search(r"does ([A-Z][A-Za-z ]+?) have", question)
        if m:
            f["client"] = m.group(1).strip()
        m = re.search(r"\bat ([A-Z][A-Za-z ]*[A-Za-z])\b", question)
        if m:
            f["site"] = m.group(1).strip()
        m = re.search(r"in (january|february|march|april|may|june|july|august|september|october|november|december)\s+(20\d\d)", question, re.I)
        if m:
            mon = ["january", "february", "march", "april", "may", "june", "july", "august",
                   "september", "october", "november", "december"].index(m.group(1).lower()) + 1
            f["period"] = f"{m.group(2)}-{mon:02d}"
        m = re.search(r"next (\d+) days", question, re.I)
        if m:
            f["days"] = int(m.group(1))

        # relative dates + business entities — shared bilingual rules (time_entity_parser)
        from time_entity_parser import parse_time, resolve_entity
        pt = parse_time(question, relative_only=True)
        pt.pop("_time_phrase", None)
        f.update(pt)
        ent = resolve_entity(question)
        if ent:
            f["_entity"] = ent        # underscore key: routes opt in explicitly, never auto-leaks to args
        return f

    def _route(self, q, f, terms):
        """Return (domain, tool, function, args, steps). Order = intent priority."""
        # GitHub automation estate (knowledge layer — explicit keywords, so checked first)
        if any(p in q for p in ["automation", "github", "workflow", "自动化", "流水线"]):
            if any(p in q for p in ["fail", "success", "succeed", "status", " ran ", " run", "deploy",
                                    "跑", "成功", "失败", "健康", "部署"]):
                return ("Automation", "automation", "get_automation_runs", {},
                        ["latest GitHub Actions runs (live gh, cached fallback)"])
            return ("Automation", "automation", "list_automations", {},
                    ["list the automation registry"])
        if any(p in q for p in ["哪个系统", "什么系统", "which system", "what system"]):
            m = re.search(r"(?:which|what) system\s*(?:handles|manages|updates|runs|does|is for)?\s*(.+?)[\?？]?$", q)
            kw = (m.group(1).strip() if m
                  else re.sub(r"(哪个系统|什么系统|负责|管理|管|更新|处理|是|的|呢|[\?？])", "", q).strip())
            return ("Automation", "automation", "find_automation", {"keyword": kw},
                    ["search automation registry by topic"])

        # cross-domain intelligence first (most specific)
        if "360" in q or "everything about" in q or "full picture" in q:
            return ("Insight", "insight", "worker_360",
                    {"worker_id": f.get("worker_id")}, ["combine profile+certs+roster+hours+licences"])
        if ("deployable" in q or "available to deploy" in q or "ready to deploy" in q or "可派" in q
                or "available for work" in q or "available right now" in q or "谁有空" in q):
            return ("Insight", "insight", "find_deployable_workers", {},
                    ["active AND fully compliant AND not rostered = deployable"])
        if "supplier" in q and ("risk" in q or "compliance" in q):
            return ("Insight", "insight", "supplier_compliance_risk", {},
                    ["join workers->expired certs, aggregate per supplier"])
        if ("site" in q or f.get("site")) and "compliance" in q:
            return ("Insight", "insight", "site_compliance_report", {"site": f.get("site", "")},
                    ["join site crew -> expired certs"])
        if "roster" in q and "risk" in q or ("rostered" in q and ("expired" in q or "risk" in q)):
            return ("Roster/Compliance", "roster", "check_roster_risk",
                    {k: v for k, v in [("project", f.get("project"))] if v},
                    ["join rostered workers -> expired certs = deployment risk"])
        if any(p in q for p in ["roster gap", "no roster", "not rostered", "unrostered", "没有排班"]):
            return ("Roster", "roster", "find_roster_gaps", {"days": f.get("days", 90)},
                    ["active workers anti-join recent roster"])

        # compliance / training
        if ("compliant" in q or "can work" in q) and ("worker_id" in f or "worker_name" in f):
            return ("Compliance", "training", "check_worker_compliance",
                    {k: f[k] for k in ("worker_id", "worker_name", "competency") if k in f},
                    ["verdict: compliant iff a non-expired matching cert exists"])
        if "not_eligible" in terms or ("cannot" in q and ("work" in q or "deploy" in q)):
            return ("Compliance", "training", "find_not_eligible_workers", {},
                    ["semantic: cannot work => expired certs, grouped per worker"])
        if "forecast" in q or "预测" in q:
            return ("Compliance", "training", "expiry_forecast", {"months": 6},
                    ["group upcoming expiries by month"])
        if "expiring_soon" in terms:
            return ("Compliance", "training", "find_expiring_tickets", {"days": f.get("days", 30)},
                    ["semantic: expiring soon => days_to_expiry 0..N"])
        if "expired" in terms:
            count = any(w in q for w in ["how many", "多少", "count"])
            return ("Compliance", "training", "find_expired_tickets", {"count_only": count},
                    ["semantic: expired => is_expired = true"])
        if "licence" in q or "license" in q or "执照" in q:
            return ("Compliance", "people", "get_worker_licences",
                    {k: f[k] for k in ("worker_id",) if k in f}, ["licence_register lookup"])

        # weekly timesheet (ACTUAL hours, GitHub automation logic) — beats roster/monthly-hours routes
        if "timesheet" in q or "工时表" in q or "考勤" in q:
            args = {}
            if "date_from" in f:
                args["date_from"], args["date_to"] = f["date_from"], f.get("date_to", f["date_from"])
            elif "month" in f:
                args["date_from"], args["date_to"] = f["month"] + "-01", f["month"] + "-31"
            else:                                   # no date given -> last full week (the weekly report)
                import datetime as dt
                from lulu_time import perth_today
                monday = perth_today() - dt.timedelta(days=perth_today().weekday() + 7)
                args["date_from"], args["date_to"] = monday.isoformat(), (monday + dt.timedelta(days=6)).isoformat()
            for k in ("worker_id", "worker_name", "site"):
                if k in f:
                    args[k] = f[k]
            ent = f.get("_entity")
            if ent and ent["type"] in ("site", "project") and ent["type"] not in args:
                args[ent["type"]] = ent["value"]
            elif ent and ent["type"] == "person" and not (args.keys() & {"worker_name", "worker_id"}):
                args["worker_name"] = ent["value"]
            # "everyone's timesheet" wants the weekly-report MATRIX (per person, day columns)
            if (any(p in q for p in ["所有人", "所有员工", "每个人", "大家", "全员", "everyone",
                                     "all workers", "all staff", "per person", "per worker", "按人",
                                     "周报", "weekly report"])
                    and not (args.keys() & {"worker_name", "worker_id"})):
                args["group_by"] = "matrix"
            return ("Time", "timesheet", "get_weekly_timesheet", args,
                    ["actual = OPMS-matched hours minus gap deductions (weekly automation logic)"])

        # time / hours
        if "total_hours" in terms or "hours" in q or "工时" in q:
            if "worker_id" in f or "worker_name" in f:
                return ("Time", "timesheet", "get_worker_hours",
                        {k: f[k] for k in ("worker_id", "worker_name", "month", "year") if k in f},
                        ["sum hours for one worker"])
            if "site" in q:
                return ("Time", "timesheet", "get_site_hours", {"year": f.get("year")},
                        ["sum hours grouped by site"])
            if "project" in q:
                return ("Time", "timesheet", "get_project_hours", {}, ["rostered-hours proxy per project"])
            if "top" in q or "most" in q:
                return ("Time", "timesheet", "top_workers_by_hours", {"year": f.get("year")}, ["rank workers"])
            return ("Time", "timesheet", "get_timesheet_summary",
                    {"year": f.get("year"), "by": "month"}, ["hours by month"])

        # OPMS<->BMS project/client bridge ("which client is SH-25006 for / 这个项目是哪个客户的")
        # — needs PROJECT context (job code or 项目/job words), else revenue/finance routes own it
        if (("which client" in q or "哪个客户" in q or "什么客户" in q or "属于哪个客户" in q)
                and (re.search(r"\b[a-z]{2,4}-\d{4,6}\b", q) or "项目" in q
                     or "project" in q or "job" in q)):
            m = re.search(r"\b([a-z]{2,4}-\d{4,6})\b", q)
            term = (m.group(1).upper() if m
                    else re.sub(r"(which client|is|for|哪个客户|什么客户|属于|项目|的|是|呢|[\?？])", "", q).strip())
            return ("Project", "project", "resolve_project_client", {"term": term},
                    ["job-code prefix -> JMS-Jobs -> JMS-Projects -> client (bridge)"])

        # projects / jobs
        if "active_project" in terms or ("project" in q and ("active" in q or "活跃" in q)):
            return ("Project", "project", "get_active_projects", {}, ["is_active = true"])
        if "client" in f or ("jobs" in q and ("client" in q or "have" in q)):
            return ("Project", "project", "get_project_jobs", {"client": f.get("client")},
                    ["job counts per project for client"])
        if "job" in q and ("detail" in q or re.search(r"job\s+[A-Z0-9-]+", q, re.I)):
            return ("Project", "project", "get_job_detail", {"job_code": f.get("job_code")}, ["job lookup"])
        if "who works at" in q or ("site" in f and ("who" in q or "crew" in q)):
            return ("Workforce", "project", "get_site_assignments", {"site": f.get("site")}, ["site crew"])

        # roster (plain)
        if ("rostered" in q or "roster" in q or "排班" in q or "时间表" in q
                or "schedule" in q or "班表" in q):
            args = {}
            if "date_from" in f:
                args["date_from"], args["date_to"] = f["date_from"], f.get("date_to", f["date_from"])
            elif "period" in f:
                args["period"] = f["period"]
            elif "month" in f:
                args["period"] = f["month"]
            elif "year" in f:
                args["period"] = f["year"]
            if "worker_id" in f:
                args["worker_id"] = f["worker_id"]
            ent = f.get("_entity")
            if ent and ent["type"] == "project" and "project" not in args:
                args["project"] = ent["value"]
            return ("Roster", "roster", "get_roster_summary", args,
                    ["resolve relative dates from today", "filter roster by period/range/person"])

        # revenue / receivables (Xero) — before purchases
        if any(p in q for p in ["revenue", "收入", "invoiced", "billed", "营收"]):
            ent = f.get("_entity")
            args = {"client": ent["value"]} if ent and ent["type"] == "client" else {}
            if "year" in f:
                args["year"] = f["year"]
            return ("Finance", "finance", "get_client_revenue", args,
                    ["Xero ACCREC rollup; amounts Finance-gated; data ends ~2026-04"])
        if any(p in q for p in ["outstanding", "receivable", "unpaid invoice", "应收", "欠款", "未付"]):
            return ("Finance", "finance", "get_outstanding_invoices", {},
                    ["authorised ACCREC with amount_due > 0"])

        # purchases / finance (before supplier!)
        if "purchase" in q or "spend" in q or "采购" in q or "花费" in q:
            return ("Finance", "finance", "get_purchase_summary", {}, ["amounts gated by Finance role"])
        if "rate" in q or "费率" in q:
            return ("Finance", "finance", "get_rate_card",
                    {k: f[k] for k in ("project",) if k in f}, ["rates gated by Finance role"])

        # suppliers
        if "supplier" in q or "供应商" in q:
            return ("Supplier", "people", "get_supplier_summary", {}, ["worker_count per supplier"])

        # audit / hseq
        if "audit" in q or "changed" in q or "变更" in q:
            return ("Audit", "hseq", "get_audit_issues",
                    {k: f[k] for k in ("worker_id",) if k in f}, ["CDC events desc"])
        if "safety" in q or "hseq" in q or "overdue" in q or "incident" in q:
            return ("HSEQ", "hseq", "get_hseq_register",
                    {"overdue_only": "overdue" in q, "open_only": "open" in q}, ["hseq flags"])

        # PPE ledger (sign-outs / monthly usage) — before generic stock so
        # "PPE usage" / "who signed out" don't fall through to stock-on-hand
        if "领用" in q or (("ppe" in q or "workwear" in q) and any(
                p in q for p in ["sign", "issued", "usage", "trend", "monthly",
                                 "who took", "consumption", "用量", "消耗", "趋势"])):
            if any(p in q for p in ["usage", "trend", "monthly", "consumption",
                                    "per month", "用量", "消耗", "趋势", "每月"]):
                return ("Inventory", "inventory_asset", "get_ppe_monthly_usage", {},
                        ["monthly PPE sign-out totals desc"])
            args = {}
            if "worker_name" in f:
                args["person"] = f["worker_name"]
            return ("Inventory", "inventory_asset", "get_ppe_signouts", args,
                    ["PPE sign-out ledger, newest first"])

        # inventory / assets
        if "out_of_stock" in terms:
            return ("Inventory", "inventory_asset", "find_low_stock", {"threshold": 0},
                    ["semantic: out of stock => stock <= 0"])
        if "low stock" in q or "low_stock" in q:
            return ("Inventory", "inventory_asset", "find_low_stock", {"threshold": f.get("days", 5)}, ["low stock"])
        if "stock" in q or "inventory" in q or "库存" in q:
            return ("Inventory", "inventory_asset", "get_inventory_summary", {}, ["stock levels desc"])
        if "asset" in q or "equipment" in q or "资产" in q:
            return ("Asset", "inventory_asset", "search_assets", {"term": f.get("term")}, ["asset search"])
        if "hardware" in q or "硬件" in q:
            return ("Asset", "inventory_asset", "hardware_stock", {}, ["hardware stock"])

        # people (needs an anchor)
        if "inactive" in q or "terminated" in q or "离职" in q:
            return ("People", "people", "find_inactive_workers", {}, ["is_active = false"])
        if "worker_id" in f or "worker_name" in f:
            return ("People", "people", "get_employee_profile",
                    {k: f[k] for k in ("worker_id",) if k in f} | ({"name": f["worker_name"]} if "worker_name" in f else {}),
                    ["profile lookup"])
        if "active_worker" in terms or ("active" in q and ("worker" in q or "staff" in q)):
            return ("People", "people", "find_active_workers", {}, ["is_active = true"])

        return None

    # ---------- main entry ----------
    def plan(self, question, user_role="default"):
        q = question.lower()
        plan = QueryPlan(question=question)
        terms = self._hits("status_terms", q) | self._hits("metric_terms", q)
        plan.resolved_terms = {k: v.get("predicate", v.get("sql", "")) for k, v in terms.items()}
        f = self._filters(question)
        if f.get("_entity"):
            plan.resolved_terms["entity"] = f"{f['_entity']['type']}:{f['_entity']['value']}"

        routed = self._route(q, f, terms)
        if routed:
            plan.domain, plan.tool, plan.function, plan.args, plan.steps = routed
            plan.args = {k: v for k, v in plan.args.items() if v is not None}
            return plan

        plan.needs_clarification = True
        plan.clarification = ("I couldn't map this to a business tool. Are you asking about people/compliance, "
                              "roster, hours, projects, suppliers, assets, inventory, purchases, safety, or "
                              "audit history? Please name the worker/project/site you mean.")
        plan.steps = ["ambiguous -> ask, never guess"]
        return plan
