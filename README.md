# BizData

A monthly delivery and margin review for a professional services firm, run by an agent
instead of by hand.

The process it replaces takes most of a day: pull time entries, update burn for every
active engagement, look properly at anything past 70%, rebuild the same eight slides
with different numbers, and flag whatever is going sideways before it becomes a
surprise. One person owns the spreadsheet and knows how it works.

This repository is the data layer and the MCP server behind that, plus the Skills that
assemble the pack. **All 14 steps are done.** The server is
deployed on AWS behind Cognito and answering tool calls from a Cowork connector; the Skills
produce the engagement book and the partner deck, to a format that does not vary; a finished pack
lands in an S3 archive of record with its tool-call log and ledger beside it; a run ledger makes
re-running a period a decision rather than an accident; the engagements chosen for a proper look
are examined in parallel; and the whole thing installs as a plugin from this repository, which is
its own marketplace.

The security posture is in [SECURITY.md](SECURITY.md). What went wrong is further down, under
[what broke](#what-broke), and it is the part worth reading.

## All of the data here is synthetic

There is no real client, engagement, person or invoice in this repository, and there
never was one. Every row comes out of `scripts/seed.py`, which composes names from word
lists in `scripts/generator.py`. There is no real data to leak because none was ever loaded. This matters more than it usually does: the repository is public from day one, so synthetic only is load bearing rather than a preference.

## How it fits together

Cowork holds three Skills and calls a remote MCP server over OAuth. The server runs on Lambda,
reads Aurora over the RDS Data API, and returns aggregates. The Skills build the workbook and the
deck from those responses and publish the pack to an S3 archive of record.

```
                 three Skills
Cowork ──────────────┬──────────── OAuth ────▶ API Gateway ──▶ Lambda ──Data API──▶ Aurora
  │                                                              │
  │                                              one JSON line per call ──▶ CloudWatch
  │
  └── the pack ──── presigned PUT ────▶ S3, beside its tool-call log and ledger entry
```

Seven decisions, and what each one is not:

| Decision | Rather than | Why |
| --- | --- | --- |
| Aggregate in SQL and return rows | Return time entries and let the model total them | 40,000 entries is unarguably too many to hand to a model. This is not a preference to defend, it is the only thing that works — and it is what keeps "no figure in front of a partner was calculated by a model" true |
| Aurora Serverless v2 at `MinCapacity 0`, reached over the Data API | A pooled connection from inside the VPC | The Data API is an HTTPS endpoint reached with IAM, so the Lambda needs no subnet, no ENI, no security group and **no NAT gateway** — about $32/month billed hourly whether or not anything flows through it. The price is per-call latency, discussed below |
| Lambda behind the Web Adapter | A long-running container | The same ASGI app runs under `uvicorn` locally and on Lambda deployed, with no code change. A review that runs monthly should not be paying for a container the other 30 days |
| Stateless MCP | Session affinity | Stateless since the `2026-07-28` revision, so any request can land on any cold instance. Scale-to-zero is the natural shape now, not a workaround |
| Health as a score computed in SQL from a versioned weights table | Thresholds in a prompt | Changing a weight and re-running the harness turns a judgment call into a measurable regression. Weights are data, so it needs no redeploy |
| One triage call carrying every dimension the triage rule may mention | A fan-out of detail calls | Triage costs one call instead of thirty. This one was learned three times — see [what broke](#what-broke). It is a rule about *choosing* what to examine: the engagements triage has already chosen are then examined in parallel, which is step 13 and a different question |
| Read-only enforced by a database grant | Read-only enforced by the server's code | The server could be rewritten tomorrow to issue an `UPDATE` and it would still fail. `scripts/bootstrap_aurora.py` attempts a write on every run and records the refusal, so a change that widens the grant breaks the bootstrap rather than passing quietly |

Two backends sit behind one interface in `server/db.py`, chosen by `BIZDATA_DB_BACKEND`: `psycopg`
locally, the Data API deployed. Every query is text in `db/sql/` with named parameters and each
adapter binds them its own way. That seam is why deploying at step 5 was a deployment and not a
rewrite, and it is why seventeen fixture seeds can be swept on a laptop with no AWS account.

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

Four, and the number is a constraint rather than a coincidence: it is the read surface, and it is
what a caller has to choose between when deciding what to look at. Two more tools bracket a *run*
without joining it. `publish_pack` closes one out — the only write in the system. `get_run_ledger`
opens one, and answers a different question from any of the four above:

| Tool | Answers |
| --- | --- |
| `get_run_ledger` | Has this period already been published, and has anything moved since. Returns `first_run`, `unchanged` or `changed`, and names the entries filed since the last run. |

That last one is what makes running the review on a schedule safe. Without it, a monthly job either
rebuilds a period every time it fires — producing a second pack with the same figures under a new
name — or it never notices the timesheet that arrived three days late. The ledger it reads is a
per-period object in the same S3 archive the packs land in, not a database table: the server holds
a `SELECT`-only credential and step 12 declined to widen it. `docs/notes/step-12-schedule.md` has
the argument, including the two things that went wrong while building it.

The tools are checked the same way the data is, as assertions rather than description:

```bash
.venv/bin/python scripts/check_tools.py            # every reserved seed, reseeding each
.venv/bin/python scripts/check_tools.py --seed 42 -v
```

That runs the server in memory and asserts the mess cases surface through the tool surface, that
paging reaches `total_count`, that no response approaches the 1 MiB cap the Data API imposes at step
5, and that every call left exactly one complete log line. 234 assertions per seed.

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

## Installing it as a plugin

The three Skills and the remote connector are one plugin. This repository is also its own
marketplace, so no separate distribution step exists: in Cowork, open **Customize → Plugins**,
add the marketplace `lstod/BizData`, and install **BizData delivery review**. In the CLI:

```bash
claude plugin marketplace add lstod/BizData
claude plugin install bizdata-delivery-review@bizdata
```

For anywhere a marketplace cannot be added, `scripts/package_plugin.sh` builds an uploadable
`build/bizdata-delivery-review.zip`.

`plugin/.mcp.json` declares the connector as a remote HTTP server with its OAuth client id,
callback port, scopes and discovery URL — all of which a real client honours, without attempting
the dynamic client registration Cognito does not support. What it cannot declare is the **client
secret**, because the manifest format has no field for one, deliberately: secrets belong in the
OS keychain and not in a file that gets committed. This build uses a confidential Cognito client,
so the connector needs that one value supplied out of band at install time. The Skills need
nothing.

That is the whole of the limitation, and it is measured rather than assumed —
`docs/notes/plugin-packaging.md` has the token-endpoint evidence isolating it to client
authentication and nothing else, along with what closing it would cost.

`scripts/check_plugin.py` asserts all of this on every run: 46 assertions covering the manifests,
the bundled scripts, the package hygiene, that no credential ships, and that the endpoint and
client id in the manifest are still the ones deployed. `--offline` drops the six needing AWS.

```bash
python scripts/check_plugin.py            # requires Claude Code v2.x for two of them
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

51 assertions per seed: that every row of each formula column holds a formula string and reads the
columns it claims to, that the conditional formatting tests red before amber, that the slides are
in order with data quality always last — including on a synthesised period with nothing to
report — and that every number on every slide traces back to the workbook or the pack behind it.

Five of those assert against a pack with `scoring_model_version` nested under `time_summary` and
absent from the top level, which is the shape a run following the Skill actually produces. The
fixture is made worse on purpose, because the worse fixture is the honest one.

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

## What broke

Three, and the third one broke three times.

### The connector refused a laptop, one step earlier than the plan expected

The written prediction was that the request would leave Anthropic's cloud, look for
`127.0.0.1:8000`, and find its own loopback. It never got that far. Cowork's connector form
rejects the URL on the **scheme**, client-side:

> URL must start with 'https'

The **Add** button never enables and nothing is sent. That is checkable rather than plausible,
which is the point: the server was proved answering first, and across the whole exercise uvicorn's
access log holds exactly three lines — all of them that preflight. No fourth line, no connection
attempt, no TLS handshake.

So there are two independent reasons a laptop cannot be a connector, and the one you actually hit
is the cheaper one to explain: a loopback address has no certificate, so there is no `https` URL to
give it. The mechanism the plan named may well be true and this build cannot claim to have
demonstrated it. Screenshot: `docs/evidence/step-4-connector-failure.png`.

### There was nowhere to put a bearer token, which cost a day's plan

The same fifteen minutes found the more expensive thing. The form offers `OAuth Client ID
(optional)`, `OAuth Client Secret (optional)`, and nothing else — no API key box, no custom
headers. The build's auth sequence was "bearer token end to end first, then Cognito later," and a
bearer-protected endpoint is one Cowork **cannot connect to at all**. Real OAuth had gone from a
day's work sitting behind five droppable steps to load-bearing for the deployment gate.

It was pulled forward, sequenced so the risk ordering survived anyway: applied first with
`enable_auth = false`, proved with `curl`, banked in a commit, then Cognito applied on top. And the
day collapsed to about sixty lines, because **the MCP v2 SDK is already a resource server** —
`mcp.server.auth` ships the 401, the `WWW-Authenticate` header, RFC 9728 protected resource
metadata and scope enforcement. Recognising that is most of the value. The alternative, and it is a
common one, is hand-rolling an authorization facade in front of a server that already had one.

The only genuinely Cognito-specific knowledge in the whole step is worth stating, because it
produces broken integrations reliably. **A Cognito access token carries `client_id` and no `aud`;
an ID token carries `aud` and no `client_id`.** So validating `aud` on an access token fails
against a token that is perfectly valid, the usual response is `options={"verify_aud": False}`, and
that removes the audience check while leaving nothing in its place. The fix is to check
`client_id` — not to check nothing. `server/auth.py` also checks `token_use`, because Cognito signs
ID tokens and access tokens with the same keys from the same pool, and omitting that check breaks
nothing visible.

### The triage tool did not carry what the triage rule needed, three separate times

`list_engagements` first returned engagement metadata only. Deciding which engagements deserved a
proper look therefore cost a fan-out of thirty `get_engagement_burn` calls — the tool answered
"which engagements exist," when the question was "which ones should I look at." `burn_pct` and the
health band moved into SQL and onto the triage row, and triage went from thirty calls to one.

Step 13 later added a fan-out of detail calls, which sounds like a reversal and is not: it examines
the engagements triage has *already chosen*, in parallel, and triage is still one call. The rule
underneath both is the same — never call a tool to learn something the triage row already carries.
`docs/notes/step-13-fanout.md` draws the line; `plugin/skills/assemble-delivery-pack/SKILL.md` step
4a states it where a future edit would otherwise read the new section as permission.

Then the same mistake, twice more, in a form that was harder to see:

- **Step 6.** An engagement 85% delivered by one person, sitting at 67% burn in the green band, was
  invisible to a filter of `burn_pct > 70 OR health_band != green`. Person concentration lived only
  on `get_engagement_burn` — the call the filter was deciding whether to make. It went unexamined
  on **15 of 17** fixture seeds.
- **Step 8.** Same shape again for an engagement silent for three weeks and a fixed-fee engagement
  under water, both sitting green and under 70% burn on **6 of 17** seeds.

The rule was written down the first time and had to be learned three times:

> A filter can only fire on what the triage row carries.

`list_engagements` now carries six triggerable columns and the examine rule fires on all six.
`check_pack.py` asserts that the examine set reaches the mess cases a burn threshold cannot, which
is the assertion that would have caught all three.

There is a fourth, smaller, in the same family and it is written up in
`docs/evidence/step-10-cowork-run.txt` §5: the deck rendered `Scoring model n/a` on a title slide
while the workbook rendered the version correctly, because the two builders disagreed about where
in `pack.json` to find it and the harness synthesized its fixture in a shape the documented process
never produces. 427 assertions a seed passed against a pack no real run would build. The harness
agreed with itself.

## Three questions this design invites

Stated here because they are the right questions, and the honest answers are more useful than a
README that pretends the trade-offs are not trade-offs.

**The Data API adds latency to every call.** It does — `list_engagements` returns in about 1.1s
warm against roughly 200ms locally, because every call is an HTTPS round trip with IAM auth rather
than a checked-out connection. At production volume the answer is pooled connections through **RDS
Proxy inside the VPC**, and the price is precisely the networking this design avoids: subnets,
ENIs, a security group, and a NAT gateway at about $32/month billed hourly. For eighteen
engagements reviewed once a month, per-call latency is the cheaper side of that trade. For a
thousand engagements queried on demand it is not, and the migration is a `server/db.py` adapter
rather than a rewrite — which is the reason that seam exists.

**Seeding at real scale is not forty `BatchExecuteStatement` calls.** 40,000 rows over the Data API
in batches of 1,000 takes **34.6s**. The same seed into local Postgres through `COPY` takes
**0.7s** — a factor of about fifty. That is fine at this size and would not be at ten times it. The
scale answer is a CSV in S3 and `aws_s3.table_import_from_s3`, and it is deliberately not built
here, because the cost is only paid when someone reseeds and the honest number is more useful than
a code path nothing exercises.

**MCP on Lambda used to mean fighting session affinity.** The old objection was that `initialize`
establishes a session and a scale-to-zero function loses it. That stopped being true at the
`2026-07-28` revision: the protocol is stateless, there is no handshake and no session id, and
every request carries its own protocol version in `params._meta`. Any request can land on any cold
instance. Scale-to-zero is the natural shape for a workload that runs monthly, not a compromise
around one.

One measured caveat, because it is the kind of thing that bites later. Cowork sends **two protocol
revisions from one client** — `2025-11-25` and `2026-07-28`, interleaved, plus some requests
carrying no version at all. A server strict about a single revision would not fail at registration,
which is where you would look. It would fail intermittently.

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
  archive.py              a directory, or the S3 bucket the runs land in
  runlog.py               an in-process buffer, or CloudWatch Logs
  tools/                  the four read tools, plus get_run_ledger and publish_pack
.claude-plugin/
  marketplace.json        this repository, acting as its own plugin marketplace
plugin/                   the plugin root; installs as a unit
  .claude-plugin/
    plugin.json           name, version, description -- the namespace for the Skills
  .mcp.json               the remote connector: url, and OAuth minus the secret
  skills/                 the Skills, one directory each
    assemble-delivery-pack/
      SKILL.md            order of operations, the coverage rule, stop conditions
      scripts/            build_workbook.py: five tabs, no arithmetic
    house-format/
      SKILL.md            the tabs, the slides, the formulas, the voice
      scripts/            build_deck.py: eight slides, no arithmetic
      assets/             self-check.md, run against the files before shipping
    scope-escalation/
      SKILL.md            the RED list, NEEDS REVIEW, and what never to decide
      scripts/            classify.py: the flags, but not the judgment
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
  check_escalation.py     the escalation policy's, against the Exceptions tab
  check_publish.py        the archive's, against a directory standing in for the bucket
  check_ledger.py         the run ledger's; the one harness that writes to the database
  check_archive.py        the archive's, against the real bucket over HTTPS
  check_plugin.py         the plugin's: manifests, bundle, currency, no credential shipped
  sweep.sh                the SQL checks across every reserved seed
  package_skill.sh        zip one Skill for upload, without the macOS cruft
  package_plugin.sh       zip the whole plugin, for the upload install path
docker-compose.yml        local Postgres for steps 1 to 4
```

