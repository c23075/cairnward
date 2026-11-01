"""Model backend drivers (Ollama native, OpenAI-compatible)."""

from cairnward.backends.base import (
    BackendError,
    BackendUnavailable,
    ChatRequest,
    Driver,
    GenerationOptions,
)
from cairnward.backends.ollama import OllamaDriver
from cairnward.backends.openai_compat import OpenAICompatDriver
from cairnward.backends.registry import Backends, resolve_secret

__all__ = [
    "BackendError",
    "BackendUnavailable",
    "Backends",
    "ChatRequest",
    "Driver",
    "GenerationOptions",
    "OllamaDriver",
    "OpenAICompatDriver",
    "resolve_secret",
]
