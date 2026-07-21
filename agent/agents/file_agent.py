"""File Processing Agent — SharePoint/IMS files: classify, parse, structure.

Reuses attachment_analyzer.py (PDF/Excel/Word/image parsing — already built).
IMS document-library traversal + FDD RAG are scaffolded TODO (Phase A pull pending,
Phase B for de-sensitise/rename/archive writes).
"""

from base_agent import BaseAgent


class FileAgent(BaseAgent):
    name = "file"
    title = "File Processing"
    layer = "file"
    domain = "文件处理 — SharePoint/IMS 文件分类、PDF/Excel/Word 解析、生成结构化数据"
    keywords = ["文件", "file", "pdf", "excel", "word", "文档", "document", "表单", "form",
                "ims", "folder", "文件夹", "解析", "parse", "归档", "分类", "classify"]
    owns_tools = ["attachment_analyzer", "azure_ops_tool(graph)"]
    read_actions = ["parse_attachment", "classify", "ims_tree(pending)"]
    write_actions = ["desensitise", "rename", "archive"]        # Phase B — approval gate

    def status(self) -> dict:
        return {"agent": self.name, "ok": True,
                "summary": "文件解析就绪 (attachment_analyzer)；IMS 遍历 + FDD RAG 待建",
                "alerts": []}

    def handle(self, request: str, role: str = "default") -> dict:
        return {"agent": self.name, "ok": True,
                "summary": "File Agent: 拖入 PDF/Excel/图片可解析；IMS 文件夹遍历待 Admin 批准后开建",
                "data": None}
