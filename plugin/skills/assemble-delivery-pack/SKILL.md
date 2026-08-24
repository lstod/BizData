---
name: assemble-delivery-pack
description: >
  This skill should be used when assembling the monthly delivery and margin review pack for a
  professional services portfolio: the engagement book workbook and the figures behind it. It
  covers which engagements to examine, the order the data is gathered in, the reporting-coverage
  check that runs before any analysis, and how engagement-book-YYYY-MM.xlsx is produced. Use it
  whenever a monthly delivery review, engagement book, delivery pack or partner pack is asked for.
---

# Assembling the monthly delivery pack

The pack is one workbook, `engagement-book-YYYY-MM.xlsx`, built from the BizData tools and from
nothing else. Every figure in it came back from a tool call. None of it was calculated here.

The period is the calendar month being reviewed. `period_start` is its first day, `period_end` its
last, and `as_of_date` is `period_end` unless the request says otherwise.

Two things in here are easy to skip and are the reason the pack is trustworthy: the coverage check
happens **before** any analysis, and the engagements to examine are chosen on **four** triggers
rather than the obvious two.

## Order of operations

**1. List the portfolio.**

```
list_engagements(status="active", as_of_date=<period_end>, limit=100, include_portfolio=true)
```

Page with `next_cursor` until the returned rows sum to `total_count`. Never work from a partial
list. The omitted engagement is the one that was in trouble, and a short list produces a pack that
looks clean rather than one that looks short.

`include_portfolio` on the **first call only**. It returns the whole active book's totals —
blended margin, total hours, movement against the prior month — and margin per client, all
computed in SQL. The deck's summary and margin-by-client slides read those figures and there is
no other source for them: the block is identical on every page, so asking again while paging
buys nothing.

**2. Summarise the time, portfolio-wide, before analysing anything.**

```
get_time_summary(period_start=<period_start>, period_end=<period_end>, group_by="engagement,week")
```

One call, with **no `engagement_ids` argument**. This is not an optimisation, it is the coverage
check: `data_completeness` is measured across the whole firm, and scoping the call to a subset of
engagements asks a different question. Three of eighteen engagements filing looks like full
coverage to a caller who asked about exactly those three.

Do this before reading any burn figure. Whether the period can be read at all is prior to what it
says.

**3. Apply the coverage rule.** See the next section. It changes how everything below is reported.

**4. Choose what to examine. There are six triggers, and you must write out all six lists before
calling anything.**

Go through the `list_engagements` rows once and collect these six sets of `engagement_id`. Write
each one out, by name, even when it is empty:

```
A  burn_pct > 70
B  health_band is not "green"
C  person_concentration_pct > 70
D  end_date on or before period_end
E  days_since_last_entry >= 14
F  margin_pct < 15
```

Then examine the **union** of A, B, C, D, E and F: `get_engagement_burn` and `get_financials`,
once each, for every id in it.

**Do not shortcut this to A and B.** That is the natural filter, it is the one most delivery
reviews use, and it is wrong. An engagement can be comfortably inside its ceiling and in the green
band while one person is 85% of its delivery, while its contract has already ended, while nobody
has logged time against it for a month, or while it loses money on every hour. Burn expresses none
of those four.

C through F all exist for the same reason and each was added after a run missed something real. C
and D came from step 6, where a burn-threshold fan-out never examined the concentrated engagement
or the one that had already ended. E and F came from step 8, where the escalation policy could not
flag a silent engagement or a fixed-fee engagement under water, because the detail call that would
have proved it was the call this filter had declined to make. On six of the seventeen fixture seeds
those two engagements sat in the green band, under 70% burn, with a live contract, and were
invisible.

Every one of these six is a column on the `list_engagements` row precisely so that you never need
a tool call to evaluate it. **The filter can only fire on what the triage row carries**, which is
why the row carries them.

**5. Everything else gets its summary figures and nothing more.** Detail you will not use is still
detail you paid for. Do not call `get_engagement_burn` for all eighteen engagements to be thorough.

**6. Apply `scope-escalation`** to the full set before writing anything. It decides what is RED,
what is NEEDS REVIEW, and what goes in the `Exceptions` tab. This skill decides what to look at;
that one decides what to say about it.

