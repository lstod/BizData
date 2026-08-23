# BizData

A monthly delivery and margin review for a professional services firm, run by an agent
instead of by hand.

The process it replaces takes most of a day: pull time entries, update burn for every
active engagement, look properly at anything past 70%, rebuild the same eight slides
with different numbers, and flag whatever is going sideways before it becomes a
surprise. One person owns the spreadsheet and knows how it works.

This repository is the data layer and the MCP server behind that, plus the Skills that
assemble the pack. **Build in progress** — steps 0 through 7 and 14 of 14 are done. The server is
deployed on AWS behind Cognito and answering tool calls from a Cowork connector; the Skills
produce the engagement book and the partner deck, to a format that does not vary. The
architecture write-up, the security posture and the demo land at step 11.

## All of the data here is synthetic

There is no real client, engagement, person or invoice in this repository, and there
never was one. Every row comes out of `scripts/seed.py`, which composes names from word
lists in `scripts/generator.py`. There is no real data to leak because none was ever loaded. This matters more than it usually does: the repository is public from day one, so synthetic only is load bearing rather than a preference.

## Running it locally

Requires Docker and Python 3.12. The Lambda runs 3.12, and the seed generator is only reproducible against a pinned interpreter, so the version is not incidental.

```bash
docker compose up -d                      # Postgres 16 on localhost:5433

python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python scripts/seed.py --seed 42 --period 2026-08
```

That creates the schema and loads a twelve-month synthetic consultancy ending in the
demo period, in about four seconds:


| Table            | Rows                   |
| ---------------- | ---------------------- |
| `clients`        | 12                     |
| `people`         | 25                     |
| `engagements`    | 30, of which 18 active |
| `sow_line_items` | ~120                   |
| `time_entries`   | 40,000                 |
| `invoices`       | ~190                   |


Forty thousand time entries is the number that matters. It is unarguably too many to hand to a model, so aggregating server side is not a design preference that needs defending, it is the only thing that works.

The database URL defaults to the compose one. Override it with `BIZDATA_DSN`, or pass
`--dsn`. `scripts/seed.py` drops and recreates every table unless you pass
`--no-schema`.

## Checking the data

```bash
export BIZDATA_DSN='postgresql://bizdata:bizdata@localhost:5433/bizdata'

psql "$BIZDATA_DSN" -f db/checks/mess_cases.sql   # eight rows, every count non-zero
psql "$BIZDATA_DSN" -f db/checks/checksums.sql    # row counts and content hashes
psql "$BIZDATA_DSN" -f db/checks/health_v1.sql    # seven rows, every ok true
```

Those three assert things about the data. To assert them across every reserved seed at once,
`scripts/sweep.sh /tmp/out` reseeds each in turn and writes one file per seed.

## Running the MCP server

```bash
.venv/bin/uvicorn server.app:app          # http://127.0.0.1:8000/mcp
```

Four read tools, one endpoint, no session state. Every call prints one JSON line to stdout — the
tool call log, seven fields, which is what project #2 reads and what makes "the client says the
number is wrong" a question with an answer.

```
{"run_id":"pack-2026-08","tool":"get_engagement_burn","arguments":{...},
 "total_count":1,"returned_count":1,"latency_ms":74.2,"scoring_model_version":"v1.0"}
```

| Tool | Answers |
| --- | --- |
| `list_engagements` | Every engagement live in the period with its burn, health band and key-person concentration. Triage in one call. |
| `get_engagement_burn` | Hours against the SOW ceiling, the trailing run rate, and a projection labelled with how far it can be trusted. |
| `get_time_summary` | Forty thousand entries aggregated, with data quality and weekly reporting coverage alongside. |
| `get_financials` | Invoiced, paid, unbilled work in progress, margin, and payment behaviour against the client's own history. |

The tools are checked the same way the data is, as assertions rather than description:

```bash
.venv/bin/python scripts/check_tools.py            # every reserved seed, reseeding each
.venv/bin/python scripts/check_tools.py --seed 42 -v
```

That runs the server in memory and asserts the mess cases surface through the tool surface, that
paging reaches `total_count`, that no response approaches the 1 MiB cap the Data API imposes at step
5, and that every call left exactly one complete log line. 177 assertions per seed.

