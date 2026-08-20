"""Deterministic synthetic consultancy generator.

One seed produces one portfolio, reproducibly, down to the byte. Two runs of the same
seed must yield identical checksums per table (db/checks/checksums.sql), which puts two
constraints on everything below:

1. Every random draw comes from a single ``random.Random(seed)``, consumed in a fixed
   order, and only through ``random()``, ``randrange()``, ``randint()``, ``choice()``
   and ``shuffle()``. Not ``sample()`` or ``gauss()``, whose implementations have moved
   between releases. The interpreter is pinned to 3.12 (.python-version) for the same
   reason.
2. Primary keys are assigned here, not by a sequence, and money and hours are Decimal
   throughout. A float would make the checksum depend on repr rounding.

Every name is composed at generation time from the word lists in this file. There is no
real client, person or engagement anywhere in the output, which is what makes a public
repo safe rather than merely tidy.
"""

from __future__ import annotations

import calendar
import datetime as dt
import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

# --------------------------------------------------------------------------------------
# Size of the world. These are the numbers in docs/spec-a-delivery-margin.md.
# --------------------------------------------------------------------------------------

N_CLIENTS = 12
N_PEOPLE = 25
N_ENGAGEMENTS = 30
N_ACTIVE = 18

# Exact, not approximate. 39,999 generated plus the mess case 2 duplicate is 40,000, and
# the demo line "40,000 time entries in, 40 aggregated rows out" should be true.
N_TIME_ENTRIES = 40_000

HISTORY_MONTHS = 12

# Entries per person per working day, and the hours that day covers. Roughly seven
# entries averaging 1.2 hours is what fine-grained time capture actually looks like;
# one entry per day would make the 40,000 unbelievable at this headcount.
ENTRIES_PER_DAY = (5, 9)
ENTRIES_PER_DAY_BOUNDS = (3, 11)  # the correction pass may not go outside this

# Time is captured in tenths of an hour, which is what time systems in this industry
# actually do, and which is also what makes the entries within one person's day
# distinguishable. See _split_day: every entry in a day gets a different size, so the
# only pair of rows in the database matching on person, day and hours is the duplicate
# mess case 2 puts there. At quarter-hour granularity a nine-entry day cannot have nine
# distinct sizes without running to eleven hours, and the collisions have to go
# somewhere.
UNIT = Decimal("0.1")
MIN_ENTRY_UNITS = 3  # 0.3 hours
DAY_HOURS_UNITS = (65, 100)  # 6.5 to 10.0 hours
MAX_DAY_UNITS = 105  # nobody gets a longer day than this, including the case 8 top-up

PTO_RATE = 0.07
NON_BILLABLE_RATE = 0.12

CENTS = Decimal("0.01")

# --------------------------------------------------------------------------------------
# Vocabulary. Composed, never copied.
# --------------------------------------------------------------------------------------

CLIENT_STEMS = [
    "Alderwick", "Braemar", "Calderstone", "Dunmarch", "Eastgate", "Fernhollow",
    "Granthorpe", "Havelock", "Inverleigh", "Kesterly", "Lowmarsh", "Merrivale",
    "Northfield", "Oakhaven", "Pentland", "Quarrymoor", "Ravensmere", "Stonebridge",
    "Thornbury", "Underwood", "Vantage", "Westmoor", "Yarrowfield", "Ashcombe",
]
CLIENT_QUALIFIERS = [
    "Utilities", "Logistics", "Financial", "Health", "Energy", "Retail", "Media",
    "Maritime", "Agricultural", "Transit", "Insurance", "Municipal",
]
CLIENT_SUFFIXES = ["Group", "Holdings", "Partners", "Collective", "Corporation", "Trust"]
CLIENT_SEGMENTS = ["enterprise", "mid_market", "public_sector", "non_profit"]

GIVEN_NAMES = [
    "Amara", "Bela", "Corin", "Dilara", "Emeka", "Farah", "Gideon", "Halima", "Idris",
    "Juno", "Kenji", "Liora", "Mateo", "Nadia", "Osric", "Priya", "Quinn", "Rafael",
    "Saoirse", "Tomas", "Ursula", "Viggo", "Wren", "Xiomara", "Yusuf", "Zora",
    "Anselm", "Beatrix", "Caius", "Delphine",
]
FAMILY_NAMES = [
    "Ashfield", "Brennock", "Castellan", "Draycott", "Elmhurst", "Fairweather",
    "Garrowby", "Hollybrook", "Ingersoll", "Jerrold", "Kastellan", "Lindqvist",
    "Marchetti", "Nordholm", "Ockenden", "Pemberly", "Quillon", "Rosenthal",
    "Sandoval", "Threadgill", "Umbridge", "Vasquez", "Whitlock", "Xanthos",
    "Yarrow", "Zeltner", "Barrowman", "Comstock", "Denholm", "Everard",
]

ENGAGEMENT_SUBJECTS = [
    "Billing Platform", "Customer Data", "Field Operations", "Payments", "Claims",
    "Supply Chain", "Workforce Planning", "Regulatory Reporting", "Asset Register",
    "Contact Centre", "Pricing", "Inventory", "Procurement", "Grid Analytics",
    "Member Portal", "Fleet Telemetry", "Revenue Assurance", "Credit Risk",
    "Warehouse Automation", "Digital Channels",
]
ENGAGEMENT_KINDS = [
    "Migration", "Modernisation", "Assessment", "Implementation", "Programme",
    "Discovery", "Rollout", "Remediation", "Optimisation", "Integration",
]

