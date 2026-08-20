-- Everything measurable about one engagement in one period from its own time entries.
--
--   psql "$BIZDATA_DSN" -f db/views/engagement_burn_v1.sql
--
-- Requires portfolio_coverage_v1. scripts/seed.py applies the whole chain in order.
--
-- This view exists because three consumers need the same numbers and two of them are
-- new at step 3. get_engagement_burn returns the trailing run rate and the projection;
-- engagement_health_components_v1 scores burn_trajectory from the same projection; and
-- the workbook at step 7 prints both. Computing the run rate twice would give the tool's
-- number and the score's number two definitions that can silently drift apart, and an
-- engagement whose burn tile and health band disagree is worse than either being wrong.
--
-- So the grid, the cumulative totals and the trailing run rate moved here out of
-- engagement_health_components_v1, which now reads them back. The move is behaviour
-- preserving by construction — the expressions below are the step-2 expressions, not
-- rewrites of them — and it is proved rather than asserted: scripts/sweep.sh runs
-- mess_cases, checksums and health_v1 across all seventeen fixture seeds, and the output
-- is byte-identical either side of the change.
--
-- Naming: a column ending _ratio runs 0 to 1. Nothing in this file is a percentage.
-- The tools scale to 0..100 in db/sql/ and name those columns _pct, because every
-- threshold the Skills apply — burn_pct > 70, margin_pct < 15, projected_overrun_pct >
-- 10, person_concentration_pct > 70 — is written on the 0..100 scale. One conversion,
-- in SQL, at the edge.
--
-- Nothing here recommends anything. Mess case 4 is a fixed-fee engagement at negative
-- margin whose burn looks fine; margin_ratio and burn_ratio both report that faithfully
-- and neither is reconciled against the other, because an agreement between them would
-- be invented. What to do about it is scope-escalation, at step 8.

create or replace view engagement_burn_v1 as
with bounds as (
    select
        date_trunc('month', min(entry_date))::date as first_period,
        date_trunc('month', max(entry_date))::date as last_period
    from time_entries
),

periods as (
    select
        p::date                                    as period_start,
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
        sum(t.hours) filter (where t.billable)                 as billable_hours,
        sum(t.hours * pe.cost_rate)                            as cost,
        sum(t.hours * pe.bill_rate) filter (where t.billable)  as billable_value,

        -- Every hour at rate card, billable or not. The denominator of realisation:
        -- what the work would have been worth if all of it went on an invoice.
        sum(t.hours * pe.bill_rate)                            as value_at_rate_card,
        max(t.entry_date)                                      as last_entry_date
    from time_entries t
    join people pe on pe.id = t.person_id
    group by 1, 2
),

