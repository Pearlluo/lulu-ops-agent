"""DeepSeek provider — OpenAI-compatible API at api.deepseek.com.

Everything (wire format, tools, temperature) is inherited from OpenAIProvider;
only the identity and default endpoint differ. This is the whole point of the
gateway: a new provider is ~10 lines.
"""

from openai_provider import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    name = "deepseek"

    def __init__(self, model, config):
        config = dict(config or {})
        config.setdefault("base_url", "https://api.deepseek.com")
        super().__init__(model, config)
