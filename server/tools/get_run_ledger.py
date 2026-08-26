"""get_run_ledger — has this period already been done, and has anything moved since.

The sixth tool, and the first thing a run calls. It exists because
``assemble-delivery-pack`` has carried this stop condition since step 6:

    The run ledger shows this period already produced a pack and the underlying figures
    have since changed. Report what changed and ask before regenerating.

Nothing could discharge it. There was no ledger a tool could read and no watermark to
compare against, so a Skill has been instructing agents to check something unreachable for
five steps. This makes the instruction keepable.

**On the tool count.** server/tools/__init__.py defends a four-tool cap on the *read*
surface, and this is a sixth tool. The cap is not being quietly abandoned: four tools open
the **triage** surface, and two now bracket the **run**. get_run_ledger asks whether the
run should happen at all and publish_pack closes it out, and neither answers a question
about an engagement. A caller deciding what to look at still has exactly four tools to
choose between, which is the property the cap was protecting.

**Why the ledger is a file rather than a table.** Step 12's plan called for a ``run_ledger``
table and it is an object in S3. The deployed server holds one Postgres credential, for a
role with SELECT and nothing else, and scripts/bootstrap_aurora.py proves the refusal on
every run. A table the server writes means a second role, a second secret, a write path
through server/db.py, and an asterisk on a claim that is currently absolute. The archive
already held a per-run ledger written at step 9, ``s3:GetObject`` on ``runs/*`` has been
granted since then so ``head`` could work, and the bucket is versioned — so a per-period
pointer costs no new permission, no new secret and no Terraform, and overwriting it each
month keeps every prior version. The full argument is in docs/notes/step-12-schedule.md.

**Two signals, not one, because they fail differently.** The watermark is the latest moment
anything the pack reads was filed; the digest is a fingerprint of the inputs themselves. An
entry filed and then corrected moves the digest. An entry filed against a month nobody is
reviewing moves the watermark and changes no figure. The decision is made on the digest,
and the watermark is what makes the *list* of what arrived recoverable.

**The watermark cannot enumerate everything the digest detects, and this says so.** Nothing
in db/schema.sql makes ``submitted_at`` monotonic — no trigger, no default, no check — so
an entry can be filed with a timestamp below the previous high-water mark, move the digest,
and never appear in a ``submitted_at > since`` scan. Neither is ``id`` a fallback: the
schema comment at db/schema.sql:7 says ids are assigned by sorting on ``entry_date``, so
they track when work happened rather than when it arrived.

Found while building this, on the seeded data: three entries inserted below the watermark
produced ``changed`` with an empty list, and the first version of this tool reported "no new
entry has been filed" — which was false. So the ledger records the row counts as well, and
a change the watermark cannot account for is described rather than denied. ``late_arrivals``
is what can be named; ``arrivals_complete`` says whether that is the whole story.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from server import archive as archive_mod
from server import db
from server.toollog import logged, new_run_id
from server.tools.common import Response, as_period_start, month_end

FIRST_RUN = "first_run"
UNCHANGED = "unchanged"
CHANGED = "changed"

# The SQL returns at most 200. Above that the point has been made and the list stops being
# something a person reads; total_count still carries the true number, computed before the
# limit, so a large backfill is never understated.
LATE_ARRIVAL_CAP = 200


class LateArrival(BaseModel):
    """One entry filed since the last run, named rather than counted."""

    model_config = ConfigDict(extra="forbid")

    id: int
    person_id: int
    person_name: str
    engagement_id: int
    engagement_name: str
    entry_date: dt.date
    hours: float
    billable: bool | None = None
    submitted_at: dt.datetime
    filed_after_period_close: bool = Field(
        description="Filed after the period closed. The classic late timesheet."
    )
    backdated: bool = Field(
        description="Dated before this period. It still moves hours_to_date, so it still moves this pack."
    )


class PriorRun(BaseModel):
    """The run that last covered this period, read out of the archive."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    published_at: dt.datetime | None = None
    watermark: dt.datetime | None = None
    figures_digest: str | None = None
    prefix: str | None = None
    ledger_key: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    run_count: int = Field(default=1, description="How many runs have published this period.")
    scoring_model_version: str | None = None
    entries_scanned: int | None = Field(
        default=None, description="Rows the previous run read. Compared against now to explain a digest change."
    )
    invoices_scanned: int | None = None


