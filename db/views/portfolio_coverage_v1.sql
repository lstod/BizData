-- Whether a given week can be read at all, which is a different question from whether
-- the work went well.
--
--   psql "$BIZDATA_DSN" -f db/views/portfolio_coverage_v1.sql
--
-- The first view in the chain, and the one everything else is filtered by:
--
--   portfolio_coverage_v1              one row per week, firm-wide      <- here
--   engagement_burn_v1                 engagement x period, measured
--   engagement_financials_v1           engagement x period, billed
--   engagement_health_components_v1    engagement x period x component
--   engagement_health_v1               engagement x period
--
-- Mess case 7 empties a week firm-wide — an offsite, or the tracker being down — and the
-- naive read is that portfolio delivery collapsed. Every trailing run rate in
-- engagement_burn_v1 excludes the weeks this view flags, and get_time_summary's
-- data_completeness block is these same numbers. One definition, two consumers.
--
-- Excluding an unreadable week from a run rate is a measurement correction: the data is
-- not there to average. It is not the step-6 rule about never calling that week a
-- delivery slowdown, which stays in assemble-delivery-pack.
--
-- Written at step 2 inside engagement_health_v1.sql and moved to its own file at step 3,
-- unchanged, when engagement_burn_v1 became a second consumer and the load order had to
-- put this first.

create or replace view portfolio_coverage_v1 as
with bounds as (
    select min(entry_date) as first_day, max(entry_date) as last_day
    from time_entries
),

weeks as (
    select
        w::date                          as week_start,
        (w + interval '6 days')::date    as week_end
    from bounds b
    cross join generate_series(
        date_trunc('week', b.first_day::timestamp),
        date_trunc('week', b.last_day::timestamp),
        interval '1 week'
    ) w
),

-- The denominator is the engagements that were contractually live that week, not a flat
-- count of everything marked active. An engagement that had not started yet cannot fail
-- to report, and counting it would make the opening weeks of the window look like
-- coverage gaps and drag every run rate that reads them.
live as (
    select k.week_start, count(*) as engagements_active
    from weeks k
    join engagements e
      on e.status = 'active'
     and e.start_date <= k.week_end
     and e.end_date   >= k.week_start
    group by k.week_start
),

reporting as (
    select k.week_start, count(distinct t.engagement_id) as engagements_reporting
    from weeks k
    join time_entries t on t.entry_date between k.week_start and k.week_end
    join engagements e  on e.id = t.engagement_id and e.status = 'active'
    group by k.week_start
)

select
    k.week_start,
    k.week_end,
    coalesce(l.engagements_active, 0)     as engagements_active,
    coalesce(r.engagements_reporting, 0)  as engagements_reporting,
    case
        when coalesce(l.engagements_active, 0) = 0 then null
        else round(100.0 * coalesce(r.engagements_reporting, 0) / l.engagements_active, 1)
    end as pct_active_reporting,

    -- The 60% floor is the coverage rule from the decision record, and it is the one
    -- threshold in this chain that is not data. It belongs to the reading of the period
    -- rather than to the calibration of the score: a different weighting of burn against
    -- margin is a judgment call, whereas a week nobody filed against is unreadable at any
    -- weighting.
    case
        when coalesce(l.engagements_active, 0) = 0 then false
        else coalesce(r.engagements_reporting, 0)::numeric / l.engagements_active < 0.60
    end as firm_wide_gap
from weeks k
left join live      l on l.week_start = k.week_start
left join reporting r on r.week_start = k.week_start;
