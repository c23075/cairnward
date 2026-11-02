"""Searching online academic sources and fetching open-access PDFs, inside Cairnward's
rules (FR-RES-8/9): run by the core, which checks every request with the policy engine
(`net.connect` to the source's host, in the environment) and scans the query like any
outgoing text (redaction) before anything leaves; the egress report shows each
connection; `research.search` / `research.fetch` audit records hold a digest of the
query, never the query itself.

Fetching returns the PDFs (and CSL-JSON metadata) to the caller: in isolated mode the
core can't write into library folders, so the CLI saves them and indexes the library.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from urllib.parse import urlsplit

from cairnward.config.loader import Config
from cairnward.research.sources import HOSTS, SOURCES, Searcher, SourceError, slug


class OnlineError(Exception):
    pass


def enabled_sources(cfg: Config, environment: str, wanted: list[str] | None) -> list[str]:
    env = cfg.environments.get(environment)
    if env is None:
        raise OnlineError(f"unknown environment '{environment}'")
    allowed: list[str] = list(env.research_sources)
    if not allowed:
        raise OnlineError(f"online sources are off in '{environment}': add research_sources = "
                          f"[{', '.join(repr(s) for s in SOURCES)}] to its environment file, or "
                          "use the litreview-online environment")  # fmt: skip
    if not wanted:
        return allowed
    unknown = [s for s in wanted if s not in SOURCES]
    if unknown:
        raise OnlineError(f"unknown source(s) {', '.join(unknown)}: {', '.join(SOURCES)}")
    off = [s for s in wanted if s not in allowed]
    if off:
        raise OnlineError(f"not enabled in '{environment}': {', '.join(off)}")
    return wanted


class Online:
    """Bound to the core's policy engine and outbound proxy."""

    def __init__(self, cfg: Config, engine: Any, backends: Any, *, transport: Any = None) -> None:
        self.cfg, self.engine, self.backends = cfg, engine, backends
        self.transport = transport  # tests: an httpx mock transport

    def _searcher(self, environment: str, client: str, actor: str) -> Searcher:
        from cairnward.policy.engine import Action

        async def check(source: str, url: str) -> None:
            parts = urlsplit(url)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            action = Action(kind="net.connect", target=f"{parts.hostname}:{port}",
                            environment=environment, client=client, actor=actor,
                            details={"via": "research", "source": source})  # fmt: skip
            decision = await asyncio.to_thread(self.engine.check, action)
            if not decision.allowed:
                await asyncio.to_thread(self.engine.audit_deny, decision)
                raise SourceError(f"{source}: {parts.hostname} refused ({decision.reason})")

        return Searcher(self.cfg.paths.data_dir / "research_cache",
                        proxy=self.backends.web_proxy(environment), check=check,
                        transport=self.transport)  # fmt: skip

    def _redaction(self, environment: str, text: str, hosts: list[str], client: str) -> str | None:
        from cairnward.redaction import check_outbound

        verdict = check_outbound(self.engine, [text], destination=",".join(hosts),
                                 environment=environment, client=client,
                                 initiated_by="user")  # fmt: skip
        if not verdict.allowed:
            raise OnlineError(f"not sent: {verdict.reason}")
        return verdict.warning

    def _audit(self, action: str, environment: str, actor: str, **details: Any) -> None:
        if self.engine.audit:
            self.engine.audit.append(action, actor=actor, environment=environment,
                                     details=details)  # fmt: skip

    async def search(
        self,
        environment: str,
        query: str,
        *,
        sources: list[str] | None = None,
        limit: int = 10,
        client: str = "cli",
        actor: str = "user",
    ) -> dict[str, Any]:
        from cairnward.audit.log import digest

        query = query.strip()
        if len(query) < 3:
            raise OnlineError("the query is too short")
        chosen = enabled_sources(self.cfg, environment, sources)
        warning = await asyncio.to_thread(self._redaction, environment, query,
                                          [HOSTS[s] for s in chosen], client)  # fmt: skip
        s = self._searcher(environment, client, actor)
        try:
            out = await s.search(chosen, query, limit=max(1, min(limit, 50)))
        finally:
            await s.aclose()
        self._audit("research.search", environment, actor, query_digest=digest(query),
                    sources=out["sources"], results=len(out["records"]))  # fmt: skip
        if warning:
            out["warning"] = warning
        return out

    async def fetch(self, environment: str, ids: list[str], *, client: str = "cli",
                    actor: str = "user") -> list[dict[str, Any]]:  # fmt: skip
        """For each id (`doi:...`, `arxiv:...` or a DOI): metadata and the open-access PDF."""
        enabled_sources(self.cfg, environment, None)
        s = self._searcher(environment, client, actor)
        out: list[dict[str, Any]] = []
        try:
            for ident in ids[:50]:
                try:
                    rec = await s.lookup(ident)
                    pdf = await s.fetch_pdf(rec)
                except SourceError as exc:
                    out.append({"id": ident, "error": str(exc)})
                    continue
                out.append(
                    {
                        "id": ident,
                        "title": rec.title,
                        "filename": slug(rec) + ".pdf",
                        "csl": rec.csl(),
                        "pdf_b64": base64.b64encode(pdf).decode(),
                    }
                )
        finally:
            await s.aclose()
        self._audit("research.fetch", environment, actor, requested=len(ids),
                    fetched=sum("pdf_b64" in o for o in out))  # fmt: skip
        return out