class RunLedger(Response):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(
        description=f"{FIRST_RUN}, {UNCHANGED}, or {CHANGED}. What the run should do next."
    )
    period: str
    period_start: dt.date
    period_end: dt.date

    current_watermark: dt.datetime | None = Field(
        default=None, description="Latest submitted_at across everything this pack reads. Null if nothing is filed."
    )
    figures_digest: str = Field(description="Fingerprint of the inputs. Equal digests mean an identical pack.")
    figures_changed: bool = Field(description="False on a first run: there is nothing to have changed from.")

    entries_scanned: int
    invoices_scanned: int

    prior_run: PriorRun | None = None

    late_arrivals: list[LateArrival] = Field(default_factory=list)
    late_arrivals_truncated: bool = False
    entries_added: int | None = Field(
        default=None, description="Change in row count since the previous run. Negative means rows were removed."
    )
    arrivals_complete: bool = Field(
        default=True,
        description=(
            "Whether late_arrivals accounts for the whole change. False means the digest moved "
            "in a way a watermark cannot enumerate: an amendment, a deletion, or a row filed "
            "below the previous high-water mark."
        ),
    )
    change_summary: str | None = Field(
        default=None, description="What moved, in one line, on a changed period."
    )

    next_step: str


def _prior(period_label: str) -> tuple[PriorRun | None, dict[str, Any]]:
    """Read the period pointer, or nothing at all.

    A period nobody has run has no pointer, and that is a first run rather than an error.
    See server/archive.py: on S3 a missing key under this prefix can arrive as a 403 rather
    than a 404, and get_bytes folds both into None so this branch behaves the same way on a
    laptop and on Lambda.

    A pointer that is present but unreadable is a different thing and is not swallowed. It
    would mean the archive holds a corrupt record of a run, and continuing as though the
    period had never been touched is exactly how a second pack gets published over a first.
    """
    raw = archive_mod.archive().get_bytes(archive_mod.period_key(period_label))
    if raw is None:
        return None, {}

    try:
        entry = json.loads(raw)
    except ValueError as exc:
        raise ValueError(
            f"The ledger pointer for {period_label} is present but is not readable JSON "
            f"({exc}). Something has written over it. Do not publish this period until "
            f"that is understood: a run that cannot read the ledger cannot tell a first "
            f"run from a second one."
        ) from None

    return (
        PriorRun(
            run_id=str(entry.get("run_id", "")),
            published_at=_as_dt(entry.get("published_at")),
            watermark=_as_dt(entry.get("watermark")),
            figures_digest=entry.get("figures_digest"),
            prefix=entry.get("prefix"),
            ledger_key=entry.get("ledger_key"),
            artifacts=[str(a) for a in entry.get("artifacts", [])],
            run_count=int(entry.get("run_count", 1)),
            scoring_model_version=entry.get("scoring_model_version"),
            entries_scanned=_as_int(entry.get("entries_scanned")),
            invoices_scanned=_as_int(entry.get("invoices_scanned")),
        ),
        entry,
    )


def _as_int(value: Any) -> int | None:
    """None rather than zero when the key is absent.

    The distinction is load-bearing. A pointer written before step 12 recorded row counts
    has no count, which is not the same as having counted nothing, and _describe_change
    branches on exactly that: it will not claim a change is fully accounted for when it has
    no baseline to account against.
    """
    return None if value is None else int(value)


def _as_dt(value: Any) -> dt.datetime | None:
    """An ISO string off the pointer, back to the instant it came from.

    Round-tripping a timestamp through JSON is where a watermark loses precision, and a
    watermark that loses precision re-reports entries the previous run already counted.
    Postgres is microsecond-resolution and isoformat carries all six digits, so this is
    lossless — but only because nothing rounds it on the way through. See the note in
    publish_pack about not passing it through utcnow().
    """
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    parsed = dt.datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _describe_change(added: int | None, late_total: int) -> tuple[str, bool]:
    """One line saying what moved, and whether the list of arrivals is the whole story.

    The honest cases are the awkward ones. A digest that moved with nothing above the
    watermark is not "nothing happened" — it is an amendment, a deletion, or a row filed
    out of order, and which of those it is can be narrowed by the row count but not always
    settled by it. Every branch that cannot be settled says so, because the alternative is
    a confident sentence that is sometimes wrong, and this line is read by an agent
    deciding whether to overwrite a partner's pack.
    """
    def entries(n: int) -> str:
        return f"{n} entr{'y' if abs(n) == 1 else 'ies'}"

    if added is None:
        # A pointer written before step 12 recorded row counts. Nothing to compare against.
        return (
            f"{entries(late_total)} filed since, and the previous run recorded no row count "
            f"to compare against, so there may be other changes."
        ), False

    if added > 0 and late_total >= added:
        return f"{entries(late_total)} filed since, which accounts for all {added} new row(s).", True

    if added > 0:
        return (
            f"{entries(added)} added, of which only {late_total} arrived after the previous "
            f"watermark. The rest were filed with an earlier timestamp and cannot be listed."
        ), False

    if added < 0:
        return (
            f"{entries(-added)} removed since the previous run"
            + (f", and {entries(late_total)} filed." if late_total else ".")
        ), False

    if late_total:
        return (
            f"The row count is unchanged but {entries(late_total)} arrived after the previous "
            f"watermark, so records were replaced rather than added."
        ), False

    return (
        "The row count is unchanged and nothing arrived after the previous watermark, so an "
        "existing record was amended in place."
    ), False


