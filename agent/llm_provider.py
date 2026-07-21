"""
LLM Gateway core — provider-agnostic interface + model routing.

Every provider implements one method:

    chat(system, messages, tools=None, max_tokens=4096, temperature=None) -> LLMResponse

over a CANONICAL conversation format, so the agent loop never knows which vendor is behind it:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [{"id","name","args"}], "_raw": {...}}
    {"role": "tool_results", "results": [{"id","content","is_error"}]}

Canonical tool definitions use JSON Schema (name / description / input_schema) — each provider
translates to its own wire format. `_raw` lets a provider round-trip its native blocks
(e.g. Anthropic thinking signatures) without leaking vendor format into the loop.

Routing: model_registry.yaml (defaults + provider config) <- admin_settings.json (panel overrides).
Switching planner/answer/fallback = config change only. Tools / validator / Gold untouched.
"""

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = AGENT_DIR / "model_registry.yaml"
SETTINGS_PATH = AGENT_DIR / "admin_settings.json"


# ---------------- canonical response types ----------------
@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list = field(default_factory=list)   # [ToolCall]
    stop: str = "end"                                 # "tool_use" | "end"
    raw: object = None                                # provider-native assistant payload (for _raw echo)
    usage: dict = field(default_factory=dict)


# ---------------- provider base ----------------
class LLMProvider(ABC):
    name = "base"

    def __init__(self, model, config):
        self.model = model
        self.config = config or {}
        self.api_key = os.getenv(self.config.get("api_key_env", "")) or self._key_from_dotenv()

    def _key_from_dotenv(self):
        envfile = AGENT_DIR.parent / "Raw Data" / "API" / "credential" / ".env"
        if envfile.exists():
            from dotenv import dotenv_values
            return dotenv_values(envfile).get(self.config.get("api_key_env", ""))
        return None

    def available(self):
        return bool(self.api_key)

    @abstractmethod
    def chat(self, system, messages, tools=None, max_tokens=4096, temperature=None) -> LLMResponse:
        ...

    def label(self):
        return f"{self.name}/{self.model}"


# ---------------- config loading ----------------
def load_registry():
    return yaml.safe_load(open(REGISTRY_PATH, encoding="utf-8"))


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def resolve_role(role):
    """role ('planner'|'answer'|'fallback') -> (provider_name, model, provider_config)."""
    reg = load_registry()
    settings = load_settings()
    override = settings.get(f"{role}_model")
    if override and "/" in override:
        pname, model = override.split("/", 1)
    else:
        r = (reg.get("roles") or {}).get(role) or {}
        pname, model = r.get("provider"), r.get("model")
    pconf = (reg.get("providers") or {}).get(pname, {})
    return pname, model, pconf


_provider_cache = {}


def get_provider(role) -> LLMProvider:
    """Instantiate (and cache) the provider configured for a role."""
    pname, model, pconf = resolve_role(role)
    key = (pname, model)
    if key in _provider_cache:
        return _provider_cache[key]
    if pname == "anthropic":
        from anthropic_provider import AnthropicProvider
        p = AnthropicProvider(model, pconf)
    elif pname == "deepseek":
        from deepseek_provider import DeepSeekProvider
        p = DeepSeekProvider(model, pconf)
    elif pname in ("openai", "local"):
        from openai_provider import OpenAIProvider
        p = OpenAIProvider(model, pconf, name=pname)
    else:
        raise ValueError(f"Unknown provider '{pname}' for role '{role}'")
    _provider_cache[key] = p
    return p


def gateway_status():
    """What the admin panel shows: per-role provider/model + key availability."""
    out = {}
    settings = load_settings()
    for role in ("planner", "answer", "fallback"):
        pname, model, pconf = resolve_role(role)
        key_env = pconf.get("api_key_env", "")
        has_key = bool(os.getenv(key_env)) or bool(LLMProvider._key_from_dotenv(
            type("x", (), {"config": pconf})()))
        out[role] = {"provider": pname, "model": model, "api_key_env": key_env,
                     "available": has_key}
    out["temperature"] = settings.get("temperature")
    out["two_stage"] = settings.get("two_stage", "auto")
    return out
