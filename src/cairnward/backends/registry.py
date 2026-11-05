"""Build drivers from backends.toml.

Local backends are dialled directly (loopback). LAN and hosted backends go through
cairnward-egress when an egress URL is available for the request's environment, so their
traffic is policy-checked and audited like any other outbound connection.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import httpx

from cairnward.backends.base import BackendError, Driver
from cairnward.backends.ollama import OllamaDriver
from cairnward.backends.openai_compat import OpenAICompatDriver
from cairnward.config.models import Backend, BackendsConfig


def resolve_secret(ref: str | None) -> str | None:
    """'env:VAR' or 'keyring:name' -> the secret (never stored in config files)."""
    if ref is None:
        return None
    kind, _, name = ref.partition(":")
    if kind == "env":
        value = os.environ.get(name)
        if not value:
            raise BackendError(f"environment variable {name} is not set", 500)
        return value
    if kind == "keyring":
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError:
            raise BackendError(
                "api_key uses the OS keychain but the 'keyring' package isn't installed", 500
            ) from None
        value = keyring.get_password("cairnward", name)
        if not value:
            raise BackendError(f"no keychain entry 'cairnward/{name}'", 500)
        return str(value)
    raise BackendError(f"unsupported secret reference '{ref}'", 500)


class Backends:
    def __init__(
        self,
        config: BackendsConfig,
        *,
        egress_url: str | None = None,
        egress_url_for: Callable[[str | None], str | None] | None = None,
        transport_for: Callable[[str], httpx.AsyncBaseTransport | None] | None = None,
        proxy_all: str | None = None,
    ):
        # proxy_all: inside the isolated core every connection, local ones included, goes
        # through srt's proxy (the sandbox has no other route).
        self.proxy_all = proxy_all
        self.config = config
        self._egress_for = egress_url_for or (lambda _env: egress_url)
        self._transport_for = transport_for or (lambda _name: None)
        self._drivers: dict[tuple[str, str | None], Driver] = {}

    def web_proxy(self, environment: str | None) -> str | None:
        """The proxy for Cairnward's own outbound requests (a plugin's fetches): egress, or
        srt's proxy inside the isolated core."""
        return self.proxy_all or self._egress_for(environment)

    def spec(self, name: str) -> Backend:
        try:
            return self.config.backends[name]
        except KeyError:
            raise BackendError(f"unknown backend '{name}'", 404) from None

    def get(self, name: str, environment: str | None = None) -> Driver:
        b = self.spec(name)
        if self.proxy_all:
            proxy: str | None = self.proxy_all
        else:
            proxy = self._egress_for(environment) if b.location != "local" else None
        key = (name, proxy)
        if key in self._drivers:
            return self._drivers[key]
        transport = self._transport_for(name)
        driver: Driver
        if b.driver == "ollama":
            driver = OllamaDriver(name, b.url, timeout_s=b.timeout_s, proxy=proxy,
                                  transport=transport)  # fmt: skip
        else:
            driver = OpenAICompatDriver(name, b.url, api_key=resolve_secret(b.api_key),
                                        timeout_s=b.timeout_s, proxy=proxy,
                                        transport=transport)  # fmt: skip
        self._drivers[key] = driver
        return driver

    async def aclose(self) -> None:
        for d in self._drivers.values():
            await d.aclose()
        self._drivers.clear()
