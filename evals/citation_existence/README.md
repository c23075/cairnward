# Citation-existence evaluation

Does the check catch invented quotations and references (FR-RES-5)? It writes a draft mixing real quotations and references with invented ones, runs `cairnward library check`, and compares each verdict with the known answer. It needs the embedding model only, to index the two synthetic papers, so it takes seconds.

To keep it away from your data and running server, give it empty folders:

```bash
export CAIRNWARD_DATA_DIR=$(mktemp -d) CAIRNWARD_STATE_DIR=$(mktemp -d)
```

```bash
uv run python evals/citation_existence/run_eval.py
```

It prints one line per case and exits 0 when all pass. A wrong verdict that lets something invented through counts as a miss, and `missed` must be zero.

This tests the checker, not a model. Whether a model invents things is measured by `evals/pdf_qa`, which counts invented citations and quotations in real answers. DOIs are only checked for form here, so a reference with a DOI is reported as unverified, not as real.
