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
from .ollama_backend import OllamaAgent

#: All selectable backends, by id.
BACKENDS: dict[str, type[BaseAgent]] = {
    "anthropic": AnthropicAgent,
    "ollama": OllamaAgent,
}

#: Order tried when backend == "auto".
AUTO_ORDER: list[str] = ["anthropic", "ollama"]

__all__ = [
    "AnthropicAgent",
    "OllamaAgent",
    "BaseAgent",
    "ChatSettings",
    "BackendError",
    "BackendUnavailable",
    "BACKENDS",
    "AUTO_ORDER",
    "build_agent",
]


def build_agent(backend: str, manager: MCPManager, settings: ChatSettings) -> BaseAgent:
    """Return a ready agent for ``backend`` ("auto"/"anthropic"/"ollama").

    Raises :class:`BackendUnavailable` with a combined message if none work.
    """
    order = AUTO_ORDER if backend == "auto" else [backend]
    errors: list[str] = []
    for name in order:
        cls = BACKENDS[name]
        try:
            return cls.create(manager, settings)
        except BackendUnavailable as e:
            errors.append(f"{name} → {e}")
    raise BackendUnavailable(" | ".join(errors) or f"unknown backend: {backend}")
