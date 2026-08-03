"""Business Capability layer (platform baseline §15).

Capabilities are the governed, canonical implementations of business questions.
Tools/MCP expose capabilities; capabilities own the single authoritative logic
so LuLu, Power BI, the Hours Remaining site and any future consumer compute
the SAME number from the SAME definition (§18)."""
from pathlib import Path

import yaml

CAP_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = CAP_DIR / "registry.yaml"

_registry_cache = None


def load_capability_registry(force=False):
    global _registry_cache
    if _registry_cache is None or force:
        _registry_cache = yaml.safe_load(open(REGISTRY_PATH, encoding="utf-8"))
    return _registry_cache
