"""Business-tool registry. Every tool routes through tools._base -> sql_validator -> DuckDB -> Gold."""

from .people_tool import PeopleTool
from .training_tool import TrainingTool
from .roster_tool import RosterTool
from .timesheet_tool import TimesheetTool
from .project_tool import ProjectTool
from .inventory_asset_tool import InventoryAssetTool
from .finance_tool import FinanceTool
from .hseq_tool import HseqTool
from .insight_tool import InsightTool
from .automation_tool import AutomationTool
from .file_tool import FileTool
from .fds_tool import FdsTool


def build_tools():
    return {
        "people": PeopleTool(),
        "training": TrainingTool(),
        "roster": RosterTool(),
        "timesheet": TimesheetTool(),
        "project": ProjectTool(),
        "inventory_asset": InventoryAssetTool(),
        "finance": FinanceTool(),
        "hseq": HseqTool(),
        "insight": InsightTool(),
        "automation": AutomationTool(),
        "files": FileTool(),
        "fds": FdsTool(),
    }