Its bundled `classify.py` evaluates the triggers and writes the `exceptions` array into
`pack.json`. Run it, then fill in `cause` and `recommended_action` on each row as that skill
directs. Do not set a flag yourself, and do not add or remove a row.

```bash
python scripts/classify.py pack.json --out pack.json
```

**7. Build the workbook, then the deck**, both with their bundled scripts and in that order.
See *Producing the pack*. The format of both is `house-format`'s: the five tabs, the live
formulas, the eight slides and the voice all live there, and this skill does not restate them.

## The coverage rule

This is the rule the pack exists to get right.

Read `data_completeness.weeks` from the `get_time_summary` response. Any week with
`firm_wide_gap: true` — below 60% of active engagements reporting — is a **filing artifact**. The
firm did not stop delivering that week. The timesheets are not in.

- **It is already excluded from every run rate and projection.** `weekly_run_rate_4wk`,
  `projected_total_hours` and `projected_overrun_pct` are computed in SQL over readable weeks only.
  You do not need to exclude it and you must not try.
- **Never re-derive a run rate by averaging the weekly rows yourself.** The `engagement,week` rows
  in the response include the gap week, because they are a record of what was filed. Averaging them
  puts the excluded week straight back into a number that had correctly dropped it. Use the run
  rate the tool returned.
- **Report it as a data note** on the `Data Quality` tab: the week, its `pct_active_reporting`, and
  the fact that figures for it are incomplete.
- **Never describe that week as a delivery slowdown, a dip, a drop, a decline, a slump, a stall or
  reduced delivery.** It is a reporting gap. Saying otherwise puts a wrong number in front of a
  partner, and the number is wrong in the direction that starts a conversation with a client about
  a problem that does not exist.

If a week is below 60% coverage, say what is true: reporting coverage was 16.7% in the week of
2026-08-10, and delivery for that week cannot be measured.

## Rules that override anything else

- **Every figure comes from a tool response.** If a number is needed that no tool returns, say so
  on the `Data Quality` tab and leave the cell empty. Do not derive it.
- **Never compute a percentage.** Every ratio the pack reports — burn, margin, realisation,
  overrun, concentration, coverage — is already computed in SQL and returned on the scale it should
  be printed on. A percentage worked out here is a percentage that can disagree with the database.
- **`projection_confidence: low` travels with its `confidence_reason`.** Wherever the projection
  appears, the reason appears beside it. An unlabelled low-confidence projection is a defect, not a
  simplification.
- **Never re-baseline a ceiling.** If `hours_to_date` exceeds `ceiling_hours` the engagement is over
  budget. `hours_remaining` comes back negative and stays negative.
- **Never infer that an engagement is complete from absent time entries.** Absent data is absent
  data.
- **Every row carries its `engagement_id`**, and every claim traces back to a row.
- **Pass the same `run_id` to every call** in the run, so the tool-call log reconstructs it as one
  piece of work.

## Stop conditions

Stop and report rather than producing a pack when:

- Any tool call fails, or returns a `total_count` inconsistent with a prior call in the same run.
- More than 20% of the period's entries arrived after the period closed —
  `data_quality.late_entry_pct > 20`. The period is not stable enough to review yet.
- The run ledger shows this period already produced a pack and the underlying figures have since
  changed. Report what changed and ask before regenerating.

A stopped run says which condition tripped and what was seen. It does not produce a partial
workbook.

## Producing the pack

Collect the raw tool responses into one JSON file, unmodified, then run the three bundled scripts
in this order:

```bash
python scripts/classify.py       pack.json --out pack.json
python scripts/build_workbook.py pack.json --out engagement-book-2026-08.xlsx
python scripts/build_deck.py     pack.json --out delivery-review-2026-08.pptx
```

`classify.py` first, and the two judgment fields filled in before the workbook is built. The
builders copy the `Exceptions` rows as they find them, so anything still empty at that point is
empty in the file a partner opens.

The workbook first, always. The deck may not contain a number that is not in the book, and
building it second is what makes that checkable rather than merely intended.

The scripts do the layout. Neither computes anything, which is what keeps the "every figure came
from a tool" rule true through to the files on disk.

If any of the three scripts is missing from the environment, **stop and say so**. Do not write a
replacement: a hand-rolled builder produces a different layout every month, and it has already
silently dropped a column once.

