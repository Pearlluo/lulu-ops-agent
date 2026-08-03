"""LLM cost estimation tests — every default model must be priceable."""
import yaml
from pathlib import Path

from conversation_trace_logger import estimate_cost

AGENT_DIR = Path(__file__).resolve().parents[1] / "data" / "agent"


def test_default_models_have_pricing():
    reg = yaml.safe_load(open(AGENT_DIR / "model_registry.yaml", encoding="utf-8"))
    pricing = reg.get("pricing") or {}
    for role, cfg in reg["roles"].items():
        assert any(name in cfg["model"] for name in pricing), (
            f"role '{role}' model {cfg['model']} has no pricing entry"
        )


def test_estimate_cost_math():
    # 1M in + 1M out + 1M cache at deepseek-chat rates = 0.27 + 1.10 + 0.07
    c = estimate_cost({"in": 1_000_000, "out": 1_000_000, "cache_read": 1_000_000},
                      "deepseek/deepseek-chat")
    assert abs(c - 1.44) < 1e-6


def test_unknown_model_returns_none():
    assert estimate_cost({"in": 1000, "out": 10}, "somevendor/mystery-model") is None


def test_empty_tokens_returns_none():
    assert estimate_cost({}, "anthropic/claude-opus-4-8") is None


def test_fallback_label_format_tolerated():
    # runner appends ' -> provider/model (fallback)' on failover
    c = estimate_cost({"in": 1000, "out": 100},
                      "anthropic/claude-opus-4-8 -> openai/gpt-5-mini (fallback)")
    assert c is not None
