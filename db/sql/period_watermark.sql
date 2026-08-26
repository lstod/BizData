-- Where the data stood for one period, as two numbers a later run can compare against.
--
-- get_run_ledger asks this before anything else in a run: a watermark, which is the latest
-- moment anything the pack reads was filed, and a digest of the inputs themselves. The
-- watermark answers "what arrived since last time"; the digest answers "did any of it
-- matter". They are separate because they fail differently — an entry filed and then
-- corrected moves the digest without moving much else, and an entry filed for a month
-- nobody is reviewing moves the watermark and changes no figure at all.
--
-- **The scope is cumulative to period_end, not the period.** That looks wrong for a query
-- named after a period and it is the only defensible choice. engagement_burn_v1 computes
-- hours_to_date over everything up to as_of, and engagement_financials_v1 reads invoices
-- cumulatively and DSO back a year. So an entry backdated into *March* changes March's
-- hours, changes hours_to_date, and changes the burn percentage on August's deck. A digest
-- scoped to August would call that pack unchanged while the figures in it moved.
--
-- Three things here are cross-backend correctness rather than style, and all three are
-- silent when wrong. See docs/notes/step-12-schedule.md.
--
--   1. string_agg has no inherent order. Without `order by` it is stable in practice and
--      undefined by contract, which is the worst pair: seventeen seeds pass and the digest
--      moves in production.
--   2. Every date and timestamp goes through to_char with an all-numeric pattern rather
--      than `::text` or `||`. Those render through DateStyle, a session setting local
--      Postgres and Aurora arrive at by different routes — `set time zone` per connection
--      in server/db.py against a role-level GUC in db/seeds/mcp_readonly.sql, which
--      scripts/writers.py does not have because it connects as a different role. `at time
--      zone 'UTC'` first, for the same reason the late boundary is written as an explicit
--      instant in db/sql/get_time_summary_quality.sql.
--   3. A null anywhere in a concatenation nulls the whole element, so every nullable
--      column is coalesced to a marker that is not a legal value for it.
--
-- numeric goes through ::text and that is safe: numeric_out is locale-independent, always
-- a bare '.', and it renders the stored scale, so numeric(5,2) is always '7.50'. to_char
-- would be the riskier choice here, since its decimal-point pattern is not.
--
-- `note` is deliberately not in the digest. Nothing computes a figure from it, and a
-- corrected typo is not a reason to tell someone their pack is out of date.

with entries as (
    select
        t.id,
        t.person_id,
        t.engagement_id,
        t.entry_date,
        t.hours,
        t.billable,
        t.submitted_at
    from time_entries t
    where t.entry_date <= :period_end::date
),

billing as (
    select
        i.id,
        i.engagement_id,
        i.period,
        i.amount,
        i.status,
        i.issued_at,
        i.paid_at
    from invoices i
    where i.issued_at <= :period_end::date
)

select
    (select count(*) from entries)              as entries_scanned,
    (select count(*) from billing)              as invoices_scanned,

    -- Null on an empty period, which the caller reads as "nothing has ever been filed"
    -- rather than as an error. A first run against an empty window is a legitimate state.
    (select max(e.submitted_at) from entries e) as watermark,

    md5(
        coalesce((
            select string_agg(
                e.id::text
                    || '|' || e.person_id::text
                    || '|' || e.engagement_id::text
                    || '|' || to_char(e.entry_date, 'YYYY-MM-DD')
                    || '|' || e.hours::text
                    || '|' || coalesce(e.billable::text, '-')
                    || '|' || to_char(
                        e.submitted_at at time zone 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US'
                    ),
                ',' order by e.id
            )
            from entries e
        ), '')
        || '#'
        || coalesce((
            select string_agg(
                b.id::text
                    || '|' || b.engagement_id::text
                    || '|' || to_char(b.period, 'YYYY-MM-DD')
                    || '|' || b.amount::text
                    || '|' || b.status
                    || '|' || to_char(b.issued_at, 'YYYY-MM-DD')
                    || '|' || coalesce(to_char(b.paid_at, 'YYYY-MM-DD'), '-'),
                ',' order by b.id
            )
            from billing b
        ), '')
    )                                           as figures_digest,

    (select version from scoring_model where is_active) as scoring_model_version;
