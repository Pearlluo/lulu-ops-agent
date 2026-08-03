"""GoldRepository backend-selection and fail-safe tests. Fully offline —
neither backend is configured in CI, and every method must degrade to
None/[]/False rather than raise."""
import pytest

import gold_repository as gr
import blob_gold


def test_default_backend_is_azure_blob(monkeypatch):
    monkeypatch.delenv("GOLD_BACKEND", raising=False)
    gr._repo_cache.clear()
    assert gr.get_repository().name == "azure_blob"


def test_env_selects_onelake(monkeypatch):
    monkeypatch.setenv("GOLD_BACKEND", "onelake")
    gr._repo_cache.clear()
    assert gr.get_repository().name == "onelake"
    gr._repo_cache.clear()


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        gr.get_repository("s3")


def test_both_implementations_satisfy_protocol():
    assert isinstance(gr.AzureBlobGoldRepository(), gr.GoldRepository)
    assert isinstance(gr.OneLakeGoldRepository(), gr.GoldRepository)


@pytest.mark.parametrize("backend", ["azure_blob", "onelake"])
def test_unconfigured_backend_fails_safe(monkeypatch, backend, tmp_path):
    for k in ("BLOB_CONNECTION_STRING", "AZURE_STORAGE_CONNECTION_STRING",
              "AZURE_BLOB_CONNECTION_STRING", "STORAGE_CONNECTION_STRING",
              "ONELAKE_WORKSPACE", "ONELAKE_LAKEHOUSE", "ONELAKE_TENANT_ID",
              "ONELAKE_CLIENT_ID", "ONELAKE_CLIENT_SECRET",
              "SHAREPOINT_TENANT_ID", "SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    repo = gr._BACKENDS[backend]()
    assert repo.sync_gold(tmp_path) is None
    assert repo.sync_state(tmp_path) is None
    assert repo.list_tables() == []
    assert repo.get_version() is None
    assert repo.health_check() is False


def test_facade_noop_outside_cloud(monkeypatch):
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("CONTAINER_APP_REVISION", raising=False)
    assert blob_gold.pull_gold(force=True) is None
    assert blob_gold.pull_state(force=True) is None
