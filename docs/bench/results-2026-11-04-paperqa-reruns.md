# PaperQA2 reruns after the full check, 2026-11-04

The full check (report on the `dev` branch) ran on `main` (a57444e) through the running isolated
server, on CPU, with the synthetic papers. Lean passed 8 of 8 and PaperQA2 passed 6 of 8, failing
q5 and u2. Those two questions were then run three more times on PaperQA2 with the answers kept.

| Run | q5 | q5's answer | u2 | u2's answer |
|---|---|---|---|---|
| Check | fail | not kept | fail | Refused, with a quotation that isn't in the papers |
| Rerun 1 | fail | "Six" | fail | Declined in other words: true, and no refusal sentence |
| Rerun 2 | fail | "Six" | fail | Declined in other words, and says the excerpts "explicitly state" it wasn't reported |
| Rerun 3 | fail | "Six" | pass | "I cannot answer", and says the paper "explicitly states" it wasn't named |

## What was found
- **q5 fails because of the check.** The paper says "6 of the 42 gardeners". All three kept answers
  said "Six", cited the right page and are correct, but the check looks for the digit "6".
- **u2's refusals are worded too freely for the check.** PaperQA2's answer counts as a refusal only
  if it contains "cannot answer" or "can't answer" (`research/engines/paperqa.py`). Reruns 1 and 2
  declined correctly and were marked as not honest. A caller reading `cannot_answer` from such an
  answer gets the wrong value.
- **u2 misdescribes the paper, and this one is real.** The paper never mentions which species was
  most common. In 3 of the 4 u2 answers the model said the source explicitly states that the species
  wasn't given. It's a false claim about a source, and only the check's run was caught, because it
  used quotation marks. The checker can't see the other two. Where the claim comes from isn't known;
  PaperQA2's working summaries are a guess.

## Not changed
No code or eval changed from this. Three reruns on one synthetic paper show these behaviours exist,
not how often they'd happen on real papers.
