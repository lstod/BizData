-- Engagement health as a deterministic, versioned measurement.
--
--   psql "$BIZDATA_DSN" -f db/views/engagement_health_v1.sql
--
-- Requires db/seeds/scoring_weights.sql to have been applied first. scripts/seed.py
-- does both, in that order, after the generated tables are loaded — db/schema.sql drops
-- the six tables with CASCADE, which takes these views with them.
--
-- Three views, bottom-up:
--
--   portfolio_coverage_v1              one row per week, firm-wide
--   engagement_health_components_v1    one row per engagement per period per component
--   engagement_health_v1               one row per engagement per period
--
-- The line this file holds, and it is the point of putting the score in SQL at all: the
-- score and the band are a measurement. What to *do* about a score — RED versus NEEDS
-- REVIEW, who decides, what goes in front of a partner — is policy, and it lives in
-- plugin/skills/scope-escalation at step 8. Nothing below recommends anything.
--
-- Neither does anything below reconcile burn against margin. Mess case 4 is a fixed-fee
-- engagement at negative margin whose burn looks fine, and both components scoring it
-- independently is the correct behaviour. An agreement between them would be invented.


-- --------------------------------------------------------------------------------------
-- portfolio_coverage_v1
--
-- Whether a given week can be read at all, which is a different question from whether
-- the work went well. Mess case 7 empties a week firm-wide — an offsite, or the tracker
-- being down — and the naive read is that portfolio delivery collapsed.
--
-- This lives here rather than at step 3 because the run rate below has to exclude that
-- week, and get_time_summary's data_completeness block needs the same numbers. One
-- definition, two consumers.
--
-- Excluding an unreadable week from a run rate is a measurement correction: the data is
-- not there to average. It is not the step-6 rule about never calling that week a
-- delivery slowdown, which stays in assemble-delivery-pack.
-- --------------------------------------------------------------------------------------

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
    -- threshold in this file that is not data. It belongs to the reading of the period
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


-- --------------------------------------------------------------------------------------
-- engagement_health_components_v1
--
-- The long form: one row per engagement per period per component, carrying the raw
-- measurement, the risk it maps to, the weight that risk was given, and the points it
-- deducted. This is what turns "why is this one amber" into a list of numbers.
--
-- Each component_risk runs 0 to 1, where 0 is nothing wrong and 1 is as bad as that
-- component measures. A null risk means the component could not be measured for this
-- engagement in this period — no invoices yet, too few weeks to establish a run rate —
-- and null is deliberately not zero. Treating an unmeasurable component as no-risk makes
-- missing data read as good news, which is the failure mode mess case 3 exists to punish.
-- --------------------------------------------------------------------------------------

create or replace view engagement_health_components_v1 as
with model as (
    select version from scoring_model where is_active
),

bounds as (
    select
        date_trunc('month', min(entry_date))::date as first_period,
        date_trunc('month', max(entry_date))::date as last_period
    from time_entries
),

periods as (
    select
        p::date                                   as period_start,
        (p + interval '1 month - 1 day')::date     as period_end
    from bounds b
    cross join generate_series(
        b.first_period::timestamp,
        b.last_period::timestamp,
        interval '1 month'
    ) p
),

-- One row per engagement per month it was contractually live. as_of is where the
-- measurement stands: the period end, or the engagement's end date if that came first.
-- That least() is what stops mess case 6 — an engagement ending mid-period — from
-- reading as ten days of silence.
grid as (
    select
        e.id                                as engagement_id,
        e.client_id,
        e.fee_type,
        e.ceiling_hours,
        e.ceiling_amount,
        e.start_date,
        e.end_date,
        e.status,
        pr.period_start,
        pr.period_end,
        least(pr.period_end, e.end_date)    as as_of
    from engagements e
    join periods pr
      on e.start_date <= pr.period_end
     and e.end_date   >= pr.period_start
),

monthly as (
    select
        t.engagement_id,
        date_trunc('month', t.entry_date)::date                as period_start,
        sum(t.hours)                                           as hours,
        sum(t.hours * pe.cost_rate)                            as cost,
        sum(t.hours * pe.bill_rate) filter (where t.billable)  as billable_value,
        max(t.entry_date)                                      as last_entry_date
    from time_entries t
    join people pe on pe.id = t.person_id
    group by 1, 2
),

cumulative as (
    select
        g.engagement_id,
        g.period_start,
        coalesce(sum(m.hours), 0)           as hours_to_date,
        coalesce(sum(m.cost), 0)            as cost_to_date,
        coalesce(sum(m.billable_value), 0)  as billable_value_to_date,
        max(m.last_entry_date)              as last_entry_date
    from grid g
    left join monthly m
      on m.engagement_id = g.engagement_id
     and m.period_start <= g.period_start
    group by 1, 2
),

