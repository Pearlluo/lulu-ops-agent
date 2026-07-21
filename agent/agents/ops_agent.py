"""Azure Ops Agent — monitors the Azure estate (read-only). Phase-B: restart/rerun via gate."""

from base_agent import BaseAgent
from tools.azure_ops_tool import AzureOpsTool


class AzureOpsAgent(BaseAgent):
    name = "azure_ops"
    title = "Azure Ops"
    layer = "ops"
    domain = "运维监控 — Azure Functions/App/Container Job、Blob/SQL、夜间刷新状态"
    keywords = ["azure", "运维", "ops", "刷新", "refresh", "job", "container", "容器",
                "function", "app service", "lulu-app", "lulu-refresh", "系统状态", "system status",
                "部署", "deploy", "down", "挂", "失败", "fail", "restart", "重启", "rerun", "重跑",
                "blob", "sql", "monitor", "监控"]
    owns_tools = ["azure_ops_tool"]
    read_actions = ["health_summary", "refresh_status", "app_status", "estate"]
    write_actions = ["restart_app", "rerun_job", "scale"]      # Phase B — approval gate

    def __init__(self):
        self.tool = AzureOpsTool()

    def status(self) -> dict:
        h = self.tool.health_summary()
        return {"agent": self.name, "ok": h["ok"], "summary": h["summary"], "alerts": h["alerts"]}

    def handle(self, request: str, role: str = "default") -> dict:
        t = (request or "").lower()
        if any(k in t for k in ["刷新", "refresh", "夜间", "nightly"]):
            r = self.tool.refresh_status()
        elif any(k in t for k in ["app", "应用", "容器", "container"]):
            r = self.tool.app_status()
        elif any(k in t for k in ["资源", "estate", "多少", "规模"]):
            r = self.tool.estate()
        else:
            r = self.tool.health_summary()
        return {"agent": self.name, "ok": r.get("ok", True), "summary": r["summary"], "data": r}
