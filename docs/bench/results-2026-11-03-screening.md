# Review screening, 2026-11-03

Why screening moved from the fast model to the main model (D63). `evals/litreview_table`'s
four synthetic papers, called directly on local Ollama, AMD Ryzen 3 3200G on CPU. Three papers
should be kept and one (glacier) is off topic.

| Model | Reasoning | Prompt | Relevant papers | Glacier | Note |
|---|---|---|---|---|---|
| `qwen3:1.7b` | off | old | excluded, 18 of 18 tries | excluded | With or without the eval's filler text |
| `qwen3:1.7b` | off | reasoning first | closest kept, other two excluded (15 tries) | excluded | |
| `qwen3:4b` | off | old | "unsure" | mostly "unsure" | Reasons in the reply, then cut off |
| `qwen3:30b` | off | old | "unsure" | excluded | **Cut off or barely finished: not a real judgement** |
| `qwen3:30b` | on | old | 2 kept, nurses paper excluded (one try each) | excluded | |
| `qwen3:30b` | on | earlier wording | 9 of 9 at first; **napping paper excluded on 2 of 4** later | excluded | Not good enough |
| `qwen3:30b` | on | **current** | **kept, 12 of 12** | **excluded, 4 of 4** (and 4 of 4 for a children's study) | |

An earlier wording ("exclude when the paper fails a criterion") looked right at 12 of 12 on three
tries each, then excluded two relevant papers in the real pipeline. Repeated tries showed why: it
excluded the napping paper on 2 of 4 tries, so three tries weren't enough to tell.

The current prompt excludes a paper only when it's clearly about a different subject or its title
or abstract explicitly rules out a criterion. A related topic, a different measure or a criterion
the abstract doesn't mention gets "unsure". Over 4 tries each, it was right 20 of 20 times:

| Paper | Should be | Result |
|---|---|---|
| Sleep and working memory in older adults | kept | include, 4 of 4 |
| Napping and working memory in students | kept | include, 4 of 4 |
| Sleep and memory complaints in nurses | kept | unsure, 4 of 4 |
| Glacier mass balance (off topic) | excluded | exclude, 4 of 4 |
| Sleep and memory in school children (fails "adults") | excluded | exclude, 4 of 4 |

**The whole pipeline with this prompt:** `evals/litreview_table` passed 12 of 12 in 19 minutes:
all three relevant papers kept, the glacier paper excluded, notes correct with page references,
a synthesis citing all three with no invalid citations, and a `.bib` with three entries. That
run used a scratch server in a single process, not isolated. An earlier run of the pipeline
through the isolated server (with the older prompt) showed that path working.

**Cost:** about 2 minutes per paper on average (25 to 37 s off topic, up to 5 minutes for a
borderline paper), so about 3.5 hours per 100 papers, against about 7 s per paper before.

These are a few short synthetic papers with one question: it shows the fast model can't do this
job here, not how accurate the main model is on real abstracts.

**Also found:** with `think: false`, `qwen3:30b` and `qwen3:4b` still reason, in the visible
reply, ending in a stray `</think>`. Research calls now strip it. Gateway clients still receive
it unchanged, which could matter at efforts with reasoning "off" (`medium`, which the coding
eval uses). Not investigated further.
