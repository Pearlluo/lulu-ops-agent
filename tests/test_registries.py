"""All YAML registries must parse and carry the fields the runtime relies on.
Fully offline — catches a bad hand-edit before it ships in an image."""
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parents[1] / "data" / "agent"

REGISTRY_FILES = [
    "agent_registry.yaml",
    "automation_registry.yaml",
    "business_definitions.yaml",
    "entity_aliases.yaml",
    "event_registry.yaml",
    "example_question_mapping.yaml",
    "model_registry.yaml",
]


def test_all_registries_parse():
    for name in REGISTRY_FILES:
        p = AGENT_DIR / name
        assert p.exists(), f"{name} missing"
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data, f"{name} parsed empty"


def test_model_registry_providers_have_key_env():
    with open(AGENT_DIR / "model_registry.yaml", encoding="utf-8") as f:
        reg = yaml.safe_load(f)
    providers = reg.get("providers", {})
    assert providers, "model_registry has no providers"
    for name, cfg in providers.items():
        assert cfg.get("api_key_env"), f"provider {name} missing api_key_env"


def test_regression_cases_parse():
    p = AGENT_DIR / "tests" / "regression_cases.yaml"
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        cases = yaml.safe_load(f)
    assert cases is not None
