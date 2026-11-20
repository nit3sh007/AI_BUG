import os

from . import env


def make_llm():
    """LLM_PROVIDER=gemini (default) or azure (any OpenAI-compatible endpoint, incl. Azure OpenAI/Foundry)."""
    provider = os.environ.get("LLM_PROVIDER", "gemini").lower()
    if provider in ("azure", "openai"):
        from .llm_openai import OpenAICompatLLM
        return OpenAICompatLLM()
    from .llm import GeminiLLM
    return GeminiLLM()