**If a skill this one references is not available, stop and say so.** Do not supply its judgment
yourself. This is the same rule and it is stated separately because the first version named
scripts, and a missing skill walked straight through it: with `scope-escalation` absent, a run
wrote ten exception rows on criteria it invented rather than halting. It declared them, which was
the good version of the behaviour, and they were still the only content in the pack that traced to
nothing.

`pack.json` has this shape. Keys are exactly the field names the tools returned:

```json
{
  "period": "2026-08",
  "run_id": "delivery-review-2026-08",
  "engagements": [ "<every list_engagements row, all pages, in order>" ],
  "portfolio": "<the portfolio block from the first list_engagements call, whole>",
  "time_summary": "<the whole get_time_summary response, including both blocks>",
  "burn": { "12": "<the get_engagement_burn response for engagement 12>" },
  "financials": { "12": "<the get_financials response for engagement 12>" },
  "exceptions": [
    {
      "engagement_id": 9,
      "flag": "RED",
      "triggers": "which rules fired, written by classify.py",
      "situation": "one sentence, figures only, written by classify.py",
      "cause": "or: not determinable from available data",
      "recommended_action": "the decision needed, and from whom",
      "decision_owner": "engagement lead"
    }
  ]
}
```

`burn` and `financials` hold only the engagements chosen at step 4. `portfolio` is copied whole
from the first `list_engagements` response and is required — the deck refuses to build without
it rather than adding the rows up itself. `exceptions` comes from `scope-escalation`'s
`classify.py`, with `cause` and `recommended_action` filled in afterwards; an empty list is valid
and produces a tab saying no engagement was flagged, which is itself a finding.

The scripts write five tabs in order — `Summary`, `Engagements`, `Time Detail`, `Exceptions`,
`Data Quality` — and the deck's slides in `house-format`'s order, reporting the gap week on the
last tab and the last slide.

## Publishing

The two files are on disk in a working directory. Publishing puts them somewhere they survive.
Both destinations, in this order, every run.

**1. The archive of record.** Call `publish_pack` with this run's `run_id`, the period, and the
two filenames:

```
publish_pack(run_id="<this run's id>", period="2026-08",
             artifacts=["engagement-book-2026-08.xlsx", "delivery-review-2026-08.pptx"])
```

It returns one presigned upload URL per file, each scoped to a single object key under
`runs/<run_id>/` and valid for fifteen minutes. Upload each file to its own URL with an HTTP
`PUT` of the raw bytes. A URL writes the one key it was minted for and nothing else, so do not
reuse one URL for both files.

**2. Close the run out.** Call `publish_pack` again with the same three arguments and
`finalize=true`. It checks that both artifacts arrived, then writes this run's full tool-call log
and its ledger entry beside them. If it says a file did not arrive, the upload failed — retry that
upload rather than the call. If a URL has expired, call `publish_pack` without `finalize` to mint
fresh ones.

**Do not upload `run-log.json` or `ledger.json`.** They are the server's record of this run and it
writes them itself; naming either as an artifact is refused. Publish the workbook and the deck,
nothing else.

**Never reuse a run id from an earlier run.** The archived tool-call log is gathered by filtering
the last twenty-four hours of the server's log on the run id, so a reused id archives both runs'
calls in one file and the ledger's count follows it. Republishing *this* run under its own id is
fine and is what to do if an upload failed.

**3. Save both files to the Drive folder**, with the run id in the filename:

```
engagement-book-2026-08-<run_id>.xlsx
delivery-review-2026-08-<run_id>.pptx
```

The archive is keyed by run id in its path, and Drive is flat, so the id moves into the filename
to keep the same two files findable from either side. This is the one place the house format's
filenames are extended, and `house-format` says so.

**Drive is output only.** Never read a figure back from a file in Drive — not this run's, not last
month's. Every number in the pack comes from a tool call against the data layer, and a folder that
becomes a second source of figures is how two versions of last month's margin start circulating.

If `publish_pack` is not available, **stop and say so** and report where the files are on disk. Do
not upload them anywhere else, and do not treat a local path as a published pack.

Then tell the user all three destinations: the working directory, the archive prefix, and the
Drive folder.