cumulative as (
    select
        g.engagement_id,
        g.period_start,
        coalesce(sum(m.hours), 0)               as hours_to_date,
        coalesce(sum(m.billable_hours), 0)      as billable_hours_to_date,
        coalesce(sum(m.cost), 0)                as cost_to_date,
        coalesce(sum(m.billable_value), 0)      as billable_value_to_date,
        coalesce(sum(m.value_at_rate_card), 0)  as value_at_rate_card_to_date,
        max(m.last_entry_date)                  as last_entry_date
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
-- laterals below and recomputes the whole coverage view once per engagement-month, which
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
-- scores null rather than guessing and projection_confidence drops to low.
--
-- weeks_with_no_hours is the step-3 addition: a readable week in which this engagement
-- logged nothing. Readable means the firm was filing that week, so the absence is the
-- engagement's own, which is the gap the spec drops projection_confidence for.
run_rate as (
    select
        g.engagement_id,
        g.period_start,
        avg(coalesce(wk.hours, 0))               as weekly_run_rate,
        count(*)                                 as weeks_counted,
        count(*) filter (where wk.hours is null) as weeks_with_no_hours
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

-- The eight readable weeks behind those four, which is what the current run rate is
-- compared against. Behaviour against the engagement's own baseline rather than a
-- portfolio average: a team that has always run at ten hours a week is not accelerating,
-- and one that has just gone from ten to thirty is, whatever the portfolio is doing.
--
-- No 70-day cap here, unlike the window above. The cap exists to stop a stale run rate
-- being presented as current, which is exactly not the job of a baseline.
baseline_rate as (
    select
        g.engagement_id,
        g.period_start,
        avg(coalesce(wk.hours, 0))  as baseline_weekly_run_rate,
        count(*)                    as baseline_weeks_counted
    from grid g
    cross join lateral (
        select c.week_start
        from readable_weeks c
        where c.week_end   <= g.as_of
          and c.week_start >= g.start_date
        order by c.week_start desc
        offset 4
        limit 8
    ) rw
    left join weekly wk
      on wk.engagement_id = g.engagement_id
     and wk.week_start    = rw.week_start
    group by 1, 2
),

-- Key-person concentration, mess case 8. Within the period rather than to date, because
-- continuity risk is about who is on it now.
period_person as (
    select
        g.engagement_id,
        g.period_start,
        t.person_id,
        sum(t.hours) as hours
    from grid g
    join time_entries t
      on t.engagement_id = g.engagement_id
     and t.entry_date between g.period_start and g.period_end
    group by 1, 2, 3
),

concentration as (
    select
        engagement_id,
        period_start,
        sum(hours)                          as period_hours,
        count(*)                            as people_count,
        max(hours) / nullif(sum(hours), 0)  as person_concentration_ratio
    from period_person
    group by 1, 2
),

-- Mess case 1. The boundary is the instant the period closed, written as an explicit UTC
-- timestamp rather than a bare cast so the answer does not depend on the session
-- timezone of whoever is connected — the tools run over HTTP and set nothing.
-- db/checks/mess_cases.sql computes the same instant under `set timezone = 'UTC'`.
late as (
    select
        g.engagement_id,
        g.period_start,
        count(*)                                     as period_entry_count,
        count(*) filter (
            where t.submitted_at >= ((g.period_end + 1)::timestamp at time zone 'UTC')
        )                                            as late_entry_count
    from grid g
    join time_entries t
      on t.engagement_id = g.engagement_id
     and t.entry_date between g.period_start and g.period_end
    group by 1, 2
),

joined as (
    select
        g.engagement_id,
        g.client_id,
        g.period_start,
        g.period_end,
        g.as_of,
        g.fee_type,
        g.ceiling_hours,
        g.ceiling_amount,
        g.start_date,
        g.end_date,
        g.status,

        c.hours_to_date,
        c.billable_hours_to_date,
        c.cost_to_date,
        c.billable_value_to_date,
        c.value_at_rate_card_to_date,
        c.last_entry_date,

        rr.weekly_run_rate,
        rr.weeks_counted,
        rr.weeks_with_no_hours,
        br.baseline_weekly_run_rate,
        br.baseline_weeks_counted,

        cn.period_hours,
        cn.people_count,
        cn.person_concentration_ratio,

        coalesce(l.period_entry_count, 0)  as period_entry_count,
        coalesce(l.late_entry_count, 0)    as late_entry_count,
        case
            when coalesce(l.period_entry_count, 0) > 0
                then l.late_entry_count::numeric / l.period_entry_count
        end as late_entry_ratio,

        -- Projected forward at the trailing run rate for however much contract is left.
        -- Never re-baselined: if actuals have already passed the ceiling the projection
        -- stays above the ceiling and the denominator does not move.
        c.hours_to_date
            + coalesce(rr.weekly_run_rate, 0)
              * greatest(0, (g.end_date - g.as_of))::numeric / 7.0
        as projected_total_hours
    from grid g
    left join cumulative     c  on c.engagement_id  = g.engagement_id and c.period_start  = g.period_start
    left join run_rate       rr on rr.engagement_id = g.engagement_id and rr.period_start = g.period_start
    left join baseline_rate  br on br.engagement_id = g.engagement_id and br.period_start = g.period_start
    left join concentration  cn on cn.engagement_id = g.engagement_id and cn.period_start = g.period_start
    left join late           l  on l.engagement_id  = g.engagement_id and l.period_start  = g.period_start
),

-- Confidence is the tool's job, not the model's. A projection the agent should not trust
-- has to be labelled by the data layer, with the reason attached, or the labelling
-- becomes a thing a prompt might forget to do.
confidence as (
    select
        j.*,
        array_remove(array[
            case when coalesce(j.weeks_counted, 0) < 3 then format(
                'the run rate rests on %s readable week(s), fewer than the three a projection needs',
                coalesce(j.weeks_counted, 0)
            ) end,
            case when coalesce(j.weeks_with_no_hours, 0) > 0 then format(
                '%s of the trailing %s readable weeks have no time logged against this engagement',
                j.weeks_with_no_hours, j.weeks_counted
            ) end,
            case when j.late_entry_ratio > 0.10 then format(
                '%s%% of the period''s entries were filed after the period closed',
                round(100 * j.late_entry_ratio, 1)
            ) end
        ], null) as confidence_reasons
    from joined j
)

select
    engagement_id,
    client_id,
    period_start,
    period_end,
    as_of,
    fee_type,
    status,
    start_date,
    end_date,
    ceiling_hours,
    ceiling_amount,

    hours_to_date,
    billable_hours_to_date,
    cost_to_date,
    billable_value_to_date,
    value_at_rate_card_to_date,
    last_entry_date,

    hours_to_date / ceiling_hours                   as burn_ratio,

    -- Negative when the ceiling has already been passed, and left negative on purpose.
    -- The one thing scope-escalation may never do is move this denominator.
    ceiling_hours - hours_to_date                   as hours_remaining,
    greatest(0, end_date - as_of)                   as days_remaining,
    (as_of - coalesce(last_entry_date, start_date))::numeric as days_since_last_entry,

    weekly_run_rate                                 as weekly_run_rate_4wk,
    weeks_counted,
    weeks_with_no_hours,
    baseline_weekly_run_rate,
    baseline_weeks_counted,
    case
        when baseline_weekly_run_rate > 0
            then (weekly_run_rate - baseline_weekly_run_rate) / baseline_weekly_run_rate
    end                                             as run_rate_vs_baseline_ratio,

    projected_total_hours,
    projected_total_hours / ceiling_hours           as projected_burn_ratio,
    greatest(0, projected_total_hours - ceiling_hours) / ceiling_hours
                                                    as projected_overrun_ratio,

    -- Both definitions inherited from step 1 rather than re-decided here. Fixed fee
    -- takes the fee as revenue; T&M takes billable value at rate card. Mess case 4 is
    -- constructed against the first one and stops being a contradiction under any other.
    case
        when fee_type = 'fixed'
            then (ceiling_amount - cost_to_date) / ceiling_amount
        when billable_value_to_date > 0
            then (billable_value_to_date - cost_to_date) / billable_value_to_date
    end                                             as margin_ratio,

    case
        when value_at_rate_card_to_date > 0
            then billable_value_to_date / value_at_rate_card_to_date
    end                                             as realisation_ratio,

    period_hours,
    people_count,
    person_concentration_ratio,
    period_entry_count,
    late_entry_count,
    late_entry_ratio,

    case when cardinality(confidence_reasons) = 0 then 'high' else 'low' end
                                                    as projection_confidence,
    case
        when cardinality(confidence_reasons) = 0 then format(
            '%s readable weeks with time logged in every one, %s%% of entries filed after the period closed',
            weeks_counted, round(100 * coalesce(late_entry_ratio, 0), 1)
        )
        else array_to_string(confidence_reasons, '; ')
    end                                             as confidence_reason
from confidence;