weekly as (
    select
        t.engagement_id,
        date_trunc('week', t.entry_date)::date  as week_start,
        sum(t.hours)                            as hours
    from time_entries t
    group by 1, 2
),

-- Materialized deliberately. Without the keyword Postgres inlines the CTE into the
-- lateral below and recomputes the whole coverage view once per engagement-month, which
-- is fifty-odd weeks of firm-wide aggregation done three hundred times and takes twenty
-- seconds. The window is about fifty rows; computing it once takes milliseconds. This
-- matters beyond tidiness — step 5 puts the tools behind an HTTP API that times out at
-- thirty seconds.
readable_weeks as materialized (
    select week_start, week_end
    from portfolio_coverage_v1
    where not firm_wide_gap
),

-- The trailing four readable, complete, in-contract weeks. Readable excludes mess case
-- 7's week; complete excludes the ragged part-week at a month end, which would otherwise
-- halve the run rate of every engagement in the portfolio; in-contract stops a week
-- before kickoff counting as a week of zero delivery.
--
-- weeks_counted comes out with it, because scope-escalation's rule is that a projection
-- built on fewer than three weeks is not a projection. Below three, burn_trajectory
-- scores null rather than guessing.
run_rate as (
    select
        g.engagement_id,
        g.period_start,
        avg(coalesce(wk.hours, 0))  as weekly_run_rate,
        count(*)                    as weeks_counted
    from grid g
    cross join lateral (
        select c.week_start
        from readable_weeks c
        where c.week_end   <= g.as_of
          and c.week_start >= g.start_date
          and c.week_start >  g.as_of - 70
        order by c.week_start desc
        limit 4
    ) rw
    left join weekly wk
      on wk.engagement_id = g.engagement_id
     and wk.week_start    = rw.week_start
    group by 1, 2
),

client_paid as (
    select
        e.client_id,
        i.paid_at,
        (i.paid_at - i.issued_at) as days_to_pay
    from invoices i
    join engagements e on e.id = i.engagement_id
    where i.status = 'paid'
),

-- DSO against the client's own history rather than a global threshold. A client that
-- always pays on day 45 is not a risk; one that used to pay on day 10 and now pays on
-- day 40 is. Ninety days of recent behaviour against the nine months before it.
dso as (
    select
        g.engagement_id,
        g.period_start,
        avg(cp.days_to_pay) filter (where cp.paid_at >  g.as_of - 90)  as dso_current,
        avg(cp.days_to_pay) filter (where cp.paid_at <= g.as_of - 90)  as dso_baseline
    from grid g
    left join client_paid cp
      on cp.client_id = g.client_id
     and cp.paid_at  <= g.as_of
     and cp.paid_at   > g.as_of - 365
    group by 1, 2
),

raw as (
    select
        g.engagement_id,
        g.period_start,
        g.period_end,
        g.fee_type,
        g.ceiling_hours,
        g.ceiling_amount,
        c.hours_to_date,
        c.cost_to_date,
        c.billable_value_to_date,
        rr.weeks_counted,

        -- Burn against the ceiling, projected forward at the trailing run rate for
        -- however much contract is left. Never re-baselined: if actuals have already
        -- passed the ceiling the projection stays above 1 and the denominator does not
        -- move.
        (c.hours_to_date
            + coalesce(rr.weekly_run_rate, 0)
              * greatest(0, (g.end_date - g.as_of))::numeric / 7.0
        ) / g.ceiling_hours as projected_burn_pct,

        -- Both definitions inherited from step 1 rather than re-decided here. Fixed fee
        -- takes the fee as revenue; T&M takes billable value at rate card. Mess case 4 is
        -- constructed against the first one and stops being a contradiction under any
        -- other.
        case
            when g.fee_type = 'fixed'
                then (g.ceiling_amount - c.cost_to_date) / g.ceiling_amount
            when c.billable_value_to_date > 0
                then (c.billable_value_to_date - c.cost_to_date) / c.billable_value_to_date
        end as margin_pct,

        -- Against the engagement's own start where nothing has ever been logged, so a
        -- live engagement that has never filed a timesheet is measured rather than
        -- excused.
        (g.as_of - coalesce(c.last_entry_date, g.start_date))::numeric as days_since_last_entry,

        case
            when d.dso_baseline > 0 and d.dso_current is not null
                then (d.dso_current - d.dso_baseline) / d.dso_baseline
        end as dso_change_pct
    from grid g
    left join cumulative c on c.engagement_id = g.engagement_id and c.period_start = g.period_start
    left join run_rate  rr on rr.engagement_id = g.engagement_id and rr.period_start = g.period_start
    left join dso        d on d.engagement_id  = g.engagement_id and d.period_start  = g.period_start
),

