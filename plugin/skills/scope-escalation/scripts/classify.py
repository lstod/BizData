#!/usr/bin/env python3
"""Apply the scope-escalation trigger list to a pack, deterministically.

    python scripts/classify.py pack.json --out pack.json

Reads engagements, burn and financials out of pack.json, evaluates the RED and NEEDS REVIEW
triggers in plugin/skills/scope-escalation/SKILL.md against them, and writes the exceptions
array back. Same input the two builders read, so a flag and the figures behind it can never
come from different runs.

The point of this being a script rather than prose is that a flag is re-derivable. A partner
can take the Engagements tab, apply the trigger list, and get the same rows. That stops being
true the moment a model applies the thresholds by eye, and it stopped being true once already:
with this skill absent, a run invented its own criteria, produced ten defensible-looking rows,
and left the pack with content that traced to nothing.

WHAT THIS FILE DOES NOT DO. It does not write a cause and it does not write a recommended
action. Those are the two fields a threshold cannot produce, they are left empty here, and the
skill tells the agent how to fill them. Splitting it that way is the whole design: the flag is
arithmetic and the conversation is judgment, and mixing them produces a system where neither
can be checked.

Two things follow from that and are worth knowing before editing:

  * No arithmetic on reported figures. Every value in a situation sentence is a field copied
    from a tool response and formatted, never combined. The one number computed here is the
    median fee, which is a threshold rather than a reported figure: it decides whether a row
    exists and never appears in the pack. It matches percentile_cont(0.5) in
    db/checks/mess_cases.sql, and check_escalation.py asserts the two agree per seed.
  * A contradiction outranks a RED trigger. See classify_one. This is the one precedence rule
    and it is the reason mess case 4 is NEEDS REVIEW rather than RED.

Runs in Cowork's code execution sandbox, so the standard library and nothing else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from typing import Any

# The thresholds. Every one of them is from spec-a-delivery-margin.md except the
# concentration pair, which the build plan adds at step 8 for mess case 8.
OVERRUN_TRIGGER = 10.0
FIXED_FEE_MARGIN_FLOOR = 15.0
BURN_RED = 90.0
DURATION_REMAINING_TRIGGER = 0.20
SILENT_DAYS = 14
CONCENTRATION_TRIGGER = 70.0

# The line between "the margin is failing" and "the hours are failing too". Same 70 as
# assemble-delivery-pack's burn trigger, deliberately: one definition of a healthy burn in
# the codebase, so the contradiction rule and the examine rule cannot drift apart.
BURN_HEALTHY = 70.0

RED = "RED"
NEEDS_REVIEW = "NEEDS REVIEW"

# Triggers describing a continuity risk or an untrustworthy signal rather than a problem with
# a known shape. The skill says to recommend nothing on these. A row whose triggers are all in
# this set gets its recommended_action written here rather than left for the agent, because
# "recommend nothing" is the one instruction a helpful model reliably improves on.
NO_RECOMMENDATION = frozenset({"concentration above median fee", "overrun, projection not trustworthy"})
NO_RECOMMENDATION_TEXT = "None. This trigger is a reason to look, not a decision to take."

DECISION_OWNER = "engagement lead"


class PackError(Exception):
    """The input could not produce a defensible set of flags."""


# --------------------------------------------------------------------------- input


def load_pack(path: Path) -> dict[str, Any]:
    """Read pack.json and refuse anything that would produce flags with holes in them."""
    try:
        pack = json.loads(path.read_text())
    except FileNotFoundError:
        raise PackError(f"No pack file at {path}") from None
    except json.JSONDecodeError as exc:
        raise PackError(f"{path} is not valid JSON: {exc}") from None

    for key in ("engagements", "burn", "financials"):
        if key not in pack:
            raise PackError(
                f"{path} has no {key!r}. Required keys: engagements, burn, financials. "
                "Classification runs after the detail calls, not instead of them."
            )

    if not pack["engagements"]:
        raise PackError(
            "engagements is empty. A pack with no engagements is a failed run, not an empty "
            "month; stop and report rather than classifying nothing."
        )

    if not pack["burn"]:
        raise PackError(
            "burn is empty, so no engagement was examined. Every RED trigger except the "
            "fixed-fee margin one reads a get_engagement_burn field, so classifying now would "
            "return an empty exceptions list that means 'not looked at' rather than 'nothing "
            "found'. Run the examine step first."
        )

    return pack


# ----------------------------------------------------------------------- formatting


def number(value: Any, places: int = 1) -> str:
    """A figure as it is printed in a situation sentence, or an em dash if absent.

    Rounding for display only. The value written is the tool's, and nothing downstream reads
    these strings back as data.
    """
    if value is None:
        return "—"
    return f"{float(value):,.{places}f}"


def median_fee(engagements: list[dict[str, Any]]) -> float | None:
    """The median ceiling_amount across the active book.

    statistics.median interpolates between the two middle values on an even-sized list, which
    is what percentile_cont(0.5) does in db/checks/mess_cases.sql. numpy-style midpoint
    indexing would disagree on every even portfolio, and eighteen active engagements is even.
    """
    fees = [float(e["ceiling_amount"]) for e in engagements if e.get("ceiling_amount") is not None]
    return statistics.median(fees) if fees else None


def duration_days(engagement: dict[str, Any]) -> int | None:
    """Contract length in days, from the two dates on the triage row."""
    start, end = engagement.get("start_date"), engagement.get("end_date")
    if not start or not end:
        return None
    try:
        days = (dt.date.fromisoformat(str(end)) - dt.date.fromisoformat(str(start))).days
    except ValueError:
        return None
    return days or None


# ---------------------------------------------------------------------- the triggers


def classify_one(
    engagement: dict[str, Any],
    burn: dict[str, Any] | None,
    financials: dict[str, Any] | None,
    fee_median: float | None,
) -> dict[str, Any] | None:
    """One engagement against the trigger list. None when nothing fires.

    Order matters in exactly one place. The contradiction check runs before the flag is
    settled, and when it fires the row is NEEDS REVIEW no matter what else did. A single
    signal RED says the problem is legible; a contradiction says two true numbers disagree,
    and calling that RED asserts which of them is real.
    """
    burn = burn or {}
    financials = financials or {}

    burn_pct = engagement.get("burn_pct")
    margin_pct = financials.get("margin_pct")
    overrun = burn.get("projected_overrun_pct")
    confidence = burn.get("projection_confidence")
    silent_days = burn.get("days_since_last_entry")
    concentration = engagement.get("person_concentration_pct")
    fee_type = engagement.get("fee_type")

    red: list[str] = []
    review: list[str] = []
    situations: list[str] = []

    # --- RED ----------------------------------------------------------------------
    if overrun is not None and overrun > OVERRUN_TRIGGER and confidence == "high":
        red.append("projected overrun, high confidence")
        situations.append(
            f"Projected overrun is {number(overrun)}% against a ceiling of "
            f"{number(engagement.get('ceiling_hours'))} hours, at high projection confidence."
        )

    if fee_type == "fixed" and margin_pct is not None and margin_pct < FIXED_FEE_MARGIN_FLOOR:
        red.append("fixed fee below the margin floor")
        situations.append(
            f"Fixed-fee engagement at {number(margin_pct)}% margin, against a floor of "
            f"{number(FIXED_FEE_MARGIN_FLOOR, 0)}%."
        )

    duration = duration_days(engagement)
    remaining = engagement.get("days_remaining")
    if (
        burn_pct is not None
        and burn_pct > BURN_RED
        and duration
        and remaining is not None
        and remaining / duration > DURATION_REMAINING_TRIGGER
    ):
        red.append("burn above 90% with duration remaining")
        situations.append(
            f"Burn is {number(burn_pct)}% of a {number(engagement.get('ceiling_hours'))} hour "
            f"ceiling with {remaining} of {duration} contract days remaining."
        )

    if engagement.get("status") == "active" and silent_days is not None and silent_days >= SILENT_DAYS:
        red.append("no time logged for 14 days")
        situations.append(
            f"No time has been logged for {silent_days} days and the engagement is still active. "
            "Absent data is not evidence of completion."
        )

    # --- NEEDS REVIEW -------------------------------------------------------------
    if overrun is not None and overrun > OVERRUN_TRIGGER and confidence == "low":
        review.append("overrun, projection not trustworthy")
        reason = burn.get("confidence_reason")
        situations.append(
            f"Projected overrun is {number(overrun)}% at low projection confidence"
            + (f": {reason}." if reason else ".")
        )

    contradiction = (
        margin_pct is not None
        and margin_pct < FIXED_FEE_MARGIN_FLOOR
        and burn_pct is not None
        and burn_pct <= BURN_HEALTHY
    )
    if contradiction:
        review.append("margin failing, burn healthy")
        situations.append(
            f"Margin is {number(margin_pct)}% while burn is {number(burn_pct)}% of the "
            f"{number(engagement.get('ceiling_hours'))} hour ceiling. The two figures disagree "
            "and the data does not say which is the operative one."
        )

    if (
        concentration is not None
        and concentration > CONCENTRATION_TRIGGER
        and fee_median is not None
        and float(engagement.get("ceiling_amount") or 0) > fee_median
    ):
        review.append("concentration above median fee")
        situations.append(
            f"One person is {number(concentration)}% of hours on an engagement of "
            f"{number(engagement.get('ceiling_amount'), 0)}, above the median fee. "
            f"{engagement.get('people_count')} people have logged time."
        )

    if not red and not review:
        return None

    # The one precedence rule. A contradiction outranks every RED trigger it sits beside.
    flag = NEEDS_REVIEW if (contradiction or not red) else RED

    triggers = red + review
    return {
        "engagement_id": engagement["engagement_id"],
        "flag": flag,
        "triggers": "; ".join(triggers),
        "situation": " ".join(situations),
        "cause": "",
        "recommended_action": (
            NO_RECOMMENDATION_TEXT if set(triggers) <= NO_RECOMMENDATION else ""
        ),
        "decision_owner": DECISION_OWNER,
    }


def classify(pack: dict[str, Any]) -> list[dict[str, Any]]:
    """Every engagement in the pack, in engagement_id order so two runs agree."""
    fee_median = median_fee(pack["engagements"])
    burn, financials = pack.get("burn") or {}, pack.get("financials") or {}

    exceptions = []
    for engagement in pack["engagements"]:
        eid = str(engagement["engagement_id"])
        row = classify_one(engagement, burn.get(eid), financials.get(eid), fee_median)
        if row is not None:
            exceptions.append(row)

    return sorted(exceptions, key=lambda r: r["engagement_id"])


# --------------------------------------------------------------------------- entry


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("pack", type=Path, help="pack.json: the run's tool responses, unmodified.")
    ap.add_argument("--out", type=Path, help="Defaults to writing back to the pack file.")
    args = ap.parse_args()

    try:
        pack = load_pack(args.pack)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    exceptions = classify(pack)
    pack["exceptions"] = exceptions

    out = args.out or args.pack
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, default=str, indent=2))

    red = sum(1 for e in exceptions if e["flag"] == RED)
    print(
        f"{out}: {len(exceptions)} exception(s) — {red} RED, {len(exceptions) - red} NEEDS REVIEW. "
        "cause and recommended_action are left for the agent."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
