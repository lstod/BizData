# BizData

A monthly delivery and margin review for a professional services firm, run by an agent
instead of by hand.

The process it replaces takes most of a day: pull time entries, update burn for every
active engagement, look properly at anything past 70%, rebuild the same eight slides
with different numbers, and flag whatever is going sideways before it becomes a
surprise. One person owns the spreadsheet and knows how it works.

This repository is the data layer and the MCP server behind that, plus the Skills that
assemble the pack. **Build in progress** — steps 0 through 2 of 14 are done. The
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
| `list_engagements` | Every engagement live in the period with its burn and health band. Triage in one call. |
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
  db.py                   local Postgres now, RDS Data API at step 5
  toollog.py              one JSON line per call, seven fields
  tools/                  the four read tools, one module each
infra/
  bootstrap/              Terraform state bucket and the budget alarm, local state
  main/                   everything else, S3 backend with native locking
scripts/
  seed.py                 --seed, --period; the entry point
  generator.py            the deterministic portfolio
  writers.py              local Postgres now, RDS Data API at step 5
  check_tools.py          the tools' Done-when conditions, as assertions
  sweep.sh                the SQL checks across every reserved seed
docker-compose.yml        local Postgres for steps 1 to 4
```

