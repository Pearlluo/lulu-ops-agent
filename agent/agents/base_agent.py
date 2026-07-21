"""BaseAgent — the contract every specialist agent under the Orchestrator implements.

A specialist agent owns ONE domain. It exposes:
  - status()      → a health/summary dict for the Orchestrator's system overview
  - can_handle()  → a 0..1 routing score for a free-text request
  - handle()      → do the work (read), or describe the write it WOULD do (gated)

Write actions are declared but NOT executed here — the Orchestrator runs them only
after its approval gate (Phase B). Read actions are free.
"""

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent          # .../data/agent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


class BaseAgent:
    name = "base"
    title = "Base Agent"
    domain = ""                 # one-line description of what this agent owns
    layer = ""                  # ops | file | business | finance
    keywords = []               # routing hints (zh + en)
    owns_tools = []             # existing tool modules this agent fronts
    read_actions = []           # things it can do now (no approval)
    write_actions = []          # things that need the Orchestrator approval gate (Phase B)
    read_only = True

    def status(self) -> dict:
        """Health/summary for the Orchestrator overview. Override per agent."""
        return {"agent": self.name, "ok": True, "summary": self.domain, "alerts": []}

    def can_handle(self, text: str) -> float:
        if not text:
            return 0.0
        t = str(text).lower()
        hits = sum(1 for k in self.keywords if k.lower() in t)
        return min(1.0, hits / 2.0) if hits else 0.0

    def handle(self, request: str, role: str = "default") -> dict:
        return {"agent": self.name, "ok": False,
                "summary": f"[{self.name}] handle() not implemented yet", "data": None}

    def card(self) -> dict:
        """Static descriptor for the Orchestrator / UI."""
        return {"name": self.name, "title": self.title, "layer": self.layer,
                "domain": self.domain, "owns": self.owns_tools,
                "read": self.read_actions, "write_gated": self.write_actions}
