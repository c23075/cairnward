"""PaperQA2 worker: answers one question about a set of PDFs, through Cairnward only.

Runs with the engine's own Python (its virtual environment holds paper-qa and its 78
dependencies; Cairnward's core never imports them), inside the sandbox with network
access to Cairnward's gateway only. Standalone on purpose: no Cairnward imports.

stdin:  {"gateway": "http://127.0.0.1:8484/v1", "models": {"main", "fast", "embed"},
         "papers": [{"path", "docname", "title"}], "question", "cache", "evidence_k",
         "skip_summary"}
env:    CAIRNWARD_API_KEY (a run key)
stdout: a line "RESULT {json}" (answer, contexts) or "ERROR {json}".

Before anything runs, guard() checks the final settings: every model must be one of
Cairnward's virtual models and every API base the gateway; online metadata lookups
(use_doc_details) and image enrichment (multimodal) must be off; no other URL may
appear. A violation stops the worker (FR-RES-13); the sandbox would block it anyway.
"""

from __future__ import annotations

import json
import os
import pickle
import sys
from pathlib import Path
from typing import Any

PREFIX = "openai/cw/"
# Per model call. The library's default is 60 s with retries, but a single answer takes
# minutes on a CPU and may first wait in the gateway's queue.
CALL_TIMEOUT_S = 3600.0


def _walk(value: Any, path: str = "") -> Any:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, value


def guard(settings: dict[str, Any], gateway: str) -> list[str]:
    """Violations of 'everything through Cairnward' in a dumped Settings object."""
    problems = []
    origin = gateway.split("/v1")[0].rstrip("/")
    for path, value in _walk(settings):
        leaf = path.rsplit(".", 1)[-1]
        is_model = (leaf.endswith("llm") or leaf == "embedding") and isinstance(value, str)
        if is_model and not str(value).startswith(PREFIX):
            problems.append(f"{path} = {value!r} is not a Cairnward model")
        if leaf in ("api_base", "base_url") and value is not None and value != gateway:
            problems.append(f"{path} = {value!r} is not the gateway")
        if (
            isinstance(value, str)
            and value.startswith(("http://", "https://"))
            and not (value.startswith(origin + "/") or value == origin)
        ):
            problems.append(f"{path} = {value!r} points outside Cairnward")
    parsing = settings.get("parsing") or {}
    if parsing.get("use_doc_details") is not False:
        problems.append("parsing.use_doc_details must be False (no online metadata lookups)")
    if int(parsing.get("multimodal", 1)) != 0:
        problems.append("parsing.multimodal must be off (no image enrichment calls)")
    for name in ("llm_config", "summary_llm_config", "parsing.enrichment_llm_config",
                 "agent.agent_llm_config"):  # fmt: skip
        cfg: Any = settings
        for part in name.split("."):
            cfg = (cfg or {}).get(part) if isinstance(cfg, dict) else None
        if not cfg or not all((m.get("litellm_params") or {}).get("api_base") == gateway
                              for m in cfg.get("model_list", [{}])):  # fmt: skip
            problems.append(f"{name} must send every call to the gateway")
    emb = (settings.get("embedding_config") or {}).get("kwargs") or {}
    if emb.get("api_base") != gateway:
        problems.append("embedding_config must send every call to the gateway")
    return problems


def llm_config(model: str, gateway: str, key: str) -> dict[str, Any]:
    params = {"model": model, "api_base": gateway, "api_key": key, "timeout": CALL_TIMEOUT_S}
    return {"model_list": [{"model_name": model, "litellm_params": params}]}


def build_settings(spec: dict[str, Any], key: str) -> Any:
    from paperqa import Settings
    from paperqa.settings import AgentSettings, AnswerSettings, ParsingSettings

    gw = spec["gateway"]
    main, fast, embed = (PREFIX + spec["models"][k] for k in ("main", "fast", "embed"))
    main_cfg, fast_cfg = llm_config(main, gw, key), llm_config(fast, gw, key)

    return Settings(
        llm=main, llm_config=main_cfg,
        summary_llm=fast, summary_llm_config=fast_cfg,
        embedding=embed, embedding_config={"kwargs": {"api_base": gw, "api_key": key}},
        parsing=ParsingSettings(use_doc_details=False, multimodal=0, enrichment_llm=fast,
                                enrichment_llm_config=fast_cfg),
        agent=AgentSettings(agent_llm=main, agent_llm_config=main_cfg),
        answer=AnswerSettings(evidence_k=int(spec.get("evidence_k", 10)),
                              evidence_skip_summary=bool(spec.get("skip_summary", False)),
                              max_concurrent_requests=1),
    )  # fmt: skip


async def run(spec: dict[str, Any]) -> dict[str, Any]:
    from paperqa import Docs

    key = os.environ["CAIRNWARD_API_KEY"]
    settings = build_settings(spec, key)
    problems = guard(json.loads(json.dumps(settings.model_dump(), default=str)), spec["gateway"])
    if problems:
        raise PermissionError("refusing to run: " + "; ".join(problems))
    cache = Path(spec["cache"])
    docs = Docs()
    if cache.exists():
        with cache.open("rb") as fh:
            docs = pickle.load(fh)
    known = {d.docname for d in docs.docs.values()}
    added = 0
    for paper in spec["papers"]:
        if paper["docname"] not in known:
            await docs.aadd(Path(paper["path"]), citation=paper["title"],
                            docname=paper["docname"], settings=settings)  # fmt: skip
            added += 1
    if added:
        tmp = cache.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(docs, fh)
        tmp.replace(cache)
    session = await docs.aquery(spec["question"], settings=settings)
    contexts = []
    for c in session.contexts:
        contexts.append({"name": c.text.name, "docname": c.text.doc.docname,
                         "score": c.score, "summary": c.context, "text": c.text.text})  # fmt: skip
    return {"answer": session.answer, "formatted": session.formatted_answer,
            "contexts": contexts, "added": added}  # fmt: skip


def main() -> int:
    os.environ.update({
        "LITELLM_LOCAL_MODEL_COST_MAP": "True", "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1", "DO_NOT_TRACK": "1", "ANONYMIZED_TELEMETRY": "false",
    })  # fmt: skip
    spec = json.loads(sys.stdin.read())
    os.environ["OPENAI_API_KEY"] = os.environ.get("CAIRNWARD_API_KEY", "")
    os.environ["OPENAI_API_BASE"] = spec["gateway"]
    import asyncio

    try:
        result = asyncio.run(run(spec))
    except Exception as exc:  # reported to Cairnward, which shows it
        print("ERROR " + json.dumps({"error": f"{type(exc).__name__}: {exc}"[:2000]}), flush=True)
        return 1
    print("RESULT " + json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
