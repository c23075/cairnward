# ADR-0014: Literature question-answering engine

- Status: Proposed (awaiting owner decision)
- Date: 2026-11-01
- Spec: §4.6; FR-RES-1, FR-RES-2, FR-RES-12
- Evidence: [spikes/t11-paperqa](../../spikes/t11-paperqa/README.md)

## Context
M1 needs `lit.ask` (answers with page citations) and overnight review jobs on a CPU-only machine where reading a prompt costs ~1 minute per 1,200 tokens on the main model. PaperQA2 is the best-known open-source engine for this.

## Findings (summary)
- Works fully through Cairnward's gateway and fully offline in the sandbox, once its hosted-model defaults and online metadata lookups are switched off.
- Default mode costs ~11 model calls and ~15K input tokens per question: ≈ 8–10 min per question here (≈ 17 min with summaries on the 30B model).
- 78 dependencies (242 MB), including LiteLLM and huggingface-hub; licences fine (pypdf by default, not PyMuPDF).
- Quality with small local models not yet measured.

## Options
1. **PaperQA2 as the engine.** Mature retrieval + per-chunk summaries; heavy dependencies inside Cairnward; slow per question on CPU; dangerous defaults to override and keep overridden across upgrades.
2. **Own lean pipeline.** pypdfium2 parsing, small chunks with page numbers, sqlite-vec retrieval, one answer prompt with page citations, citation check. ≈ 2–3 min per question; full control over prompts, context size and prefix caching; quality must be built and evaluated by us.
3. **Both: own pipeline by default, PaperQA2 as an optional "deep" engine.** Lean pipeline for interactive `lit.ask`; PaperQA2 runs as a separate, sandboxed worker process (its dependencies never enter Cairnward's core), configured by Cairnward with all calls through the gateway, for overnight jobs where its per-chunk summaries can be afforded.

## Recommendation
Option 3, built in this order: the lean pipeline first (M1), the PaperQA2 worker as an optional add-on once the lean pipeline's evals exist (so the two can be compared on the same questions with the real model). Borrow PaperQA2's ideas where they help regardless: an optional per-chunk "evidence" pass on the fast model for jobs, explicit "cannot answer from these sources" behaviour, and citation keys tied to page ranges.

## Decision
Pending.