DELIVERABLES = [
    "Current state assessment", "Target operating model", "Data migration plan",
    "Integration build", "Cutover rehearsal", "Hypercare support", "Solution design",
    "Test strategy", "Vendor evaluation", "Benefits case", "Training materials",
    "Go-live readiness review",
]

BILLABLE_NOTES = [
    "Requirements workshop", "Data model review", "Stakeholder interviews",
    "Integration build", "Migration dry run", "Defect triage", "Test case authoring",
    "Architecture review", "Client status meeting", "Reconciliation analysis",
    "Cutover planning", "Solution walkthrough", "Environment configuration",
    "Report specification", "Interface mapping", "Performance tuning",
    "Acceptance criteria drafting", "Runbook drafting",
]
NON_BILLABLE_NOTES = [
    "Internal team sync", "Knowledge transfer", "Onboarding", "Estimation refinement",
    "Proposal support", "Internal QA review", "Tooling setup",
]

ROLE_MIX = (
    ("partner", 3),
    ("principal", 4),
    ("senior_consultant", 7),
    ("consultant", 7),
    ("analyst", 4),
)

# cost_rate range, bill_rate range, in whole currency units per hour
ROLE_RATES = {
    "partner": ((180, 225), (400, 480)),
    "principal": ((140, 175), (320, 385)),
    "senior_consultant": ((95, 128), (235, 290)),
    "consultant": ((70, 92), (180, 225)),
    "analyst": ((50, 66), (130, 165)),
}

# Firm-wide non-working days, as (month, day). Applied to whichever year of the window
# they land in, weekends excluded separately.
HOLIDAY_MONTH_DAYS = [
    (1, 1), (2, 16), (4, 3), (5, 18), (7, 1), (8, 3), (9, 7), (10, 12), (11, 11),
    (12, 25), (12, 28),
]


# --------------------------------------------------------------------------------------
# Small deterministic helpers. Nothing here calls into random module state.
# --------------------------------------------------------------------------------------


def month_start(d: dt.date) -> dt.date:
    return d.replace(day=1)


