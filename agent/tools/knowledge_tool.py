"""Knowledge domain — semantic search over the company knowledge index (RAG).

Unlike the data tools this does NOT query Gold: its source is the prebuilt
vector index (knowledge_index.py) covering system docs, the automation
registry business cards, business definitions and company memory. Use it for
"how does X work / what system does Y / what does Z mean" questions; use the
data tools for counts, rows and dates."""
from ._base import ToolResult


class KnowledgeTool:
    name = "knowledge"

    def search_company_knowledge(self, query, top_k=5, user_role="default"):
        import knowledge_index

        try:
            hits = knowledge_index.search(query, top_k=int(top_k))
        except Exception as e:
            return ToolResult(tool=self.name, function="search_company_knowledge",
                              args={"query": query}, ok=False, summary=f"knowledge index error: {e}",
                              confidence="Low")
        if not hits:
            return ToolResult(tool=self.name, function="search_company_knowledge",
                              args={"query": query}, ok=True, data=[], row_count=0,
                              summary="No knowledge chunks matched. The index may need a rebuild "
                                      "(python knowledge_index.py build).",
                              confidence="Low")
        top = hits[0]["score"]
        return ToolResult(
            tool=self.name, function="search_company_knowledge",
            args={"query": query, "top_k": top_k}, ok=True,
            data=hits, row_count=len(hits),
            summary=f"{len(hits)} knowledge chunk(s); best match '{hits[0]['title']}' "
                    f"({hits[0]['source']}, score {top}).",
            confidence="High" if top >= 0.45 else "Medium",
            caveats=["Knowledge chunks are documentation, not live data — verify current "
                     "numbers with the data tools."],
        )
