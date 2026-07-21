"""Finance Agent — payroll/billing, quote margin, rates/markup, invoice reconciliation.

Money/rate fields are Finance-role gated by the EXISTING security chain
(agent_registry restricted_fields + Finance role). This agent enforces the role
before delegating to the Gold finance tool; quote/rates logic lives in
automation_registry. Invoice reconciliation is the one new capability (scaffolded).
"""

from base_agent import BaseAgent

_lulu = None


def _agent():
    global _lulu
    if _lulu is None:
        from lulu_agent import LuluAgent
        _lulu = LuluAgent()
    return _lulu


class FinanceAgent(BaseAgent):
    name = "finance"
    title = "Finance"
    layer = "finance"
    domain = "财务处理 — Payroll/billing、Quote margin、Rates/markup、Invoice 对账（finance-only 权限闸）"
    keywords = ["财务", "finance", "payroll", "工资", "billing", "账单", "invoice", "发票",
                "对账", "quote", "报价", "margin", "毛利", "rate", "费率", "markup", "成本", "金额", "purchase"]
    owns_tools = ["finance", "automation(quote/rates logic)"]
    read_actions = ["query_finance_gold (Finance role only)"]
    write_actions = []
    PERMISSION = "Finance"      # role gate enforced before any finance answer

    def status(self) -> dict:
        return {"agent": self.name, "ok": True,
                "summary": "财务就绪 — finance 工具 + 安全链 Finance 角色门禁；invoice 对账待建",
                "alerts": []}

    def handle(self, request: str, role: str = "default") -> dict:
        if role not in ("Finance", "Admin_IT"):
            return {"agent": self.name, "ok": False,
                    "summary": "🔒 财务数据需 Finance 角色 — 当前角色无权查看金额/费率", "data": None}
        try:
            res = _agent().answer(request, user_role=role)
            return {"agent": self.name, "ok": getattr(res, "ok", True),
                    "summary": getattr(res, "answer", None) or getattr(res, "summary", str(res)),
                    "data": res}
        except Exception as ex:
            return {"agent": self.name, "ok": False, "summary": f"Finance Agent 失败: {ex}", "data": None}
