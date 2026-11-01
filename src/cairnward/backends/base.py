"""Backend driver interface (spec §4.1, ADR-0004).

The gateway speaks the OpenAI chat-completions format; drivers translate to their
server and back. Requests carry the client's messages untouched (prefix stability,
FR-GW-7): drivers only add server options (model, context size, thinking, sampling).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


class BackendError(Exception):
    """The backend answered with an error."""

    def __init__(self, message: str, status: int = 502):
        self.status = status
        super().__init__(message)


class BackendUnavailable(BackendError):
    """The backend couldn't be reached."""

    def __init__(self, message: str):
        super().__init__(message, 502)


@dataclass(frozen=True)
class GenerationOptions:
    """What an effort level and environment set for one request (spec §4.2)."""

    thinking: bool | None = None
    context_tokens: int | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    keep_alive: str | None = None  # Ollama: how long to keep the model loaded


@dataclass
class ChatRequest:
    model: str  # the backend's model name, e.g. qwen3:30b
    messages: list[dict[str, Any]]
    options: GenerationOptions = field(default_factory=GenerationOptions)
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    # Other OpenAI fields from the client (stop, seed, response_format, ...), passed on.
    extra: dict[str, Any] = field(default_factory=dict)


class Driver(Protocol):
    name: str

    async def chat(self, req: ChatRequest) -> dict[str, Any]:
        """A complete OpenAI `chat.completion` object."""
        ...

    def chat_stream(self, req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
        """OpenAI `chat.completion.chunk` objects; the last carries `usage` when known."""
        ...

    async def embed(self, model: str, inputs: list[str]) -> tuple[list[list[float]], int]:
        """Embedding vectors for `inputs`, and the number of input tokens (0 if unknown)."""
        ...

    async def models(self) -> list[str]: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...
