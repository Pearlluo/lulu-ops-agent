"""invoice_register missing (Xero unconnected): DuckDB raises CatalogException;
the finance tool must answer insufficient_data instead of crashing."""
from tools.finance_tool import FinanceTool


def _boom(self, *a, **k):
    raise RuntimeError("Catalog Error: Table with name invoice_register does not exist!")


def test_outstanding_invoices_degrade_gracefully(monkeypatch):
    monkeypatch.setattr(FinanceTool, "_query", _boom)
    res = FinanceTool().get_outstanding_invoices(user_role="Finance")
    assert not res.ok
    assert "insufficient_data" in res.summary
    assert any("Xero" in c for c in res.caveats)


def test_project_revenue_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(FinanceTool, "_query", _boom)
    res = FinanceTool().get_project_revenue("SH-26046", user_role="default")
    assert not res.ok and "insufficient_data" in res.summary


def test_unrelated_errors_still_raise(monkeypatch):
    def other(self, *a, **k):
        raise RuntimeError("something else broke")
    monkeypatch.setattr(FinanceTool, "_query", other)
    try:
        FinanceTool().get_project_revenue("SH-26046")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "something else" in str(e)
