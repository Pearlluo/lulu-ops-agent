"""Pytest bootstrap: put data/agent on sys.path and expose data-lake availability.

CI has no Gold lake (data/gold is gitignored; the real source is Azure blob),
so tests that execute DuckDB queries or build name dictionaries from parquet
are skipped automatically when the lake is absent.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "data" / "agent"
GOLD_DIR = REPO_ROOT / "data" / "gold"

sys.path.insert(0, str(AGENT_DIR))

GOLD_AVAILABLE = GOLD_DIR.is_dir() and any(GOLD_DIR.glob("*.parquet"))

requires_gold = pytest.mark.skipif(
    not GOLD_AVAILABLE, reason="Gold parquet lake not present (CI environment)"
)