The server is stateless, which is worth knowing before hand-writing a request to it: there is no
`initialize` handshake and no session id, and every request carries its own protocol version in
`params._meta`. Without that envelope the server answers `400`.

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{
       "io.modelcontextprotocol/protocolVersion":"2026-07-28",
       "io.modelcontextprotocol/clientCapabilities":{}}}}'
```

## Assembling the pack

`plugin/skills/assemble-delivery-pack/` is the first of three Skills. It fixes the order the
tools are called in, the rule for deciding which engagements get looked at properly, and how
`engagement-book-YYYY-MM.xlsx` gets written. The workbook builder it bundles contains no
arithmetic at all: every cell is either a field copied from a tool response or an Excel formula
over another tab, which is how "no figure in front of a partner was calculated by a model"
stays true all the way to the file on disk.

The rule worth reading is the coverage one. Before any burn figure is read, the Skill makes one
portfolio-wide `get_time_summary` call and checks `pct_active_reporting`. A week below 60%
coverage is a filing artifact — it is already excluded from every run rate in SQL, it is
reported as a data note, and it is never described as a delivery slowdown.

That rule exists because of a real miss. After the `ceiling_hours` fix the portfolio got
healthier, and a connector review filtering on `burn_pct > 70 OR health_band != green` examined
8 of 18 engagements and never surfaced three of the seeded mess cases. Two of them needed only
an order of operations. The third needed a column:

> A triage tool has to carry every dimension the triage rule is allowed to mention.

Key-person concentration lived only on `get_engagement_burn` — the call the filter was deciding
whether to make. So an engagement 85% delivered by one person, sitting at 67% burn in the green
band, was invisible to the thing choosing what to examine. It went unexamined on 15 of the 17
fixture seeds. `list_engagements` now carries it.

```bash
.venv/bin/python scripts/check_pack.py             # every reserved seed
.venv/bin/python scripts/check_pack.py --seed 42 -v --keep /tmp/pack
```

That follows the Skill's order of operations against the in-memory server, runs the bundled
builder with the command line the Skill prescribes, then re-reads the workbook off disk and
asserts on it — that paging reached `total_count`, that the examine set reaches the mess cases a
burn threshold cannot, that the run rates in the book are the tools' own figures, and that no
cell anywhere calls the low-coverage week a slowdown. 32 assertions per seed.

## The format is the deliverable

`plugin/skills/house-format/` is the second Skill, and it exists because a pack that looks
different every month is a pack nobody learns to read. It fixes the five tabs, the eight slides,
the number formats, and the voice — declarative, no adjectives on numbers.

Three columns on the `Engagements` tab are Excel formulas rather than values, so clicking a burn
cell shows `=IF(N(J4)>0,K4/J4,"")` and not `84.2%`. Margin branches on fee type, because the SQL
does and a workbook that disagrees with the health score is worse than one with no margin in it.

The deck is eight slides and a native chart, built from the same `pack.json` as the workbook, and
it may not contain a number the workbook does not.

```bash
.venv/bin/python scripts/check_format.py           # every reserved seed
.venv/bin/python scripts/check_format.py --seed 42 -v --keep /tmp/format
```

44 assertions per seed: that every row of each formula column holds a formula string and reads the
columns it claims to, that the conditional formatting tests red before amber, that the slides are
in order with data quality always last — including on a synthesised period with nothing to
report — and that every number on every slide traces back to the workbook or the pack behind it.

## Engagement health is a number, not a threshold in a prompt

Health is a deterministic score computed in SQL from a versioned set of weights, so the same inputs produce the same number every run and month over month comparison means something.

```sql
select engagement_id, health_score, health_band,
       score_delta_vs_prior_period, top_risk_factor, scoring_model_version
