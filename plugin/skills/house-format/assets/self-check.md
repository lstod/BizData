# Before you say the pack is done

Run these against the files you actually wrote, not against what you meant to write. Every one
of them has failed at least once in a real run.

Answer them in order. A "no" is a stop, not a caveat to mention at the end.

## The scripts

1. Did `classify.py`, `build_workbook.py` and `build_deck.py` all run, from `scripts/`, and exit
   cleanly?
2. If any was missing, did you **stop and report** rather than writing a substitute?
3. Were all three skills available — `assemble-delivery-pack`, `house-format`,
   `scope-escalation`? If one was absent, did you stop rather than supplying its judgment?

If a script was absent and you wrote your own, the pack is not in the house format and saying
so afterwards does not fix it. This is the failure that has actually happened: the run reported
success, and the substitute workbook had a column heading over empty cells.

Question 3 is the same failure in the place the first fix did not reach. With `scope-escalation`
absent, a run wrote ten exception rows on criteria it invented. Every script guard passed.

## The workbook

4. Five tabs, in order: `Summary`, `Engagements`, `Time Detail`, `Exceptions`, `Data Quality`.
5. Click a burn cell. Does the formula bar show a formula, not a number?
6. Do `Summary`'s totals reference `Engagements!`, rather than holding pasted values?
7. Does every engagement in `total_count` have a row? Count them.
8. Do unexamined engagements have **blank** detail cells rather than zeros?
9. Is `Exceptions` a sentence saying nothing was flagged, rather than an empty tab, when
   nothing was flagged?

## The exceptions

10. Is every `flag` and every `situation` exactly what `classify.py` wrote? You fill in `cause`
    and `recommended_action`, and nothing else.
11. Are there exactly as many rows as the script produced — none added, none removed?
12. Does every `cause` you could not source read "not determinable from available data", rather
    than a plausible story?
13. Is `decision_owner` the engagement lead on every row, and never the agent?

## The deck

14. Is the last slide *Data quality and caveats*? It is never dropped, including in a clean
    month.
15. Is margin by client a chart object you can click into, rather than an image?
16. Pick three numbers off three different slides. Find each one in the workbook. If a number
    is not in the book, it should not be in the deck.
17. Does every low-confidence projection carry its `confidence_reason` on the slide it appears
    on?

## The words

18. Search the workbook and the deck for: slowdown, dip, drop, decline, slump, stall, reduced
    delivery. A week below the coverage floor is a **filing artifact**, and none of those words
    may be attached to it.
19. Read one sentence of your own prose aloud. Does it put an adjective on a number? "Burn is
    84%", not "burn is concerningly high at 84%".
20. Does anything in the pack describe a ceiling as re-baselined, adjusted or revised? Burn
    above 100% stays above 100%.
21. Does anything describe a silent engagement as complete, finished or wrapped up? Absent data
    is absent data.

## The arithmetic you did not do

22. Is there any percentage in either file that you worked out yourself? There should be none.
    Every ratio came back from a tool or is an Excel formula in a cell.
23. Did you average the weekly rows to get a run rate? The gap week is in those rows on
    purpose. Use `weekly_run_rate_4wk` as returned.
24. Did you total the engagement rows to get blended margin? That figure comes from the
    `portfolio` block and nowhere else — the rows cannot produce it, because the numerator
    switches on fee type.
