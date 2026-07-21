"""Orchestrator — the control layer over the 4 specialist agents.

Responsibilities (the boss's '管理总控'):
  1. system_overview()  — collect every agent's status (看所有系统状态)
  2. route(text)        — pick the right specialist agent (分配任务)
  3. handle(text)       — run it; WRITE actions stop at the approval gate (修复 vs 报警)
  4. trace()            — append every decision/action to logs/orchestrator_trace.jsonl

The 4 specialists own their domains; Business/Finance already exist as Gold tools,
Azure Ops is live (read-only), File is scaffolded. Write actions are never executed
without explicit approval (Phase B).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_DIR = HERE.parent
for _p in (str(HERE), str(AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ops_agent import AzureOpsAgent              # noqa: E402
from file_agent import FileAgent                 # noqa: E402
from business_agent import BusinessAgent         # noqa: E402
from finance_agent import FinanceAgent           # noqa: E402

TRACE = AGENT_DIR / "logs" / "orchestrator_trace.jsonl"
WRITE_VERBS = ["restart", "重启", "rerun", "重跑", "rename", "改名", "archive", "归档",
               "delete", "删除", "desensitise", "去敏", "scale", "扩容"]


class Orchestrator:
    name = "orchestrator"
    title = "Orchestrator · AI Control Layer"

    def __init__(self):
        self.agents = [AzureOpsAgent(), FileAgent(), BusinessAgent(), FinanceAgent()]
        self.by_name = {a.name: a for a in self.agents}

    # ---- 1. 看所有系统状态 ----
    def system_overview(self) -> dict:
        cards, alerts = [], []
        for a in self.agents:
            s = a.status()
            cards.append({**a.card(), "ok": s.get("ok"), "summary": s.get("summary")})
            alerts += s.get("alerts", [])
        return {"healthy": not alerts, "alerts": alerts, "agents": cards}

    # ---- 2. 分配任务 (route) ----
    def route(self, text: str):
        scored = sorted(((a.can_handle(text), a) for a in self.agents),
                        key=lambda x: x[0], reverse=True)
        best_score, best = scored[0]
        return (best if best_score > 0 else self.by_name["business"]), best_score

    # ---- 3. 决定: 执行 vs 报警 (approval gate on writes) ----
    def handle(self, text: str, role: str = "default", approved: bool = False) -> dict:
        agent, score = self.route(text)
        wants_write = any(v in (text or "").lower() for v in WRITE_VERBS)
        if wants_write and agent.write_actions and not approved:
            res = {"agent": agent.name, "ok": False, "gated": True,
                   "summary": f"⚠ 这是写操作（{agent.title}），需审批后执行。可执行: {agent.write_actions}"}
        else:
            res = agent.handle(text, role)
        self.trace({"ts": datetime.now(timezone.utc).isoformat(), "request": text, "role": role,
                    "routed_to": agent.name, "score": round(score, 2),
                    "gated": res.get("gated", False), "ok": res.get("ok")})
        return res

    # ---- 4. trace ----
    def trace(self, rec: dict):
        try:
            TRACE.parent.mkdir(parents=True, exist_ok=True)
            with open(TRACE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")    # Windows console
    o = Orchestrator()
    print("=" * 60, "\nORCHESTRATOR · SYSTEM OVERVIEW\n" + "=" * 60)
    ov = o.system_overview()
    for c in ov["agents"]:
        flag = "🟢" if c["ok"] else "🟡"
        print(f"\n{flag} {c['title']}  [{c['layer']}]")
        print(f"   {c['summary']}")
        if c["write_gated"]:
            print(f"   🔒 写操作(待审批): {', '.join(c['write_gated'])}")
    print("\n" + "=" * 60)
    print("ALERTS:", ov["alerts"] or "none")
    print("\n--- routing demo ---")
    for q in ["夜间刷新挂了吗", "NWM明天谁能去", "上周timesheet", "这条报价毛利多少", "重启lulu-app"]:
        a, sc = o.route(q)
        print(f"  '{q}'  ->  {a.name} ({sc:.1f})")
