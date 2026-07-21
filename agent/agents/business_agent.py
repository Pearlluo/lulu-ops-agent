"""Business Agent — workforce/operations Q&A over Gold (read-only, via the security chain).

This domain ALREADY EXISTS as 7-8 Gold tools + the deterministic planner. The agent
fronts them: it delegates to the existing LuluAgent so every query still goes through
agent_registry → sql_validator → DuckDB → Gold (no bypass).
"""

from base_agent import BaseAgent

_lulu = None


def _agent():
    global _lulu
    if _lulu is None:
        from lulu_agent import LuluAgent          # lazy — pulls the whole deterministic stack
        _lulu = LuluAgent()
    return _lulu


class BusinessAgent(BaseAgent):
    name = "business"
    title = "Business"
    layer = "business"
    domain = "业务处理 — People/Roster/Timesheet、Project/Site readiness、Training/Compliance、Inventory/PPE、Workforce risk"
    keywords = ["人", "people", "员工", "roster", "排班", "timesheet", "工时", "project", "项目",
                "site", "现场", "training", "培训", "compliance", "合规", "证", "inventory",
                "库存", "ppe", "派工", "风险", "risk", "worker", "可去", "deployable"]
    owns_tools = ["people", "roster", "timesheet", "project", "training", "inventory_asset", "insight"]
    read_actions = ["query_gold (via security chain)"]
    write_actions = []          # read-only domain

    def status(self) -> dict:
        return {"agent": self.name, "ok": True,
                "summary": "业务问答就绪 — 7 个 Gold 工具 + insight(worker_360/roster_risk/site_readiness)，只读走安全链",
                "alerts": []}

    def handle(self, request: str, role: str = "default") -> dict:
        try:
            res = _agent().answer(request, user_role=role)
            return {"agent": self.name, "ok": getattr(res, "ok", True),
                    "summary": getattr(res, "answer", None) or getattr(res, "summary", str(res)),
                    "data": res}
        except Exception as ex:
            return {"agent": self.name, "ok": False,
                    "summary": f"Business Agent 委派 LuluAgent 失败: {ex}", "data": None}
