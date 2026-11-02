"""Presidio worker: finds personal data (names, places, groups...) in text for Cairnward's
redaction scanner. Runs with the Presidio engine's own Python, as a child of Cairnward's
core (inside its sandbox when the core is isolated), with no network: the spaCy model is
installed locally. Standalone: no Cairnward imports.

stdin:  one JSON object per line: {"id": n, "text": "...", "entities": [...], "min_score": x}
stdout: one JSON object per line: {"id": n, "findings": [{"type", "start", "end", "score"}]}
        (or {"id": n, "error": "..."}); first line "READY" once the model is loaded.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "DO_NOT_TRACK": "1"})
    from presidio_analyzer import AnalyzerEngine

    engine = AnalyzerEngine()
    print("READY", flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            found = engine.analyze(text=req["text"], language="en",
                                   entities=req.get("entities") or None)  # fmt: skip
            out = [{"type": r.entity_type, "start": r.start, "end": r.end, "score": r.score}
                   for r in found if r.score >= float(req.get("min_score", 0))]  # fmt: skip
            print(json.dumps({"id": req.get("id"), "findings": out}), flush=True)
        except Exception as exc:  # one bad request shouldn't stop the worker
            print(
                json.dumps({"id": None, "error": f"{type(exc).__name__}: {exc}"[:300]}), flush=True
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
