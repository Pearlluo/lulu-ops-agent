"""Knowledge index (RAG) tests. The prebuilt index parquet ships with the
repo, so these run in CI too — keyword mode needs no API key."""
from pathlib import Path

import pandas as pd

INDEX = Path(__file__).resolve().parents[1] / "data" / "agent" / "knowledge" / "knowledge_index.parquet"


def test_index_exists_and_has_contract_columns():
    assert INDEX.exists(), "knowledge_index.parquet missing — run: python knowledge_index.py build"
    df = pd.read_parquet(INDEX)
    assert len(df) >= 20
    for col in ("source", "title", "text", "embedding"):
        assert col in df.columns


def test_keyword_search_finds_timesheet_automation():
    import knowledge_index

    hits = knowledge_index.search("weekly timesheet automation email", top_k=5, mode="keyword")
    assert hits, "keyword search returned nothing"
    assert any("timesheet" in (h["title"] + h["text"]).lower() for h in hits)


def test_knowledge_tool_returns_toolresult():
    from tools.knowledge_tool import KnowledgeTool

    r = KnowledgeTool().search_company_knowledge("what does the gap tracker do", top_k=3)
    assert r.ok
    assert r.row_count > 0
    assert r.caveats
