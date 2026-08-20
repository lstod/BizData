-- get_engagement_burn. One engagement, one period, everything about how the hours are
-- tracking against the ceiling.
--
-- Every figure here is read from engagement_burn_v1 rather than computed, which is what
-- makes this number and the burn_trajectory component of the health score the same number
-- rather than two numbers that agree most of the time.
--
-- projection_confidence and its reason come out of the view too. That is the single most
-- important thing in this file: a projection the agent should not trust is labelled by the
-- data layer, with the reason attached, rather than left for a model to infer. Putting it
-- in a prompt makes it a thing that can be forgotten.
--
-- Nothing is re-baselined. hours_remaining goes negative when the ceiling has been passed
-- and projected_overrun_pct keeps counting; the denominator never moves.

select
    b.engagement_id,
    e.name,
    c.name                                              as client_name,
    e.sow_ref,
    b.fee_type,
    b.status,
    b.period_start,
    b.period_end,
    b.as_of,

    b.hours_to_date,
    b.ceiling_hours,
    round(100 * b.burn_ratio, 1)                        as burn_pct,
    b.hours_remaining,
    b.days_remaining,

    round(b.weekly_run_rate_4wk, 2)                     as weekly_run_rate_4wk,
    b.weeks_counted,
    b.weeks_with_no_hours,
    round(b.projected_total_hours, 1)                   as projected_total_hours,
    round(100 * b.projected_overrun_ratio, 1)           as projected_overrun_pct,

    b.projection_confidence,
    b.confidence_reason,

    -- The two ports from spec B. Concentration is within the period, because continuity
    -- risk is about who is on it now; the run rate comparison is against this engagement's
    -- own trailing baseline rather than a portfolio average.
    round(100 * b.person_concentration_ratio, 1)        as person_concentration_pct,
    b.people_count,
    round(100 * b.run_rate_vs_baseline_ratio, 1)        as run_rate_vs_baseline_pct,
    round(b.baseline_weekly_run_rate, 2)                as baseline_weekly_run_rate,
    b.baseline_weeks_counted,

    b.last_entry_date,
    round(b.days_since_last_entry)::int                 as days_since_last_entry,
    b.period_entry_count,
    b.late_entry_count,
    round(100 * b.late_entry_ratio, 1)                  as late_entry_pct,

    (select version from scoring_model where is_active) as scoring_model_version
from engagement_burn_v1 b
join engagements e on e.id = b.engagement_id
join clients     c on c.id = b.client_id
where b.engagement_id = :engagement_id::int
  and b.period_start  = date_trunc('month', :as_of_date::date)::date;
