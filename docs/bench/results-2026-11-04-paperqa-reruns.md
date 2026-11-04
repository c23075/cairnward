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

## Where the false claims come from
u2 was run six more times with PaperQA2's working summaries kept, alternating with and without them.

| Setting | Runs | Says a source states it | Clear refusal | Time per question |
|---|---|---|---|---|
| Summaries on (default) | 3 | 2 | 1 | 316 to 448 s |
| Summaries off (`skip_summary`) | 3 | 0 | 3 | 838 to 1,258 s |

The fast model ends a summary with "it does not explicitly state ...". The main model reads the
summaries instead of the paper, so it reports that sentence as what the paper says, and sometimes
quotes it. Turning summaries off also stopped the false claim, but took about 2.6 times as long and
cited every passage, including the glacier paper on a question about bees.

## The fix
- **Prompts** (`paperqa_worker.py`): summaries must not mention what an excerpt leaves out. Answers
  must not quote the context or say a source states something is missing, and a refusal must begin
  with "I cannot answer."
- **Refusals:** other wordings seen in real answers now count, such as an answer opening "The context
  does not ...". This can also mark a partial answer as a refusal.
- **The check:** q5 accepts "6" or "six". The exit-criteria line now says invented citations count
  against every engine.

## After the fix
Run for real, with the new worker, the running isolated server's gateway and the same synthetic
papers. The worker ran through the CLI, not through the server, which still has the old code.

| Question | Runs | Passed | Note |
|---|---|---|---|
| u2 | 5 | 5 | All begin "I cannot answer", with no claim about what a source states and nothing quoted |
| q1 to q6, u1 | 1 each | 7 | |

## Still wrong, and not caught
These answers passed, because the check only looks for the expected fact and valid quotations. All
of them come from one run each.

- **q3:** two summaries were false, saying drone surveys began in 2020 and in 1990 (the paper says
  2012). The answer gave 2012, then cited those two pages as "assertions" it contradicted. Those are
  false claims about sources, which is the same kind of problem as before.
- **q6:** a summary guessed the study recommends less pesticide use, and the answer repeated it as a
  strategy the study identified. The paper recommends only native plants.
- **q1:** one summary still said the text "does not explicitly state" something, so the summary rule
  is not always followed. This one didn't reach the answer.
- **q2 and q5:** the answers added explanation the paper doesn't give.

So the fix removes the failure on u2, but the cause underneath remains: the fast model's summaries
can be wrong, and the main model trusts them. Five runs on one synthetic paper can't show how often
this happens on real papers.
