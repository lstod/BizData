-- The data_completeness block that rides alongside get_time_summary's rows.
--
-- Whether each week in the window can be read at all, firm-wide. This is mess case 7 and
-- it is the demo beat: one week where under 20% of active engagements logged anything,
-- which the naive read calls a delivery collapse and which was a firm-wide offsite.
--
-- Firm-wide rather than scoped to the engagements asked about, deliberately. The question
-- "is this week readable" is not answerable from a subset — if three of eighteen
-- engagements filed, a caller looking at exactly those three sees full coverage.
--
-- The rule that consumes this lives in assemble-delivery-pack at step 6: any week below
-- 60% coverage is a filing artifact, excluded from every run rate and projection, reported
-- as a data note, and never described as a delivery slowdown. firm_wide_gap is that 60%
-- floor already applied, so the Skill compares a boolean rather than a threshold.

select
    week_start,
    week_end,
    engagements_active,
    engagements_reporting,
    pct_active_reporting,
    firm_wide_gap
from portfolio_coverage_v1
where week_end   >= :period_start::date
  and week_start <= :period_end::date
order by week_start;
