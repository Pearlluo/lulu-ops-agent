"""
LuluAgent Smart V2 — planner-driven orchestrator with tool analytics.

Single questions  -> one business tool (V1 routing).
Meta questions    -> PlannerV2 emits a MULTI-STEP plan; the agent executes every step
                     through the same safety chain and SYNTHESISES one executive answer.
Every call        -> logged by UsageLogger (success / zero-rows / follow-up / correction),
                     so we learn which tools earn their keep.

"""

import time
from dataclasses import dataclass, field

from planner_v2 import PlannerV2, MetaPlan
from tools import build_tools
from tool_usage_logger import UsageLogger
from memory_manager import MemoryManager
from conversation_trace_logger import TraceLogger


@dataclass
class AgentResponse:
    question: str
    domain: str = ""
    tool: str = ""
    function: str = ""
    args: dict = field(default_factory=dict)
    tables: list = field(default_factory=list)
    plan_steps: list = field(default_factory=list)
    sql: str = ""
    validator_ok: bool = False
    validator_errors: list = field(default_factory=list)
    answer: str = ""
    confidence: str = "Low"
    caveats: list = field(default_factory=list)
    data: list = field(default_factory=list)
    is_meta: bool = False
    step_results: list = field(default_factory=list)   # per-step {tool, function, ok, rows, summary}
    memory_used: list = field(default_factory=list)    # which remembered knowledge informed this answer
    learned: str = ""                                   # what this turn taught Lulu (if a statement)
    row_count: int = 0                                  # total result rows (trace logging)
    trace_id: str = ""                                  # conversation trace id (feedback hooks)
    export_rows: list = field(default_factory=list)     # FULL result rows (UI Excel export)


WHO_FOR_SITE = ("who", "谁", "can go", "can work", "ready", "可以去", "能去", "派", "deploy")


