---
name: scope-escalation
description: >
  This skill should be used when an engagement is trending past its SOW ceiling, running at
  unexpected margin, missing time data, or carried by one person. It defines what is flagged
  RED, what is flagged NEEDS REVIEW, the four-line note each flag produces, and the actions
  the agent must never take without a human decision. Use it on every engagement in a
  delivery review, after the figures are gathered and before any artifact is written.
---

# Scope and escalation

This skill decides what to say about an engagement. `assemble-delivery-pack` decides what to look
at; `house-format` decides what the output looks like. This one is the judgment in the middle, and
it is deliberately the smallest of the three: most of what a delivery review could say about an
engagement, it should not say.

The flags are not opinions. They come from `scripts/classify.py`, which reads the same `pack.json`
the builders read and evaluates the trigger list below against tool fields. What is left for you is
the part a threshold cannot produce: the cause, when the data supports one, and the conversation the
flag should start.

## Use the bundled script

```bash
python scripts/classify.py pack.json --out pack.json
```

It reads `engagements`, `burn` and `financials`, and writes the `exceptions` array back into the
same file. Every row it writes carries `engagement_id`, `flag`, `triggers`, `situation` and
`decision_owner`, and leaves `cause` empty for you — along with `recommended_action`, except on the
triggers where the answer is to recommend nothing and the script writes that itself.

**If the script is missing, stop and report it.** Do not write a replacement and do not classify by
eye. A hand-applied threshold is a threshold that moved, and the whole claim of this pack is that a
partner can re-derive every flag from the figures beside it. This has already happened once: with
this skill absent entirely, a run wrote ten exception rows on criteria it invented, declared them
honestly, and still produced the only content in the pack that traced to nothing.

**If a skill this one references is not available, stop and say so.** Do not supply its judgment
yourself. That is the same rule as the one above and it is written twice because the first version
was scoped to scripts, and a missing skill went straight through it.

## Flag as RED

- `projected_overrun_pct > 10` with `projection_confidence: high`
- Fixed-fee engagement with `margin_pct < 15`
- `burn_pct > 90` with more than 20% of the engagement duration remaining
- Any engagement with no time logged for 14 consecutive days while still `active` —
  `days_since_last_entry >= 14`

## Flag as NEEDS REVIEW — different from RED, and the distinction matters

- `projected_overrun_pct > 10` with `projection_confidence: low`. The signal is real, the
  projection is not trustworthy. Report both, recommend nothing.
- **Contradictory signals**: `margin_pct < 15` on an engagement whose burn is at or under 70%. The
  margin is failing while the hours are not. State the contradiction plainly. Do not resolve it —
  a resolution requires context the data does not contain.
- **`person_concentration_pct > 70`** on an engagement above the median fee. Flag the continuity
  risk and recommend nothing. Whether one person carrying an engagement is a problem depends on
  whether they are about to go on leave, whether the client asked for them by name, and whether
  anyone else has been offered the work — none of which is in the database.

## A contradiction outranks a RED trigger

An engagement matching both a RED trigger and a contradiction trigger is **NEEDS REVIEW**, not RED.

This is the one precedence rule and it is the substance of the skill. A single-signal RED says the
problem is legible: the hours ran out, the filing stopped, the projection is going to breach. A
contradiction says two true numbers point in opposite directions, and escalating that as RED asserts
which of them is the real one. Nobody can assert that from the data.

The case this exists for is a fixed-fee engagement at negative margin whose burn is comfortably
inside its ceiling. It fires the fixed-fee RED rule on margin alone. It is not RED. The hours are
fine and the price was wrong, or the hours are fine and the cost base moved, and the difference
between those two is a conversation rather than a query.

## For each flagged engagement, produce

A four-line note for the `Exceptions` tab. `classify.py` writes the first line and the last; you
write the middle two.

- **Situation** — one sentence, figures only. Written by the script, from the tool values. Do not
  edit it.
- **Cause** — only if the data supports one. Otherwise: "not determinable from available data". It
  usually does not support one. A tool returns what happened, not why, and a plausible cause written
  into a partner deck is indistinguishable from a known one.
- **Recommended action** — the scope conversation, the specific decision needed, and from whom.
  Where the trigger list says to recommend nothing, the script has already written *"None. This
  trigger is a reason to look, not a decision to take."* and you must leave it exactly as it
  stands. A NEEDS REVIEW raised on concentration alone, or on a low-confidence projection, is a
  thing to look at rather than a thing to do, and filling that line in is the single most likely
  way this skill gets quietly undone.
- **Decision owner** — the engagement lead. Never the agent.

## Never, under any circumstances

- **Never re-baseline a ceiling.** If actuals exceed `ceiling_hours`, the engagement is over budget.
  It is not a case of the budget having been wrong. Adjusting the denominator to make burn look
  reasonable is the single most damaging thing this workflow could do, and it is the kind of thing
  that gets done helpfully. Burn above 100% is reported above 100%. Never describe a ceiling as
  re-baselined, revised, adjusted, reset or updated.
- **Never infer that an engagement is complete from absent time entries.** Absent data is absent
  data. Flag it and let someone who knows say. An engagement is not finished, wrapped up, closed out
  or delivered because nobody filed against it.
- **Never present a projection built on fewer than three weeks of entries as a projection.** Report
  the actuals and say the run rate is not established. `projection_confidence` already carries this;
  the rule is that you repeat it rather than round it off.
- **Never soften a red flag because the client relationship is sensitive.** That judgment belongs to
  the partner, and it can only be made if the flag reached them.
- **Never change a flag the script assigned, and never add or remove a row.** If a flag looks wrong,
  the trigger list is wrong and that is worth saying in the run summary. Editing the output makes the
  flags unre-derivable, which costs more than one wrong flag.

## What is not flagged

An engagement examined and not flagged is a result. It gets its row on `Engagements` and no row on
`Exceptions`, and an empty `Exceptions` tab is a finding rather than a gap — `build_workbook.py`
writes a sentence saying so.

Resist raising a flag because an engagement was examined. `assemble-delivery-pack` examines on four
triggers deliberately wider than this skill's, so that nothing is invisible at triage. Most of what
it surfaces is fine.