-- Each raw measurement mapped onto 0..1. The calibration constants are here rather than
-- in scoring_weights on purpose: they are part of what a component *means*, so changing
-- one changes the shape of the model and earns a v2 view. The weights, which trade the
-- four off against each other, are data.
scored as (
    select engagement_id, period_start, period_end,
           'burn_trajectory' as component,
           round(projected_burn_pct, 4) as raw_value,
           case when weeks_counted >= 3
                then least(1, greatest(0, (projected_burn_pct - 0.85) / 0.30))
           end as component_risk
    from raw

    union all
    select engagement_id, period_start, period_end,
           'margin',
           round(margin_pct, 4),
           case when margin_pct is not null
                then least(1, greatest(0, (0.40 - margin_pct) / 0.40))
           end
    from raw

    union all
    select engagement_id, period_start, period_end,
           'reporting_gap',
           round(days_since_last_entry, 4),
           -- Nothing for a week is ordinary. Twenty-one days is mess case 3, and
           -- scope-escalation's own line is fourteen consecutive days.
           least(1, greatest(0, (days_since_last_entry - 7) / 14))
    from raw

    union all
    select engagement_id, period_start, period_end,
           'payment_behaviour',
           round(dso_change_pct, 4),
           case when dso_change_pct is not null
                then least(1, greatest(0, dso_change_pct / 1.0))
           end
    from raw
),

weighted as (
    select
        s.*,
        w.weight,
        m.version
    from scored s
    cross join model m
    join scoring_weights w
      on w.version = m.version
     and w.component = s.component
),

-- Normalised over the weights of the components that could actually be measured, and
-- over their sum rather than an assumed 1.0. Both matter: the first stops a missing
-- signal reading as a healthy one, and the second is what makes a single UPDATE to one
-- weight a valid recalibration on its own.
normalised as (
    select
        weighted.*,
        case
            when component_risk is null then null
            else weight / nullif(
                sum(weight) filter (where component_risk is not null)
                    over (partition by engagement_id, period_start),
                0
            )
        end as weight_normalised
    from weighted
)

select
    engagement_id,
    period_start,
    period_end,
    component,
    raw_value,
    round(component_risk, 6)                                as component_risk,
    weight,
    round(weight_normalised, 6)                             as weight_normalised,
    round(100 * weight_normalised * component_risk, 4)      as contribution,
    version                                                 as scoring_model_version
from normalised;


-- --------------------------------------------------------------------------------------
-- engagement_health_v1
--
-- The headline row. One per engagement per period, with the component breakdown carried
-- alongside as jsonb so an amber engagement can be explained from a single select.
--
-- The view takes no arguments and needs none: it scores every month of the window, so
-- score_delta_vs_prior_period is a window function over the engagement's own history
-- rather than a second pass, and step 3's tools filter with
--
--     where period_start = date_trunc('month', :as_of_date)
-- --------------------------------------------------------------------------------------

create or replace view engagement_health_v1 as
with agg as (
    select
        engagement_id,
        period_start,
        period_end,
        scoring_model_version,
        round(100 - coalesce(sum(contribution), 0), 1)               as health_score,
        count(*) filter (where component_risk is not null)           as components_measured,
        count(*)                                                     as components_defined,
        (array_agg(component order by contribution desc, component)
            filter (where contribution > 0))[1]                      as top_risk_factor,
        jsonb_object_agg(
            component,
            jsonb_build_object(
                'raw_value',         raw_value,
                'risk',              component_risk,
                'weight',            weight,
                'weight_normalised', weight_normalised,
                'contribution',      contribution
            )
        )                                                            as components
    from engagement_health_components_v1
    group by 1, 2, 3, 4
)

select
    a.engagement_id,
    a.period_start,
    a.period_end,
    a.health_score,
    (
        select b.band
        from scoring_bands b
        where b.version = a.scoring_model_version
          and a.health_score >= b.min_score
        order by b.min_score desc
        limit 1
    ) as health_band,
    round(
        a.health_score - lag(a.health_score) over (
            partition by a.engagement_id order by a.period_start
        ),
        1
    ) as score_delta_vs_prior_period,
    a.top_risk_factor,
    a.components_measured,
    a.components_defined,
    a.components,
    a.scoring_model_version
from agg a;
