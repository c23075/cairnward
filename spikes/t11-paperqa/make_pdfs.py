#!/usr/bin/env python3
"""Generate small synthetic 'papers' (about 4 pages each) for the spike."""

import random
import sys
from pathlib import Path

from fpdf import FPDF

TOPICS = [
    ("Sleep duration and working memory in adolescents", "sleep", "working memory"),
    ("Screen time and attention in primary-school children", "screen time", "attention"),
    ("Physical activity and exam performance in university students", "exercise", "exam scores"),
    ("Caffeine intake and reaction time in shift workers", "caffeine", "reaction time"),
    ("Mindfulness training and stress in nurses", "mindfulness", "stress"),
]
WORDS = [
    "participants",
    "measured",
    "significant",
    "cohort",
    "baseline",
    "follow-up",
    "variance",
    "regression",
    "effect",
    "sample",
    "controlled",
    "randomised",
    "outcome",
    "association",
    "confidence",
    "interval",
    "methodology",
    "limitation",
    "dataset",
    "analysis",
    "reported",
    "compared",
    "intervention",
]

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
rng = random.Random(1)
for i, (title, x, y) in enumerate(TOPICS):
    pdf = FPDF()
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.multi_cell(0, 8, new_x="LMARGIN", new_y="NEXT", text=title)
    pdf.set_font("Helvetica", size=10)
    for section in ("Abstract", "Introduction", "Methods", "Results", "Discussion", "Limitations"):
        pdf.set_font("Helvetica", "B", 11)
        pdf.multi_cell(0, 7, new_x="LMARGIN", new_y="NEXT", text=section)
        pdf.set_font("Helvetica", size=10)
        for _ in range(3):
            filler = " ".join(rng.choice(WORDS) for _ in range(90))
            pdf.multi_cell(
                0,
                5,
                new_x="LMARGIN",
                new_y="NEXT",
                text=f"We studied how {x} relates to {y}. {filler}. "
                f"Higher {x} was associated with a change in {y} "
                f"(effect size {rng.uniform(0.1, 0.6):.2f}).",
            )
    pdf.output(str(out / f"paper{i + 1}.pdf"))
print(f"wrote {len(TOPICS)} PDFs to {out}")