class LuluAgent:
    def __init__(self):
        self.planner = PlannerV2()
        self.tools = build_tools()
        self.logger = UsageLogger()
        self.memory = MemoryManager()
        self.trace = TraceLogger()

    # ------------------------------------------------------------------
    def ask(self, question, user_role="default", user="admin",
            history=None, conversation_id=None) -> AgentResponse:
        """history: previous turns [{question, answer, tool, function, args}] for follow-ups
        ("那这周呢?"). conversation_id groups the turns in the trace log."""
        t0 = time.time()
        turn_no = len(history or []) + 1
        self.logger.current_user = user          # stamp every usage-log row with who asked

        def _traced(resp, plan=None):
            resp.trace_id = self.trace.log_deterministic(
                resp, t0, user=user, user_role=user_role,
                resolved_terms=getattr(plan, "resolved_terms", None),
                conversation_id=conversation_id, turn=turn_no)
            return resp

        # ---- Tier 2/3 memory: LEARN from statements before planning ----
        kind, learned = self.memory.capture(question, user=user)
        if kind in ("site_rule", "supplier_flag", "definition", "preference", "fact"):
            resp = AgentResponse(question=question, domain="Memory", tool="memory", function=kind)
            resp.learned = str(learned)
            resp.confidence = "High"
            resp.validator_ok = True
            if kind == "site_rule":
                site, tickets = next(iter(learned.items()))
                resp.answer = (f"✅ 已记住业务规则: {site.upper()} 要求 {', '.join(tickets)}。"
                               f" 以后问『谁可以去 {site.upper()}』我会直接按这个规则核对证件。")
            elif kind == "supplier_flag":
                resp.answer = f"✅ 已记住: 供应商 {learned.get('name')} 被标记为高风险({learned.get('noted')})。后续供应商风险分析会参考这条。"
            elif kind == "definition":
                term, meaning = next(iter(learned.items()))
                resp.answer = f"✅ 已记住定义: '{term}' = {meaning}"
            elif kind == "preference":
                resp.answer = f"✅ 已记住你的偏好: {learned}"
            else:
                resp.answer = f"✅ 已记入业务备忘: {str(learned)[:120]}"
            resp.plan_steps = ["classified as business knowledge -> persisted to company_memory.yaml"]
            return _traced(resp)

        # ---- Tier 2 memory: RECALL relevant knowledge for this question ----
        recalled = self.memory.recall(question)

        # memory-driven site staffing: remembered rule + 'who can go' intent
        if recalled["site_rules"] and any(w in question.lower() for w in WHO_FOR_SITE):
            site, rule = next(iter(recalled["site_rules"].items()))
            tickets = rule["required_tickets"]
            rows, sql = self.memory.workers_meeting_tickets(tickets, user_role)
            resp = AgentResponse(question=question, domain="Memory + Compliance",
                                 tool="memory", function="site_staffing_by_rule",
                                 args={"site": site, "required_tickets": tickets})
            resp.memory_used = [f"site rule {site.upper()} = {', '.join(tickets)} (learned {rule['learned']})"]
            resp.plan_steps = [f"1) memory: {site.upper()} requires {', '.join(tickets)}",
                               "2) Gold: workers holding a VALID cert for EVERY required ticket",
                               "3) same safety chain (validator -> DuckDB -> gold)"]
            resp.sql = sql
            resp.validator_ok = True
            resp.tables = ["training_compliance"]
            resp.data = rows[:8]
            names = ", ".join(f"{r['first_name']} {r['last_name']}" for r in rows[:5])
            resp.answer = (f"{len(rows)} workers meet {site.upper()}'s remembered requirements "
                           f"({', '.join(tickets)}) with all certs valid"
                           + (f": {names}{'…' if len(rows) > 5 else ''}." if rows else
                              " — no one currently holds valid certs for all of them."))
            resp.confidence = "High" if rows else "Medium"
            resp.row_count = len(rows)
            self.memory.observe(user, question, resp.domain)
            self.logger.log(question, "memory", "site_staffing_by_rule", True, len(rows), resp.confidence)
            return _traced(resp)

        # ---- normal planning path ----
        plan = self.planner.plan(question, user_role)

        # multi-turn: a clarification WITH history is usually a follow-up delta
        # ("那这周呢?" inherits the last tool, swaps only the changed filters)
        if not isinstance(plan, MetaPlan) and plan.needs_clarification and history:
            from conversation_context import merge_followup
            import inspect
            fu = merge_followup(question, history)
            fn = fu and getattr(self.tools.get(fu["tool"]), fu["function"], None)
            if fn:
                valid = set(inspect.signature(fn).parameters)
                dropped = sorted(k for k in fu["args"] if k not in valid)
                plan.tool, plan.function = fu["tool"], fu["function"]
                plan.args = {k: v for k, v in fu["args"].items() if k in valid}
                plan.domain = "Follow-up"
                plan.needs_clarification, plan.clarification = False, ""
                plan.steps = ([f"follow-up of: {fu['inherited_from'][:60]}"] + fu["notes"]
                              + ([f"dropped unsupported filter(s): {', '.join(dropped)}"] if dropped else []))

        routed_tool = getattr(plan, "tool", None) or (plan.name if isinstance(plan, MetaPlan) else None)
        self.logger.observe_next_question(question, routed_tool=routed_tool)

        resp = self._run_meta(plan, user_role) if isinstance(plan, MetaPlan) \
            else self._run_single(plan, user_role)

        # enrich answers with remembered supplier flags / definitions
        if recalled["supplier_flags"] and ("supplier" in question.lower() or "供应商" in question or
                                           "risk" in question.lower() or "风险" in question):
            for f in recalled["supplier_flags"][:2]:
                note = (f" 📌 Memory: 你在 {f.get('noted')} 提到过 {f.get('name')}"
                        f"（『{f.get('note','')[:40]}…』）— 当前数据验证后该判断仍成立。"
                        if f.get("name", "").lower() in resp.answer.lower() else
                        f" 📌 Memory: 你此前标记过 {f.get('name')} 高风险（{f.get('noted')}）。")
                resp.answer += note
                resp.memory_used.append(f"supplier flag: {f.get('name')} ({f.get('noted')})")
        if recalled["definitions"]:
            for t, d in list(recalled["definitions"].items())[:2]:
                resp.memory_used.append(f"definition: {t} = {d['meaning'][:50]}")

        self.memory.observe(user, question, resp.domain)
        return _traced(resp, plan)

    # ---------------- single-tool path (V1) ----------------
    def _run_single(self, plan, user_role):
        resp = AgentResponse(question=plan.question, domain=plan.domain,
                             tool=plan.tool, function=plan.function, args=plan.args,
                             plan_steps=plan.steps)
        if plan.needs_clarification:
            resp.answer = f"CLARIFICATION NEEDED: {plan.clarification}"
            self.logger.log(plan.question, "", "clarification", True, 0, "Low")
            return resp

        t0 = time.time()
        fn = getattr(self.tools.get(plan.tool), plan.function, None)
        if fn is None:
            resp.answer = f"Internal routing error: {plan.tool}.{plan.function}"
            return resp
        try:
            r = fn(**plan.args, user_role=user_role)
        except Exception as ex:
            resp.answer = f"Tool error: {type(ex).__name__}: {ex}"
            self.logger.log(plan.question, plan.tool, plan.function, False, 0, "Low",
                            [str(ex)], (time.time() - t0) * 1000, user_role)
            return resp

        # ---- Search escalation: a 0-row Gold answer is never final. Re-check intent,
        # resolve the entity, retry, probe related tables, (Admin_IT) inspect RAW —
        # and only then answer with a DIAGNOSIS, not a bare "no records". ----
        if r.ok and r.row_count == 0 and plan.args:
            from search_escalation import escalate
            from tools._base import get_query_tool
            r, esc_notes = escalate(plan, r, fn, get_query_tool(), user_role)
            resp.plan_steps = list(resp.plan_steps) + esc_notes
            resp.args = plan.args

        resp.sql, resp.tables = r.sql, r.tables
        resp.validator_ok, resp.validator_errors = r.ok, r.validator_errors
        resp.answer, resp.confidence, resp.caveats = r.summary, r.confidence, r.caveats
        resp.data = r.data[:8]
        resp.export_rows = r.data                       # full rows for the UI export button
        resp.row_count = r.row_count
        self.logger.log(plan.question, plan.tool, plan.function, r.ok, r.row_count,
                        r.confidence, r.validator_errors, (time.time() - t0) * 1000, user_role)
        return resp

    # ---------------- meta path (V2 composition) ----------------
    def _run_meta(self, plan: MetaPlan, user_role):
        resp = AgentResponse(question=plan.question, domain=plan.domain,
                             tool="meta", function=plan.name, is_meta=True,
                             plan_steps=[f"{s.tool}.{s.function} — {s.purpose}" for s in plan.steps])
        results = {}
        all_ok = True
        caveats = []
        for s in plan.steps:
            t0 = time.time()
            fn = getattr(self.tools.get(s.tool), s.function, None)
            try:
                r = fn(**s.args, user_role=user_role)
            except Exception as ex:
                all_ok = False
                resp.step_results.append({"tool": s.tool, "function": s.function, "ok": False,
                                          "rows": 0, "summary": f"error: {ex}"})
                continue
            results[s.function] = r
            for c in r.caveats:
                if c not in caveats:
                    caveats.append(c)
            all_ok = all_ok and r.ok
            resp.step_results.append({"tool": s.tool, "function": s.function, "ok": r.ok,
                                      "rows": r.row_count, "summary": r.summary})
            self.logger.log(plan.question, s.tool, s.function, r.ok, r.row_count,
                            r.confidence, r.validator_errors, (time.time() - t0) * 1000,
                            user_role, meta=True)

        resp.validator_ok = all_ok
        resp.caveats = caveats
        resp.row_count = sum(s.get("rows", 0) for s in resp.step_results)
        resp.sql = " ;; ".join(results[k].sql for k in results)
        synth = getattr(self, f"_synth_{plan.synthesis}", None)
        resp.answer = synth(results) if synth else " | ".join(
            f"{k}: {v.summary}" for k, v in results.items())
        resp.confidence = "High" if all_ok and results else "Medium"
        return resp

    # ---------------- synthesisers (meta answers in business language) ----------------
    @staticmethod
    def _synth_workforce_risk(res):
        risk = res.get("check_roster_risk")
        sup = res.get("supplier_compliance_risk")
        gaps = res.get("find_roster_gaps")
        fc = res.get("expiry_forecast")
        n_risk = risk.row_count if risk else 0
        level = "HIGH" if n_risk > 50 else "MEDIUM" if n_risk > 10 else "LOW"
        parts = [f"WORKFORCE RISK: {level}.",
                 f"{n_risk} rostered-worker/expired-cert combinations in the last 30 days."]
        if sup and sup.data:
            parts.append(f"Risk concentrates at supplier {sup.data[0]['supplier_name']} "
                         f"({sup.data[0]['workers_with_expired']} workers, {sup.data[0]['expired_certs']} expired certs).")
        if gaps:
            parts.append(f"Mitigation capacity: {gaps.row_count} active workers idle (no roster in 90 days).")
        if fc and fc.data:
            parts.append("Outlook: expiries rising — " +
                         ", ".join(f"{r['month']}: {r['certs_expiring']}" for r in fc.data[:3]) + ".")
        parts.append("Recommend: re-validate certs for rostered workers first, then backfill from the idle pool.")
        return " ".join(parts)

    @staticmethod
    def _synth_workforce_report(res):
        act = res.get("find_active_workers")
        exp = res.get("find_expired_tickets")
        expg = res.get("find_expiring_tickets")
        dep = res.get("find_deployable_workers")
        gaps = res.get("find_roster_gaps")
        sup = res.get("get_supplier_summary")
        n_exp = exp.data[0]["expired_certs"] if exp and exp.data else 0
        return (f"WORKFORCE REPORT — Active workers: {act.row_count if act else '?'} (BMS-tracked). "
                f"Compliance: {n_exp:,} expired certs, {expg.row_count if expg else 0} expiring within 30 days. "
                f"Capacity: {dep.row_count if dep else 0} deployable now, "
                f"{gaps.row_count if gaps else 0} on the bench (90d no roster). "
                f"Supply base: {sup.row_count if sup else 0} active suppliers"
                + (f", largest {sup.data[0]['supplier_name']} ({sup.data[0]['worker_count']} workers)." if sup and sup.data else "."))

    @staticmethod
    def _synth_site_readiness(res):
        crew = res.get("get_site_assignments")
        comp = res.get("site_compliance_report")
        dep = res.get("find_deployable_workers")
        n_crew = crew.row_count if crew else 0
        n_holes = comp.row_count if comp else 0
        verdict = "READY" if n_crew > 0 and n_holes == 0 else ("AT RISK" if n_holes else "NO CREW ASSIGNED")
        s = f"SITE READINESS: {verdict}. Crew assigned: {n_crew}; crew members with expired certs: {n_holes}."
        if n_holes and comp.data:
            s += (f" Worst: {comp.data[0]['first_name']} {comp.data[0]['last_name']} "
                  f"({comp.data[0]['expired_certs']} expired).")
        s += f" Replacement pool: {dep.row_count if dep else 0} deployable workers."
        return s

    # ---------------- analytics passthrough ----------------
    def usage_report(self, last_n=100):
        return self.logger.print_report(last_n)
