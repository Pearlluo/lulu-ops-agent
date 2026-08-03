"""Gold hot-reload (pull_gold_if_newer): TTL throttle, version compare,
re-sync only on version change. Offline — repository is faked."""
import blob_gold


class _FakeRepo:
    def __init__(self, version):
        self.version = version
        self.synced = 0

    def get_version(self):
        return self.version

    def sync_gold(self, dest):
        self.synced += 1
        return 44


def _setup(monkeypatch, tmp_path, version="2026-07-31T03:30:00"):
    repo = _FakeRepo(version)
    monkeypatch.setenv("CONTAINER_APP_NAME", "lulu-mcp")           # cloud gate
    monkeypatch.setattr(blob_gold, "get_repository", lambda: repo)
    monkeypatch.setattr(blob_gold, "GOLD_DIR", tmp_path)
    monkeypatch.setattr(blob_gold, "_MARKER", tmp_path / ".blob_sync")
    monkeypatch.setattr(blob_gold, "_VERSION_MARKER", tmp_path / ".blob_version")
    monkeypatch.setattr(blob_gold, "_VERSION_CHECK", tmp_path / ".blob_version_check")
    return repo


def test_syncs_when_version_unknown_then_throttles(monkeypatch, tmp_path):
    repo = _setup(monkeypatch, tmp_path)
    assert blob_gold.pull_gold_if_newer() == 44          # no version on disk -> sync
    assert repo.synced == 1
    assert blob_gold.pull_gold_if_newer() is False       # throttled by TTL marker
    assert repo.synced == 1


def test_no_resync_when_version_unchanged(monkeypatch, tmp_path):
    repo = _setup(monkeypatch, tmp_path)
    blob_gold.pull_gold_if_newer()
    (tmp_path / ".blob_version_check").unlink()          # expire the throttle
    assert blob_gold.pull_gold_if_newer() is False       # version unchanged -> no download
    assert repo.synced == 1


def test_resyncs_when_backend_version_moves(monkeypatch, tmp_path):
    repo = _setup(monkeypatch, tmp_path)
    blob_gold.pull_gold_if_newer()
    repo.version = "2026-07-31T04:45:00"                 # refresh finished, new gold
    (tmp_path / ".blob_version_check").unlink()
    assert blob_gold.pull_gold_if_newer() == 44
    assert repo.synced == 2
    assert (tmp_path / ".blob_version").read_text() == repo.version


def test_offcloud_is_noop(monkeypatch, tmp_path):
    repo = _setup(monkeypatch, tmp_path)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("CONTAINER_APP_REVISION", raising=False)
    assert blob_gold.pull_gold_if_newer() is None
    assert repo.synced == 0
