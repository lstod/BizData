---
name: house-format
description: >
  This skill should be used when producing the engagement book workbook or the partner review
  deck, and defines the required tab structure, slide order, live formula conventions, number
  formatting and voice. It covers which cells must be formulas rather than pasted values, the
  conditional formatting thresholds on burn, and the eight slides in the order they appear
  every month. Use it for any delivery review artifact, alongside assemble-delivery-pack, so
  that output is identical month to month and survives a reviewer clicking a cell.
---

# The house format

Two artifacts, the same shape every month:

```
engagement-book-YYYY-MM.xlsx      five tabs
delivery-review-YYYY-MM.pptx      eight slides
```

Build the workbook first, then the deck **from the same `pack.json`**. The deck never contains
a number that is not in the workbook.

The whole point of this skill is that the format does not vary. If a run cannot produce these
artifacts to this format, it says so and stops. It does not produce something close.

## Use the bundled scripts

Both artifacts are written by scripts that ship with the plugin. Run them; do not write your
own.

```bash
python scripts/build_workbook.py pack.json --out engagement-book-2026-08.xlsx
python scripts/build_deck.py     pack.json --out delivery-review-2026-08.pptx
```

`build_workbook.py` is bundled with `assemble-delivery-pack`; `build_deck.py` with this skill.
Neither computes anything. Every value they write is either a field copied from a tool
response or an Excel formula, and that is what keeps "no figure in front of a partner was
calculated by a model" true all the way to the file on disk.

**If a script is missing, stop and report it.** Do not write a replacement. A hand-written
builder produces whatever layout was convenient that day, which is the one thing this skill
exists to prevent — and it has already happened once, in step 6, where the substitute silently
dropped a column and the run reported success. The rest of this document is the specification
those scripts implement, written down so the format is reviewable and so a discrepancy is
findable. It is not a set of instructions for rebuilding them.

## The workbook — five tabs, in this order

| Tab | Contents |
|---|---|
| `Summary` | Portfolio totals. Every total a formula over `Engagements`, never a pasted value, plus the figures the portfolio block measured |
| `Engagements` | One row per active engagement, carrying its `engagement_id`, through to margin and projected overrun |
| `Time Detail` | `get_time_summary` grouped by `engagement,week`, with each week's reporting coverage on the row |
| `Exceptions` | Only engagements flagged by `scope-escalation`, with reason, recommended action and decision owner |
| `Data Quality` | Late entries, nulls, suspected duplicates, weekly coverage, and any week below the floor |

## Live formulas, not computed constants

A reviewer will click a cell. Three columns on `Engagements` are Excel formulas:

| Column | Formula |
|---|---|
| `burn_pct` | `=Hours_To_Date / Ceiling_Hours` |
| `projected_overrun_pct` | `=MAX(0, Projected_Total_Hours - Ceiling_Hours) / Ceiling_Hours` |
| `margin_pct` | fixed fee: `=(Ceiling_Amount - Cost) / Ceiling_Amount` · time and materials: `=(Billable_Value - Cost) / Billable_Value` |

Each is guarded so that a row with no detail stays blank. An unexamined engagement's blank
cells are deliberate — a blank is honest about not having been looked at, and `0.0%` is not.

**Margin branches on fee type, and this is not optional.** A single
`=(Billable_Value - Cost) / Billable_Value` disagrees with the margin behind the health score
on every fixed-price engagement, because `engagement_burn_v1` takes the fee as revenue there.
It would also dissolve mess case 4, whose entire content is a fixed-fee engagement at negative
margin sitting beside a healthy burn.

The portfolio totals on `Summary` are `SUM`, `COUNT` and `COUNTIF` over the `Engagements` tab.
`COUNT` rather than `COUNTA` throughout: the `Exceptions` tab holds a sentence when nothing was
flagged, and `COUNTA` would count that sentence as an exception.

**Blended margin is the one portfolio figure that cannot be a formula.** It is a ratio of sums
whose numerator switches on fee type, so the quantity it sums is not a column on the tab. It
comes from the portfolio block on `list_engagements`, computed in SQL, and is copied onto
`Summary` under *Portfolio, as the tools measured it*.

## Two percent scales, and why

The three formula columns hold a ratio between 0 and 1, formatted `0.0%` so Excel scales them
for display. Every other `_pct` column holds a value the tools already scaled to 0..100, and is
formatted `0.0"%"` — a literal sign appended, no multiplication. Both read as `58.3%`. Nothing
is ever rescaled in Python.

This is why the `Summary` thresholds are written on two scales: `COUNTIF(burn, ">0.7")` against
the formula column, `COUNTIF(concentration, ">70")` against a tool value.

## Number formats

| Kind | Format |
|---|---|
| Percentages | one decimal |
| Currency | no decimals, thousands separated |
| Hours | one decimal, thousands separated |
| Dates | ISO, `yyyy-mm-dd` |
| Scores and day counts | one decimal |

## Conditional formatting

On the burn column only: **amber above 0.70, red above 0.90**, on the 0..1 scale the formula
produces. Red is tested first, because Excel stops at the first matching rule.

Burn is the only column that gets colour. An amber cell means a threshold was crossed, not that
somebody thinks the engagement is in trouble.

## The deck — eight slides, in this order

| # | Slide | Contents |
|---|---|---|
| 1 | Title | Period, portfolio size, date generated, `scoring_model_version` |
| 2 | Portfolio summary | Active engagements, total hours, blended margin, movement vs prior month |
| 3 | Margin by client | A native chart, not an image of one |
| 4 | Engagements at risk | One line each, maximum eight |
| 5–7 | One slide per RED engagement | Maximum three. Situation, the numbers, recommended action, decision owner |
| 8 | Data quality and caveats | Never omitted, even when clean |

On the count: eight is the format's full extent, not a fixed length. The spec says both "eight
slides, same order every month" and "maximum three" RED slides, and those cannot both hold in a
month with one RED engagement. Padding to a fixed eight would mean shipping slides that say
nothing, which teaches a reader to skim. **What is fixed is the order, and that data quality is
always last and always present.** A month with no RED engagements gets one slide saying so, and
the deck runs to six.

`scoring_model_version` goes on the title slide. A health band is only comparable against the
model that produced it.

## Rules the format does not bend

- **Every number on a slide is in the workbook.** Build the workbook first.
- **`projection_confidence: low` travels with its `confidence_reason`**, on every slide it
  appears on as well as in the book.
- **Never re-baseline a ceiling.** Burn above 100% is reported above 100%. The denominator
  never moves.
- **Slide 8 is never dropped.** "No issues this period" is a finding, and a deck that omits the
  caveats slide in a clean month has taught its reader that the slide is decorative.
- **Every row carries its `engagement_id`**, and every slide claim traces to a row.

## Voice

Declarative. No hedging, and no adjectives on numbers.

> Burn is 84%.

not

> Burn is concerningly high at 84%.

The reader decides what is concerning. State the figure, state what it is measured against, and
stop.

One word class is banned outright rather than discouraged. A week below the coverage floor is a
**filing artifact**, and it is never described as a slowdown, a dip, a drop, a decline, a slump,
a stall or reduced delivery — not in a cell, not in a slide, not in the accompanying message.
The firm did not stop delivering. The timesheets are not in.

## Before saying the pack is done

Work through `assets/self-check.md` against the files on disk. Eighteen questions, all of them
things that have gone wrong in a real run, and answering them takes about a minute.
