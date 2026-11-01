# T1.1 PaperQA2 spike

Question: use PaperQA2 (FutureHouse, Apache-2.0) as Cairnward's literature engine, or build a lean pipeline of our own? Decision: [ADR-0014](../../docs/adr/0014-literature-engine.md).

## What was run (2026-11-01)
PaperQA2 2026.8.12 in its own virtual environment, configured so **every** model call went through Cairnward's gateway (`cw/litreview-offline:medium` for answers, `:low` for evidence summaries, `:embed` for embeddings), with an instrumented stand-in for Ollama that logs the size of each call. Five generated 4-page papers (~4,500 tokens each). One question.

Files: `fake_ollama_logging.py`, `make_pdfs.py`, `run_paperqa.py`.

## Findings
**Licences and size.** paper-qa is Apache-2.0; its default PDF parser is pypdf (BSD); PyMuPDF (AGPL) is only an optional extra. 78 packages, 242 MB, including LiteLLM, huggingface-hub, tiktoken and tantivy.

**Privacy defaults.** Out of the box it uses OpenAI for the answer, the evidence summaries and image enrichment (`gpt-4o`), OpenAI embeddings, and looks up paper metadata online (`parsing.use_doc_details = True`). All of it can be redirected; a missed setting would leak data.

**Offline.** With `use_doc_details = False`, `multimodal = 0` and `LITELLM_LOCAL_MODEL_COST_MAP=True`, it ran to completion **inside the sandbox with only Cairnward's gateway reachable**.

**Work per question (measured)**

| Mode | Calls | Input tokens |
|---|---|---|
| Default (`evidence_k = 10`) | 10 evidence summaries (fast model) + 1 answer (main) | ≈ 13,500 + ≈ 1,200 |
| `evidence_skip_summary`, `evidence_k = 5` | 1 answer | ≈ 6,600 |
| Indexing 5 papers + question | 6 embedding calls | ≈ 24,000 |

**Projected time per question on the reference machine** (Ryzen 3 3200G; 30B reads ~20 tok/s, writes ~6–11; 1.7B reads ~50, writes ~10–17; docs/bench/):

| Mode | Estimate |
|---|---|
| Default, summaries on 1.7B | ≈ 8–10 min (incl. switching models) |
| Default, summaries on 30B | ≈ 17 min |
| Skip summaries, 5 chunks | ≈ 6 min, prompt close to the 8K context default |
| Lean pipeline: 6 chunks of ~350 tokens, one answer | ≈ 2–3 min |

**Not measured here:** answer quality (the stand-in model returns canned text). PaperQA2's per-chunk summaries are its main quality feature in published results with large hosted models; whether a 1.7B summariser keeps that benefit is open. `run_paperqa.py` can be pointed at the real gateway on the reference machine to measure quality and time.