@logged
def get_run_ledger(period: str, run_id: str | None = None) -> RunLedger:
    """Whether this period has already been published, and what has been filed since it was.

    Call this first, before list_engagements, on every run. It reads the archive's ledger
    for the period and compares it against the data as it stands now, and returns one of
    three decisions. first_run: nothing has published this period, so assemble the pack.
    unchanged: a pack exists and not one input has moved since, so report that pack rather
    than building a second one. changed: a pack exists and the inputs have moved, so report
    the entries in late_arrivals by name and ask before regenerating.

    late_arrivals lists every entry filed since the previous run that this period's figures
    read, each flagged as filed after the period closed, backdated into an earlier month, or
    both. Backdated entries are included deliberately: hours_to_date is cumulative, so work
    dated in March that arrives in September still moves the burn percentage on this
    period's deck.

    Args:
        period: YYYY-MM, the period the pack covers.
        run_id: Groups this call with the rest of one run in the tool-call log.
    """
    period_start = as_period_start(period)
    period_end = month_end(period_start)
    period_label = f"{period_start:%Y-%m}"

    now = db.query("period_watermark", {"period_end": period_end})[0]
    digest = str(now["figures_digest"])
    watermark = now["watermark"]

    prior, _raw = _prior(period_label)

    late: list[LateArrival] = []
    late_total = 0
    if prior is not None and prior.watermark is not None:
        rows = db.query(
            "period_late_arrivals",
            {"period_start": period_start, "period_end": period_end, "since": prior.watermark},
        )
        late_total = int(rows[0]["total_count"]) if rows else 0
        late = [
            LateArrival(**{k: v for k, v in r.items() if k in LateArrival.model_fields})
            for r in rows
        ]

    added: int | None = None
    complete = True
    summary: str | None = None

    if prior is None:
        decision = FIRST_RUN
        changed = False
        next_step = (
            f"No pack has been published for {period_label}. Assemble it: "
            f"list_engagements next, then the coverage check."
        )
    elif prior.figures_digest == digest:
        decision = UNCHANGED
        changed = False
        next_step = (
            f"{period_label} was published by run {prior.run_id} and not one input has "
            f"moved since. Do not build a second pack. Report the existing one at "
            f"{prior.prefix} and stop."
        )
    else:
        decision = CHANGED
        changed = True
        if prior.entries_scanned is not None:
            added = int(now["entries_scanned"]) - prior.entries_scanned
        summary, complete = _describe_change(added, late_total)
        next_step = (
            f"{period_label} was published by run {prior.run_id} and the figures have "
            f"changed since. {summary} Report what changed, by engagement, and ask before "
            f"regenerating. Do not republish under the old run id."
        )

    return RunLedger(
        run_id=run_id or new_run_id(),
        total_count=late_total,
        returned_count=len(late),
        scoring_model_version=now["scoring_model_version"],
        decision=decision,
        period=period_label,
        period_start=period_start,
        period_end=period_end,
        current_watermark=watermark,
        figures_digest=digest,
        figures_changed=changed,
        entries_scanned=int(now["entries_scanned"]),
        invoices_scanned=int(now["invoices_scanned"]),
        prior_run=prior,
        late_arrivals=late,
        late_arrivals_truncated=late_total > len(late),
        entries_added=added,
        arrivals_complete=complete,
        change_summary=summary,
        next_step=next_step,
    )
