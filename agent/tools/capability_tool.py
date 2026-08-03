"""Capability domain — governed business capabilities (registry-backed).

Thin tool adapter: business logic lives in capabilities/ (canonical, §18);
this class only wraps results into ToolResult for the agent/MCP surfaces."""
from ._base import ToolResult


class CapabilityTool:
    name = "capability"

    def project_hours_status(self, job_ref, user_role="default"):
        import sys
        from pathlib import Path
        agent_dir = str(Path(__file__).resolve().parent.parent)
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        from capabilities.project_hours_status import compute

        try:
            result = compute(job_ref, user_role=user_role)
        except Exception as e:
            return ToolResult(tool=self.name, function="project_hours_status",
                              args={"job_ref": job_ref}, ok=False,
                              summary=f"capability error: {e}", confidence="Low")
        ok = not any("not found" in x for x in result["exceptions"])
        return ToolResult(
            tool=self.name, function="project_hours_status",
            args={"job_ref": job_ref}, ok=ok,
            data=[result], row_count=1,
            summary=(result["facts"][0] if ok and result["facts"]
                     else "; ".join(result["exceptions"]) or "no result"),
            confidence=result["confidence"].capitalize(),
            caveats=result["warnings"] + result["insufficient_data"],
        )
