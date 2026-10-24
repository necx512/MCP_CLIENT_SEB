"""Chat backends and the factory that selects one.

Each backend is a :class:`BaseAgent` subclass exposing the same interface:
``.create(manager, settings)`` returns a ready agent or raises
``BackendUnavailable``. Adding a provider = drop a module here and register its
class in ``BACKENDS`` (and ``AUTO_ORDER`` if it should be auto-detected).
"""

from __future__ import annotations

from ..manager import MCPManager
from .anthropic_backend import AnthropicAgent
from .base import BackendError, BackendUnavailable, BaseAgent, ChatSettings
from .copilot_backend import CopilotAgent
from .ollama_backend import OllamaAgent
from .vllm_backend import VLLMAgent

#: All selectable backends, by id.
BACKENDS: dict[str, type[BaseAgent]] = {
    "anthropic": AnthropicAgent,
    "ollama": OllamaAgent,
    "vllm": VLLMAgent,
    "copilot": CopilotAgent,
}

#: Order tried when backend == "auto". Copilot is left out: its auth can only be
#: checked by starting its runtime, so it must be chosen explicitly.
AUTO_ORDER: list[str] = ["anthropic", "ollama", "vllm"]

__all__ = [
    "AnthropicAgent",
    "OllamaAgent",
    "VLLMAgent",
    "CopilotAgent",
    "BaseAgent",
    "ChatSettings",
    "BackendError",
    "BackendUnavailable",
    "BACKENDS",
    "AUTO_ORDER",
    "build_agent",
]


def build_agent(backend: str, manager: MCPManager, settings: ChatSettings) -> BaseAgent:
    """Return a ready agent for ``backend`` ("auto"/"anthropic"/"ollama"/"vllm"/"copilot").

    Raises :class:`BackendUnavailable` with a combined message if none work.
    """
    order = AUTO_ORDER if backend == "auto" else [backend]
    errors: list[str] = []
    for name in order:
        cls = BACKENDS[name]
        try:
            agent = cls.create(manager, settings)
        except BackendUnavailable as e:
            errors.append(f"{name} → {e}")
            continue
        if settings.system_prompt:
            # Config-supplied prompt overrides the default; reset() so backends
            # that bake the system turn into history (Ollama) pick it up.
            agent.system_prompt = settings.system_prompt
            agent.reset()
        return agent
    raise BackendUnavailable(" | ".join(errors) or f"unknown backend: {backend}")