from engagement_health_v1
where period_start = '2026-08-01';
```

Four components: burn trajectory, margin, reporting gaps and payment behaviour each produce a risk between 0 and 1, which is weighted and deducted from 100. `engagement_health_components_v1` returns one row per component per engagement per month,
so when a partner asks why an engagement is amber the answer is a list of numbers rather
than an opinion. Engagement 9 on the demo seed scores 45.2, amber:


| Component           | Raw value      | Risk | Points deducted |
| ------------------- | -------------- | ---- | --------------- |
| `margin`            | -17.65%        | 1.00 | 30.0            |
| `burn_trajectory`   | 106% projected | 0.71 | 24.8            |
| `reporting_gap`     | 0 days         | 0.00 | 0.0             |
| `payment_behaviour` | -11% DSO       | 0.00 | 0.0             |


That engagement is mess case 4, a fixed-fee engagement losing money while its burn still looks healthy at 65%. Nothing in the view reconciles those two, because they are both true, and resolving the contradiction needs context the data does not contain.

**The weights are data.** They live in `scoring_weights`, band edges live in
`scoring_bands`, and `scoring_model` says which version is live. Changing a weight and
re-selecting moves every score with no code change and no redeploy, which is what turns a
judgment call into a measurable regression. `scripts/seed.py` applies both the scoring
model and the views after loading, because resetting the schema drops the views with the
tables they read.

A component that cannot be measured scores null rather than zero, and the remaining
weights renormalise. An engagement with no invoice history yet has no payment signal, and
treating that as good news would let missing data outscore real data.

What the score *means for what happens next* is deliberately not here. RED versus NEEDS
REVIEW, and who decides, is policy and lives in the `scope-escalation` Skill. The
measurement and the policy are different things, and keeping them apart is the point.

## The deliberate mess

The portfolio has eight problems seeded into it on purpose, each one mapped to a rule
the agent has to follow rather than decoration:


| #   | Case                                               | What it exists to exercise                                  |
| --- | -------------------------------------------------- | ----------------------------------------------------------- |
| 1   | Time filed after the period closed                 | `late_entries`, and the confidence threshold on projections |
| 2   | One duplicated entry — same person, day and hours  | `suspected_duplicates`                                      |
| 3   | One engagement silent for three weeks              | Never infer completion from absent data                     |
| 4   | Fixed fee at negative margin, burn looking healthy | State the contradiction, do not resolve it                  |
| 5   | Two entries with no `billable` flag                | `null_billable`                                             |
| 6   | One engagement ending mid-period                   | Period boundaries and days remaining                        |
| 7   | One week under 20% reporting coverage              | Exclude it from run rates; never call it a slowdown         |
| 8   | One engagement 85% delivered by one person         | Key-person continuity risk                                  |


Case 7 is the interesting one. The naive read of that week is that portfolio delivery collapsed. It was a firm wide offsite, and treating it as a delivery signal would put a wrong number in front of a partner.

`db/checks/mess_cases.sql` asserts all eight and is what proves they survived a change
to the generator.

## Reproducibility, and the reserved seeds

The same seed and period produce byte-identical tables. A different seed produces a
different but equally reproducible consultancy, with all eight mess cases present on
different engagements.

**Fifteen seeds are reserved as a fixture set: 9001 through 9015.** They exist so a scoring harness can run the same task across fifteen distinct portfolios and compare results between runs. Do not use them as demo or example seeds, the demo period uses seed 42, and 43 is the worked example of a second portfolio. All seventeen are verified to produce eight non-zero mess-case assertions.

## Layout

```
db/
  schema.sql              six tables
  seeds/                  the scoring model: weights, bands, active version
  views/                  coverage, burn, financials, health -- each reads the one above
  sql/                    one query per tool, :name placeholders, Data API form
  checks/                 mess case assertions, determinism checksums, health assertions
server/
  app.py                  the MCP server; uvicorn server.app:app
  db.py                   local Postgres, or the RDS Data API
  auth.py                 Cognito token verification, ~60 lines
  toollog.py              one JSON line per call, seven fields
  tools/                  the four read tools, one module each
plugin/
  skills/                 the Skills, one directory each
    assemble-delivery-pack/
      SKILL.md            order of operations, the coverage rule, stop conditions
      scripts/            build_workbook.py: five tabs, no arithmetic
    house-format/
      SKILL.md            the tabs, the slides, the formulas, the voice
      scripts/            build_deck.py: eight slides, no arithmetic
      assets/             self-check.md, run against the files before shipping
infra/
  bootstrap/              Terraform state bucket and the budget alarm, local state
  main/                   everything else, S3 backend with native locking
scripts/
  seed.py                 --seed, --period; the entry point
  generator.py            the deterministic portfolio
  writers.py              local Postgres now, RDS Data API at step 5
  check_tools.py          the tools' Done-when conditions, as assertions
  check_pack.py           the pack's, asserted against the workbook on disk
  check_format.py         the house format's, against the workbook and the deck
  sweep.sh                the SQL checks across every reserved seed
  package_skill.sh        zip a Skill directory for upload, without the macOS cruft
docker-compose.yml        local Postgres for steps 1 to 4
```

