#!/usr/bin/env python3
"""Index the PDFs and ask one question with PaperQA2, all through Cairnward's gateway.

Usage: run_paperqa.py PDF_DIR GATEWAY_URL API_KEY [evidence_k] [skip_summary]
"""

import asyncio
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from paperqa import Docs, Settings
from paperqa.settings import AnswerSettings, ParsingSettings

pdf_dir, gw, key = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
evidence_k = int(sys.argv[4]) if len(sys.argv) > 4 else 10
skip_summary = len(sys.argv) > 5 and sys.argv[5] == "skip"
MAIN, FAST, EMBED = (
    "openai/cw/litreview-offline:medium",
    "openai/cw/litreview-offline:low",
    "openai/cw/litreview-offline:embed",
)


def llm_config(model: str) -> dict:
    return {
        "model_list": [
            {
                "model_name": model,
                "litellm_params": {"model": model, "api_base": gw, "api_key": key},
            }
        ]
    }


settings = Settings(
    llm=MAIN,
    llm_config=llm_config(MAIN),
    summary_llm=FAST,
    summary_llm_config=llm_config(FAST),
    embedding=EMBED,
    embedding_config={"kwargs": {"api_base": gw, "api_key": key}},
    parsing=ParsingSettings(use_doc_details=False, multimodal=0),
    answer=AnswerSettings(
        evidence_k=evidence_k, evidence_skip_summary=skip_summary, max_concurrent_requests=1
    ),
)


async def main() -> None:
    docs = Docs()
    t = time.monotonic()
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        await docs.aadd(pdf, citation=f"{pdf.stem}, 2024", docname=pdf.stem, settings=settings)
    print(f"indexed {len(docs.docs)} docs in {time.monotonic() - t:.1f}s")
    t = time.monotonic()
    session = await docs.aquery("How does sleep relate to working memory?", settings=settings)
    print(f"answered in {time.monotonic() - t:.1f}s")
    print("ANSWER:", session.answer[:300])


asyncio.run(main())