def month_end(d: dt.date) -> dt.date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d: dt.date, months: int) -> dt.date:
    total = (d.year * 12 + d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def months_between(start: dt.date, end: dt.date) -> list[dt.date]:
    """Every month start from the month containing start through the month containing end."""
    out = []
    cur = month_start(start)
    last = month_start(end)
    while cur <= last:
        out.append(cur)
        cur = add_months(cur, 1)
    return out


def weighted_pick(rng: random.Random, items: Sequence, weights: Sequence[int]):
    """Cumulative-weight pick, written out rather than using random.choices.

    random.choices is fine, but keeping every draw on randrange makes the stability
    argument in the module docstring true of the whole file rather than most of it.
    """
    total = sum(weights)
    target = rng.randrange(total)
    upto = 0
    for item, weight in zip(items, weights):
        upto += weight
        if target < upto:
            return item
    return items[-1]


def rand_decimal(rng: random.Random, low: int, high: int) -> Decimal:
    """A whole-unit rate in [low, high]."""
    return Decimal(rng.randint(low, high))


def units_to_hours(units: int) -> Decimal:
    return (Decimal(units) * UNIT).quantize(CENTS)


# --------------------------------------------------------------------------------------
# Intermediate objects. These carry the logic; tuples come out at the end.
# --------------------------------------------------------------------------------------


@dataclass
class Person:
    id: int
    name: str
    role: str
    cost_rate: Decimal
    bill_rate: Decimal


@dataclass
class Engagement:
    id: int
    client_id: int
    name: str
    sow_ref: str
    fee_type: str
    ceiling_hours: Decimal
    ceiling_amount: Decimal
    start_date: dt.date
    end_date: dt.date
    status: str


@dataclass
class Assignment:
    person_id: int
    engagement_id: int
    start: dt.date
    end: dt.date
    weight: int


@dataclass
class Entry:
    person_id: int
    engagement_id: int
    entry_date: dt.date
    units: int
    billable: bool | None
    note: str
    submitted_at: dt.datetime
    seq: int  # generation order, the stable tiebreak when ids are assigned


@dataclass
class Portfolio:
    period_start: dt.date
    period_end: dt.date
    window_start: dt.date
    window_end: dt.date
    clients: list[tuple] = field(default_factory=list)
    people: list[tuple] = field(default_factory=list)
    engagements: list[tuple] = field(default_factory=list)
    sow_line_items: list[tuple] = field(default_factory=list)
    time_entries: list[tuple] = field(default_factory=list)
    invoices: list[tuple] = field(default_factory=list)
    mess: dict = field(default_factory=dict)


COLUMNS = {
    "clients": ("id", "name", "segment"),
    "people": ("id", "name", "role", "cost_rate", "bill_rate"),
    "engagements": (
        "id", "client_id", "name", "sow_ref", "fee_type", "ceiling_hours",
        "ceiling_amount", "start_date", "end_date", "status",
    ),
    "sow_line_items": ("id", "engagement_id", "deliverable", "hours_budgeted", "amount", "due_date"),
    "time_entries": (
        "id", "person_id", "engagement_id", "entry_date", "hours", "billable", "note",
        "submitted_at",
    ),
    "invoices": ("id", "engagement_id", "period", "amount", "status", "issued_at", "paid_at"),
}


# --------------------------------------------------------------------------------------
# The builder.
#
# Order matters and is not arbitrary. The mess anchors are chosen straight after the
# engagements exist, because case 6 shortens an engagement and case 3 silences one, and
# both have to be true before anyone is staffed or any entry is planned. Cases 1, 2, 5
# and 8 are row-level and land after the entries are materialised. Case 4 needs the
# actual cost of the hours logged, so it rewrites its engagement's ceiling afterwards,
# and the SOW line items are generated after that so they add up to the ceiling that
# ended up on the row rather than the one it started with.
# --------------------------------------------------------------------------------------


class _Builder:
    def __init__(self, seed: int, period: str) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.period_start = dt.datetime.strptime(period + "-01", "%Y-%m-%d").date()
        self.period_end = month_end(self.period_start)
        self.window_end = self.period_end
        self.window_start = month_start(add_months(self.period_start, -(HISTORY_MONTHS - 1)))

        self.holidays = self._holidays()
        self.workdays = [
            d
            for d in self._days(self.window_start, self.window_end)
            if d.weekday() < 5 and d not in self.holidays
        ]

        # Monday of the second full week of the period. Mess case 7 empties it, and
        # mess case 3's silence starts the same day and runs to the period end.
        first_monday = self.period_start
        while first_monday.weekday() != 0:
            first_monday += dt.timedelta(days=1)
        self.coverage_week_start = first_monday + dt.timedelta(days=7)
        self.coverage_week_end = self.coverage_week_start + dt.timedelta(days=6)
        self.silent_start = self.coverage_week_start

        self.clients: list[tuple] = []
        self.people: list[Person] = []
        self.engagements: list[Engagement] = []
        self.by_id: dict[int, Engagement] = {}
        self.assignments: list[Assignment] = []
        self.by_person: dict[int, list[Assignment]] = {}
        self.entries: list[Entry] = []
        self.entry_ids: dict[int, int] = {}
        self.mess: dict = {}

    # -- calendar -----------------------------------------------------------------

    @staticmethod
    def _days(start: dt.date, end: dt.date) -> Iterable[dt.date]:
        cur = start
        while cur <= end:
            yield cur
            cur += dt.timedelta(days=1)

    def _holidays(self) -> set[dt.date]:
        out = set()
        for year in range(self.window_start.year, self.window_end.year + 1):
            for month, day in HOLIDAY_MONTH_DAYS:
                d = dt.date(year, month, day)
                if self.window_start <= d <= self.window_end:
                    out.add(d)
        return out

    # -- the portfolio ------------------------------------------------------------

    def _build_clients(self) -> None:
        stems = list(CLIENT_STEMS)
        self.rng.shuffle(stems)
        segment_weights = [5, 4, 2, 1]
        for i in range(N_CLIENTS):
            name = (
                f"{stems[i]} {self.rng.choice(CLIENT_QUALIFIERS)} "
                f"{self.rng.choice(CLIENT_SUFFIXES)}"
            )
            segment = weighted_pick(self.rng, CLIENT_SEGMENTS, segment_weights)
            self.clients.append((i + 1, name, segment))

    def _build_people(self) -> None:
        roles: list[str] = []
        for role, count in ROLE_MIX:
            roles.extend([role] * count)
        self.rng.shuffle(roles)

        given = list(GIVEN_NAMES)
        family = list(FAMILY_NAMES)
        self.rng.shuffle(given)
        self.rng.shuffle(family)

        for i in range(N_PEOPLE):
            role = roles[i]
            (cost_lo, cost_hi), (bill_lo, bill_hi) = ROLE_RATES[role]
            self.people.append(
                Person(
                    id=i + 1,
                    name=f"{given[i]} {family[i]}",
                    role=role,
                    cost_rate=rand_decimal(self.rng, cost_lo, cost_hi).quantize(CENTS),
                    bill_rate=rand_decimal(self.rng, bill_lo, bill_hi).quantize(CENTS),
                )
            )

    def _build_engagements(self) -> None:
        statuses = ["active"] * N_ACTIVE + ["completed"] * 10 + ["on_hold"] * 2
        self.rng.shuffle(statuses)

        fee_types = ["fixed"] * 12 + ["time_and_materials"] * 18
        self.rng.shuffle(fee_types)

        # Mess case 4 needs an active fixed-fee engagement. With 12 fixed out of 30 the
        # shuffle almost always supplies several, but "almost always" is not a property
        # a fixture set can have, so guarantee it.
        active_fixed = [i for i in range(N_ENGAGEMENTS) if statuses[i] == "active" and fee_types[i] == "fixed"]
        if len(active_fixed) < 4:
            swap_in = [i for i in range(N_ENGAGEMENTS) if statuses[i] == "active" and fee_types[i] != "fixed"]
            swap_out = [i for i in range(N_ENGAGEMENTS) if statuses[i] != "active" and fee_types[i] == "fixed"]
            while len(active_fixed) < 4 and swap_in and swap_out:
                a, b = swap_in.pop(0), swap_out.pop(0)
                fee_types[a], fee_types[b] = "fixed", "time_and_materials"
                active_fixed.append(a)

        client_slots = list(range(1, N_CLIENTS + 1))
        client_slots += [self.rng.randint(1, N_CLIENTS) for _ in range(N_ENGAGEMENTS - N_CLIENTS)]
        self.rng.shuffle(client_slots)

        used_names: set[str] = set()
        for i in range(N_ENGAGEMENTS):
            status = statuses[i]
            fee_type = fee_types[i]

            while True:
                name = (
                    f"{self.rng.choice(ENGAGEMENT_SUBJECTS)} "
                    f"{self.rng.choice(ENGAGEMENT_KINDS)}"
                )
                if name not in used_names:
                    used_names.add(name)
                    break

            ceiling_hours = Decimal(self.rng.randrange(200, 3201, 20))
            blended = Decimal(self.rng.randint(240, 330))
            if fee_type == "fixed":
                # A negotiated fee, not hours times a rate card.
                factor = Decimal(self.rng.randint(85, 105)) / Decimal(100)
                ceiling_amount = (ceiling_hours * blended * factor).quantize(CENTS)
            else:
                ceiling_amount = (ceiling_hours * blended).quantize(CENTS)

            if status == "active":
                start = self.window_start + dt.timedelta(days=self.rng.randrange(-210, 200))
                end = self.window_end + dt.timedelta(days=self.rng.randrange(20, 300))
                if end < start + dt.timedelta(days=120):
                    end = start + dt.timedelta(days=120)
            elif status == "completed":
                end = self.window_end - dt.timedelta(days=self.rng.randrange(20, 210))
                start = end - dt.timedelta(days=self.rng.randrange(90, 400))
            else:  # on_hold
                start = self.window_start - dt.timedelta(days=self.rng.randrange(30, 200))
                end = self.window_end + dt.timedelta(days=self.rng.randrange(30, 200))

            self.engagements.append(
                Engagement(
                    id=i + 1,
                    client_id=client_slots[i],
                    name=name,
                    sow_ref=f"SOW-{start.year}-{i + 1:03d}",
                    fee_type=fee_type,
                    ceiling_hours=ceiling_hours.quantize(CENTS),
                    ceiling_amount=ceiling_amount,
                    start_date=start,
                    end_date=end,
                    status=status,
                )
            )
        self.by_id = {e.id: e for e in self.engagements}

    # -- mess anchors -------------------------------------------------------------

    def _choose_mess_anchors(self) -> None:
        active = [e for e in self.engagements if e.status == "active"]
        pool = [e.id for e in active]
        self.rng.shuffle(pool)

        def take(predicate=None) -> int:
            for idx, eid in enumerate(pool):
                if predicate is None or predicate(self.by_id[eid]):
                    return pool.pop(idx)
            raise RuntimeError("ran out of active engagements while placing mess cases")

        # Case 4 first: it is the only one with a hard requirement on fee type.
        case4 = take(lambda e: e.fee_type == "fixed")

        # Case 6: an engagement that stops mid-period. Shortening the end date here,
        # before staffing, is what makes its time entries actually stop.
        #
        # It lands after the coverage week rather than inside it. An engagement whose
        # last days fall in case 7's blackout logs nothing near its own end date, which
        # both weakens the period-boundary case and makes it read as a second silent
        # engagement — two mess cases quietly cancelling each other out.
        case6 = take(lambda e: e.start_date < self.period_start)
        self.by_id[case6].end_date = self.coverage_week_end + dt.timedelta(days=5)

        # Case 3: silent for the last three weeks, and still contracted well past the
        # period, so nothing in the data suggests it finished.
        case3 = take(lambda e: e.end_date > self.period_end + dt.timedelta(days=30))

        # Case 8 has to be an engagement worth worrying about. scope-escalation only
        # flags key-person concentration above the median fee, so a two-person job with
        # thirty hours in it would satisfy the arithmetic and none of the point.
        fees = sorted(e.ceiling_amount for e in active)
        median_fee = fees[len(fees) // 2]
        case8 = take(lambda e: e.ceiling_amount >= median_fee)

        case1 = [take(), take(), take()]
        case2 = take()
        case5 = take()

        # Case 7: the coverage week. Everything goes dark except three engagements,
        # which puts reporting coverage at 3/18 = 16.7%, under the 20% in the spec and
        # far under the 60% rule assemble-delivery-pack applies at step 6.
        reporting_pool = [
            e.id
            for e in active
            if e.id not in {case3, case6} and e.start_date <= self.coverage_week_start
        ]
        self.rng.shuffle(reporting_pool)
        case7_reporting = sorted(reporting_pool[:3])

        self.mess = {
            "case_1_late_engagements": sorted(case1),
            "case_1_late_rates": {},  # filled in when applied
            "case_2_duplicate_engagement": case2,
            "case_3_silent_engagement": case3,
            "case_3_silent_from": self.silent_start,
            "case_4_fixed_fee_engagement": case4,
            "case_5_null_billable_engagement": case5,
            "case_6_mid_period_end_engagement": case6,
            "case_6_end_date": self.by_id[case6].end_date,
            "case_7_week_start": self.coverage_week_start,
            "case_7_week_end": self.coverage_week_end,
            "case_7_reporting_engagements": case7_reporting,
            "case_8_concentration_engagement": case8,
            "active_engagement_ids": sorted(e.id for e in active),
        }

    # -- staffing -----------------------------------------------------------------

    def _build_assignments(self) -> None:
        by_role: dict[str, list[int]] = {role: [] for role, _ in ROLE_MIX}
        for p in self.people:
            by_role[p.role].append(p.id)

        leaders = by_role["partner"] + by_role["principal"]
        seniors = by_role["senior_consultant"]
        juniors = by_role["consultant"] + by_role["analyst"]

        for e in self.engagements:
            live_start = max(e.start_date, self.window_start)
            live_end = min(e.end_date, self.window_end)
            if live_end < live_start:
                continue

            # Mess case 8 is staffed rather than imposed: one senior weighted far above
            # anything else they are on, a small supporting team weighted far below
            # theirs, and the concentration falls out of how the engagement is run. The
            # alternative — moving other people's hours onto one name afterwards — put
            # 260 hours in a month against a single person.
            is_case8 = e.id == self.mess["case_8_concentration_engagement"]

            # Teams are small because people are on two or three things at a time, not
            # eight. Oversized teams here put every consultant on most of the portfolio
            # at once, which reads as nonsense to anyone who has worked in services and
            # dilutes any concentration the data is supposed to show.
            team_size = max(2, min(6, int(e.ceiling_hours / Decimal(600)) + 1))
            n_senior = 1 if team_size <= 4 else 2
            n_junior = max(0, team_size - 1 - n_senior)
            if is_case8:
                # The lead and one senior, nobody else. A junior staffed here and
                # nowhere else spends their whole month on it whatever their weight
                # says, because weight only decides between the engagements a person is
                # actually on — which is how a supporting cast ends up owning 70% of an
                # engagement that was supposed to demonstrate the opposite.
                n_senior, n_junior = 1, 0

            chosen: list[tuple[int, int]] = []  # (person_id, weight)
            chosen.append((self.rng.choice(leaders), 1))

            picked: set[int] = {chosen[0][0]}
            for _ in range(n_senior):
                for _attempt in range(20):
                    pid = self.rng.choice(seniors)
                    if pid not in picked:
                        picked.add(pid)
                        chosen.append((pid, 20 if is_case8 else 3 + self.rng.randint(0, 1)))
                        if is_case8:
                            self.mess["case_8_person_id"] = pid
                        break
            for _ in range(n_junior):
                for _attempt in range(20):
                    pid = self.rng.choice(juniors)
                    if pid not in picked:
                        picked.add(pid)
                        chosen.append((pid, 1 if is_case8 else 3 + self.rng.randint(0, 2)))
                        break

            for index, (pid, weight) in enumerate(chosen):
                # People roll on after kickoff and off before completion — but only at
                # real boundaries. live_start and live_end are clamped to the generation
                # window, and jittering against the clamp would roll the whole firm off
                # every still-running engagement two weeks before the period ends. That
                # would put a portfolio-wide dip in the last fortnight of the demo month
                # and make it indistinguishable from mess case 7, which is the one dip
                # that is supposed to be there.
                rolls_on = index > 0 and e.start_date >= self.window_start
                rolls_off = index > 0 and e.end_date <= self.window_end

                start = (
                    live_start + dt.timedelta(days=self.rng.randrange(0, 21))
                    if rolls_on
                    else live_start
                )
                end = (
                    live_end - dt.timedelta(days=self.rng.randrange(0, 16))
                    if rolls_off
                    else live_end
                )
                if end < start:
                    start, end = live_start, live_end
                self.assignments.append(Assignment(pid, e.id, start, end, weight))

        self.by_person = {p.id: [] for p in self.people}
        for a in self.assignments:
            self.by_person[a.person_id].append(a)

    def _candidates(self, person_id: int, day: dt.date) -> list[Assignment]:
        """Engagements this person could log against on this day, after suppressions.

        Deterministic and free of random state, because planning and materialisation
        both call it and must agree.
        """
        out = []
        in_coverage_week = self.coverage_week_start <= day <= self.coverage_week_end
        silent_eid = self.mess["case_3_silent_engagement"]
        reporting = self.mess["case_7_reporting_engagements"]

        for a in self.by_person[person_id]:
            if not (a.start <= day <= a.end):
                continue
            if a.engagement_id == silent_eid and day >= self.silent_start:
                continue  # case 3
            if in_coverage_week and a.engagement_id not in reporting:
                continue  # case 7
            out.append(a)
        return out

    # -- time entries -------------------------------------------------------------

    def _plan_entry_counts(self) -> dict[tuple[int, dt.date], int]:
        planned: dict[tuple[int, dt.date], int] = {}
        for person in self.people:
            for day in self.workdays:
                # Drawn before the candidate check so the random sequence does not
                # depend on staffing lookups.
                on_pto = self.rng.random() < PTO_RATE
                count = self.rng.randint(*ENTRIES_PER_DAY)
                if on_pto:
                    continue
                if not self._candidates(person.id, day):
                    continue
                planned[(person.id, day)] = count
        return planned

    def _correct_to_target(self, planned: dict[tuple[int, dt.date], int]) -> None:
        """Nudge the plan until it sums to exactly the target.

        Mess cases 3 and 7 delete work from the calendar, and the shortfall has to land
        somewhere or the coverage week would show up as a dip in the annual total rather
        than a reporting gap. Spreading it across the other person-days is what makes
        the rest of the year look ordinary.
        """
        target = N_TIME_ENTRIES - 1  # the case 2 duplicate is the forty-thousandth row
        keys = sorted(planned)
        lo, hi = ENTRIES_PER_DAY_BOUNDS
        delta = target - sum(planned.values())

        guard = 0
        while delta != 0:
            key = keys[self.rng.randrange(len(keys))]
            value = planned[key]
            if delta > 0 and value < hi:
                planned[key] = value + 1
                delta -= 1
            elif delta < 0 and value > lo:
                planned[key] = value - 1
                delta += 1
            guard += 1
            if guard > 10_000_000:
                raise RuntimeError("could not reach the time entry target within bounds")

    def _submitted_at(self, day: dt.date) -> dt.datetime:
        """When the entry was filed, which is not when the work happened.

        Most time lands within a day or two; some of it takes a week. Mess case 1 pushes
        a slice of this past the period close later on.
        """
        lag = weighted_pick(self.rng, (0, 1, 2, 3, 4, 5), (34, 28, 18, 11, 6, 3))
        hour = self.rng.randint(8, 19)
        minute = self.rng.randrange(60)
        second = self.rng.randrange(60)
        return dt.datetime.combine(
            day + dt.timedelta(days=lag),
            dt.time(hour, minute, second),
            tzinfo=dt.timezone.utc,
        )

    def _split_day(self, n: int, total_units: int) -> list[int]:
        """Split a day into n entry sizes, all different, all at least MIN_ENTRY_UNITS.

        Starting from the smallest legal strictly-increasing set and adding the surplus
        one unit at a time, only where it does not close a gap, keeps every size
        distinct without ever needing to lengthen the day to make room.
        """
        values = [MIN_ENTRY_UNITS + i for i in range(n)]
        surplus = max(0, total_units - sum(values))
        while surplus > 0:
            i = self.rng.randrange(n)
            if i == n - 1 or values[i] + 1 < values[i + 1]:
                values[i] += 1
                surplus -= 1
        self.rng.shuffle(values)
        return values

    def _materialise_entries(self, planned: dict[tuple[int, dt.date], int]) -> None:
        seen: set[tuple[int, int, dt.date, int]] = set()
        seq = 0

        for (person_id, day) in sorted(planned):
            n = planned[(person_id, day)]
            cands = self._candidates(person_id, day)

            chunks = self._split_day(n, self.rng.randint(*DAY_HOURS_UNITS))

            k = min(len(cands), self.rng.choice((1, 1, 2, 2, 3)))
            pool = list(cands)
            weights = [a.weight for a in pool]
            picked: list[Assignment] = []
            for _ in range(k):
                a = weighted_pick(self.rng, pool, weights)
                idx = pool.index(a)
                pool.pop(idx)
                weights.pop(idx)
                picked.append(a)
            picked_weights = [a.weight for a in picked]

            for units in chunks:
                a = weighted_pick(self.rng, picked, picked_weights)
                billable = self.rng.random() >= NON_BILLABLE_RATE
                note = self.rng.choice(BILLABLE_NOTES if billable else NON_BILLABLE_NOTES)
                submitted_at = self._submitted_at(day)

                # Sizes within a day are already distinct, so this is a record of what
                # exists rather than a collision check. Case 8 reads it when it moves
                # entries between people.
                seen.add((person_id, a.engagement_id, day, units))

                self.entries.append(
                    Entry(
                        person_id=person_id,
                        engagement_id=a.engagement_id,
                        entry_date=day,
                        units=units,
                        billable=billable,
                        note=note,
                        submitted_at=submitted_at,
                        seq=seq,
                    )
                )
                seq += 1

        self._seen = seen

    # -- row-level mess -----------------------------------------------------------

    def _open_engagements(self, day: dt.date) -> list[int]:
        """Engagements anyone could plausibly log against on this day.

        Used only as the last resort in case 8, where somebody's hours have to go
        somewhere and their own staffing offers nowhere. Respects contract dates and
        both suppressions, so it cannot put time on an engagement that had not started,
        had finished, or is meant to be dark.
        """
        silent_eid = self.mess["case_3_silent_engagement"]
        reporting = self.mess["case_7_reporting_engagements"]
        in_coverage_week = self.coverage_week_start <= day <= self.coverage_week_end

        out = []
        for e in self.engagements:
            if not (e.start_date <= day <= e.end_date):
                continue
            if e.id == silent_eid and day >= self.silent_start:
                continue
            if in_coverage_week and e.id not in reporting:
                continue
            out.append(e.id)
        return out

    def _period_entries(self, engagement_id: int) -> list[Entry]:
        return sorted(
            (
                e
                for e in self.entries
                if e.engagement_id == engagement_id
                and self.period_start <= e.entry_date <= self.period_end
            ),
            key=lambda e: (e.entry_date, e.seq),
        )

    def _apply_case_8(self) -> None:
        """Top the concentration up to 85% of the engagement's period hours.

        The two-person team already produces most of this. The rest comes from thinning
        the lead's involvement, not from adding hours to the senior: the senior already
        works full days, so anything piled on top of them would be a thirteen-hour
        Tuesday. The lead's entries move to another engagement they are staffed on that
        day, which leaves every person-day exactly as long as it was and shrinks the
        denominator instead of inflating the numerator.
        """
        eid = self.mess["case_8_concentration_engagement"]
        target_id = self.mess.get("case_8_person_id")
        entries = self._period_entries(eid)
        if not entries or target_id is None:
            self.mess["case_8_share"] = None
            return

        totals: dict[int, int] = {}
        for e in entries:
            totals[e.person_id] = totals.get(e.person_id, 0) + e.units
        totals.setdefault(target_id, 0)

        day_load: dict[dt.date, int] = {}
        for e in self.entries:
            if e.person_id == target_id:
                day_load[e.entry_date] = day_load.get(e.entry_date, 0) + e.units

        for e in entries:
            if totals[target_id] / sum(totals.values()) >= 0.85:
                break
            if e.person_id == target_id:
                continue

            alternatives = [
                a
                for a in self._candidates(e.person_id, e.entry_date)
                if a.engagement_id != eid
            ]

            original_units = e.units
            owner = e.person_id

            if alternatives:
                dest_person = owner
                dest_engagement = weighted_pick(
                    self.rng, alternatives, [a.weight for a in alternatives]
                ).engagement_id
            elif day_load.get(e.entry_date, 0) + original_units <= MAX_DAY_UNITS:
                # Nowhere else for this person to be that day, so the hours go to the
                # senior instead — but only onto a day they have room for.
                dest_person, dest_engagement = target_id, eid
            else:
                # The senior is already working a full day, so the hours cannot go to
                # them. They land on another engagement that was running that day. A
                # lead booking a couple of hours somewhere they are not formally
                # staffed is ordinary; a thirteen-hour day is not.
                open_ids = [x for x in self._open_engagements(e.entry_date) if x != eid]
                if not open_ids:
                    continue
                dest_person = owner
                dest_engagement = open_ids[self.rng.randrange(len(open_ids))]

            self._seen.discard((owner, eid, e.entry_date, original_units))

            # The destination may already hold an entry of this size from this person on
            # this day, and a second duplicate group would break mess case 2's assertion.
            units = original_units
            while (dest_person, dest_engagement, e.entry_date, units) in self._seen:
                units += 1
            e.person_id = dest_person
            e.engagement_id = dest_engagement
            e.units = units
            self._seen.add((dest_person, dest_engagement, e.entry_date, units))

            totals[owner] -= original_units
            if dest_person == target_id:
                totals[target_id] += units
                day_load[e.entry_date] = day_load.get(e.entry_date, 0) + units

        self.mess["case_8_share"] = round(totals[target_id] / sum(totals.values()), 4)

    def _apply_case_1(self) -> None:
        """Time filed after the period closed.

        Two engagements at roughly 5%, matching the spec, and a third at 12%. The
        deliberate third is because get_engagement_burn only drops
        projection_confidence to low above 10% late: at a flat 5% this case would never
        exercise the rule the plan maps it to, and the only low-confidence engagement in
        the data would be the one with a reporting gap. See
        docs/notes/step-1-mess-cases.md.
        """
        rates = [Decimal("0.05"), Decimal("0.05"), Decimal("0.12")]
        applied = {}
        for eid, rate in zip(self.mess["case_1_late_engagements"], rates):
            entries = self._period_entries(eid)
            if not entries:
                continue
            n = max(1, int(len(entries) * rate))
            indices = list(range(len(entries)))
            self.rng.shuffle(indices)
            for i in indices[:n]:
                e = entries[i]
                days_late = self.rng.randint(1, 12)
                e.submitted_at = dt.datetime.combine(
                    self.period_end + dt.timedelta(days=days_late),
                    dt.time(self.rng.randint(8, 19), self.rng.randrange(60), self.rng.randrange(60)),
                    tzinfo=dt.timezone.utc,
                )
            applied[eid] = {"late": n, "of": len(entries)}
        self.mess["case_1_late_rates"] = applied

    def _apply_case_5(self) -> None:
        eid = self.mess["case_5_null_billable_engagement"]
        entries = self._period_entries(eid)
        indices = list(range(len(entries)))
        self.rng.shuffle(indices)
        touched = []
        for i in indices[:2]:
            entries[i].billable = None
            touched.append(i)
        self.mess["case_5_count"] = len(touched)

    def _apply_case_2(self) -> None:
        """The one duplicate: same person, same day, same hours, filed twice."""
        eid = self.mess["case_2_duplicate_engagement"]
        entries = self._period_entries(eid)
        source = entries[self.rng.randrange(len(entries))]
        self.entries.append(
            Entry(
                person_id=source.person_id,
                engagement_id=source.engagement_id,
                entry_date=source.entry_date,
                units=source.units,
                billable=source.billable,
                note=source.note,
                # Filed a few minutes later, which is what a double submission looks
                # like. The detection key is person, engagement, date and hours.
                submitted_at=source.submitted_at + dt.timedelta(minutes=self.rng.randint(2, 40)),
                seq=len(self.entries),
            )
        )
        self.mess["case_2_person_id"] = source.person_id
        self.mess["case_2_entry_date"] = source.entry_date

    def _apply_case_4(self) -> None:
        """A fixed-fee engagement losing money while its burn looks fine.

        Burn is hours against the ceiling and margin is fee against cost, and nothing
        makes them agree. The ceiling is set so burn lands near 65% — comfortably
        healthy — and the fee is set below the cost of the hours already delivered, so
        margin is about -18%. Both numbers are true at once, which is the whole point:
        scope-escalation at step 8 has to state the contradiction rather than resolve it.
        """
        eid = self.mess["case_4_fixed_fee_engagement"]
        engagement = self.by_id[eid]
        person_by_id = {p.id: p for p in self.people}

        units = 0
        cost = Decimal(0)
        for e in self.entries:
            if e.engagement_id != eid:
                continue
            units += e.units
            cost += units_to_hours(e.units) * person_by_id[e.person_id].cost_rate

        hours = units_to_hours(units)
        if hours <= 0:
            return

        ceiling_hours = (hours / Decimal("0.65")).quantize(Decimal("1"))
        ceiling_hours = (ceiling_hours / Decimal(10)).quantize(Decimal("1")) * Decimal(10)
        engagement.ceiling_hours = ceiling_hours.quantize(CENTS)
        engagement.ceiling_amount = (cost * Decimal("0.85")).quantize(CENTS)

        self.mess["case_4_hours_to_date"] = hours
        self.mess["case_4_burn_pct"] = round(float(hours / ceiling_hours), 4)
        self.mess["case_4_cost_to_date"] = cost.quantize(CENTS)
        self.mess["case_4_margin_pct"] = round(
            float((engagement.ceiling_amount - cost) / engagement.ceiling_amount), 4
        )

    def _assign_entry_ids(self) -> None:
        ordered = sorted(
            self.entries, key=lambda e: (e.entry_date, e.person_id, e.engagement_id, e.seq)
        )
        for i, e in enumerate(ordered, start=1):
            self.entry_ids[e.seq] = i
        self._ordered_entries = ordered

    # -- SOW line items and invoices ----------------------------------------------

    def _build_sow_line_items(self) -> list[tuple]:
        rows = []
        next_id = 1
        for e in self.engagements:
            n = self.rng.randint(3, 5)
            weights = [self.rng.randint(2, 6) for _ in range(n)]
            total_weight = sum(weights)

            hours_left = e.ceiling_hours
            amount_left = e.ceiling_amount
            span = (e.end_date - e.start_date).days or 1

            deliverables = list(DELIVERABLES)
            self.rng.shuffle(deliverables)

            for i in range(n):
                if i == n - 1:
                    hours = hours_left
                    amount = amount_left
                else:
                    hours = (e.ceiling_hours * Decimal(weights[i]) / Decimal(total_weight)).quantize(CENTS)
                    amount = (e.ceiling_amount * Decimal(weights[i]) / Decimal(total_weight)).quantize(CENTS)
                    hours_left -= hours
                    amount_left -= amount
                due = e.start_date + dt.timedelta(days=int(span * (i + 1) / n))
                rows.append((next_id, e.id, deliverables[i], hours, amount, due))
                next_id += 1
        return rows

    def _build_invoices(self, sow_rows: list[tuple]) -> list[tuple]:
        person_by_id = {p.id: p for p in self.people}

        # Billable value per engagement per month, from the entries themselves rather
        # than from the ceiling. An invoice that does not follow the time is a fiction.
        billable_value: dict[tuple[int, dt.date], Decimal] = {}
        for e in self.entries:
            if not e.billable:
                continue
            key = (e.engagement_id, month_start(e.entry_date))
            value = units_to_hours(e.units) * person_by_id[e.person_id].bill_rate
            billable_value[key] = billable_value.get(key, Decimal(0)) + value

        client_dso = {cid: self.rng.randint(18, 52) for cid, _, _ in self.clients}
        slow_payer = self.rng.choice([cid for cid, _, _ in self.clients])
        slow_from = month_start(add_months(self.period_start, -2))

        sow_by_engagement: dict[int, list[tuple]] = {}
        for row in sow_rows:
            sow_by_engagement.setdefault(row[1], []).append(row)

        rows = []
        next_id = 1

        def settle(engagement: Engagement, period: dt.date, amount: Decimal, issued_at: dt.date):
            nonlocal next_id
            if issued_at > self.window_end or amount <= 0:
                return
            lag = client_dso[engagement.client_id] + self.rng.randint(-6, 12)
            if engagement.client_id == slow_payer and period >= slow_from:
                lag += self.rng.randint(20, 35)
            paid_at = issued_at + dt.timedelta(days=max(1, lag))

            if self.rng.random() < 0.02:
                status, paid = "void", None
            elif paid_at <= self.window_end:
                status, paid = "paid", paid_at
            else:
                status, paid = "issued", None
            rows.append((next_id, engagement.id, period, amount.quantize(CENTS), status, issued_at, paid))
            next_id += 1

        for e in self.engagements:
            live_start = max(e.start_date, self.window_start)
            live_end = min(e.end_date, self.window_end)
            if live_end < live_start:
                continue

            if e.fee_type == "time_and_materials":
                # The final month of the window stays uninvoiced on purpose: that is the
                # work in progress get_financials reports as wip_unbilled.
                for period in months_between(live_start, live_end):
                    if period >= month_start(self.window_end):
                        continue
                    amount = billable_value.get((e.id, period))
                    if not amount:
                        continue
                    issued_at = month_end(period) + dt.timedelta(days=self.rng.randint(3, 9))
                    settle(e, period, amount, issued_at)
            else:
                for row in sow_by_engagement.get(e.id, []):
                    due = row[5]
                    if not (self.window_start <= due <= self.window_end - dt.timedelta(days=25)):
                        continue
                    issued_at = due + dt.timedelta(days=self.rng.randint(2, 10))
                    settle(e, month_start(due), row[4], issued_at)

        self.mess["slow_payer_client_id"] = slow_payer
        return rows

    # -- run ----------------------------------------------------------------------

    def run(self) -> Portfolio:
        self._build_clients()
        self._build_people()
        self._build_engagements()
        self._choose_mess_anchors()
        self._build_assignments()

        planned = self._plan_entry_counts()
        self._correct_to_target(planned)
        self._materialise_entries(planned)

        self._apply_case_8()
        self._apply_case_1()
        self._apply_case_5()
        self._apply_case_2()
        self._apply_case_4()
        self._assign_entry_ids()

        sow_rows = self._build_sow_line_items()
        invoice_rows = self._build_invoices(sow_rows)

        portfolio = Portfolio(
            period_start=self.period_start,
            period_end=self.period_end,
            window_start=self.window_start,
            window_end=self.window_end,
        )
        portfolio.clients = self.clients
        portfolio.people = [
            (p.id, p.name, p.role, p.cost_rate, p.bill_rate) for p in self.people
        ]
        portfolio.engagements = [
            (
                e.id, e.client_id, e.name, e.sow_ref, e.fee_type, e.ceiling_hours,
                e.ceiling_amount, e.start_date, e.end_date, e.status,
            )
            for e in self.engagements
        ]
        portfolio.sow_line_items = sow_rows
        portfolio.time_entries = [
            (
                self.entry_ids[e.seq], e.person_id, e.engagement_id, e.entry_date,
                units_to_hours(e.units), e.billable, e.note, e.submitted_at,
            )
            for e in self._ordered_entries
        ]
        portfolio.invoices = invoice_rows
        portfolio.mess = self.mess
        return portfolio


def generate(seed: int, period: str) -> Portfolio:
    """Build one reproducible portfolio.

    ``period`` is the demo month, YYYY-MM. History runs HISTORY_MONTHS back from it, and
    every mess case is anchored relative to it rather than to a hardcoded date.
    """
    return _Builder(seed, period).run()
