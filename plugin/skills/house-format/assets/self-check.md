# Before you say the pack is done

Run these against the files you actually wrote, not against what you meant to write. Every one
of them has failed at least once in a real run.

Answer them in order. A "no" is a stop, not a caveat to mention at the end.

## The scripts

1. Did `build_workbook.py` and `build_deck.py` both run, from `scripts/`, and exit cleanly?
2. If either was missing, did you **stop and report** rather than writing a substitute?

If a script was absent and you wrote your own, the pack is not in the house format and saying
so afterwards does not fix it. This is the failure that has actually happened: the run reported
success, and the substitute workbook had a column heading over empty cells.

## The workbook

3. Five tabs, in order: `Summary`, `Engagements`, `Time Detail`, `Exceptions`, `Data Quality`.
4. Click a burn cell. Does the formula bar show a formula, not a number?
5. Do `Summary`'s totals reference `Engagements!`, rather than holding pasted values?
6. Does every engagement in `total_count` have a row? Count them.
7. Do unexamined engagements have **blank** detail cells rather than zeros?
8. Is `Exceptions` a sentence saying nothing was flagged, rather than an empty tab, when
   nothing was flagged?

## The deck

9. Is the last slide *Data quality and caveats*? It is never dropped, including in a clean
   month.
10. Is margin by client a chart object you can click into, rather than an image?
11. Pick three numbers off three different slides. Find each one in the workbook. If a number
    is not in the book, it should not be in the deck.
12. Does every low-confidence projection carry its `confidence_reason` on the slide it appears
    on?

## The words

13. Search the workbook and the deck for: slowdown, dip, drop, decline, slump, stall, reduced
    delivery. A week below the coverage floor is a **filing artifact**, and none of those words
    may be attached to it.
14. Read one sentence of your own prose aloud. Does it put an adjective on a number? "Burn is
    84%", not "burn is concerningly high at 84%".
15. Does anything in the pack describe a ceiling as re-baselined, adjusted or revised? Burn
    above 100% stays above 100%.

## The arithmetic you did not do

16. Is there any percentage in either file that you worked out yourself? There should be none.
    Every ratio came back from a tool or is an Excel formula in a cell.
17. Did you average the weekly rows to get a run rate? The gap week is in those rows on
    purpose. Use `weekly_run_rate_4wk` as returned.
18. Did you total the engagement rows to get blended margin? That figure comes from the
    `portfolio` block and nowhere else — the rows cannot produce it, because the numerator
    switches on fee type.
