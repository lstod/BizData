-- Engagement health as a deterministic, versioned measurement.
--
--   psql "$BIZDATA_DSN" -f db/views/engagement_health_v1.sql
--
-- Last in the chain. Requires, in order, db/seeds/scoring_weights.sql,
-- portfolio_coverage_v1, engagement_burn_v1 and engagement_financials_v1;
-- scripts/seed.py applies all of them after every load, because db/schema.sql drops the
-- six tables with CASCADE and that takes every view with them.
--
--   portfolio_coverage_v1              one row per week, firm-wide
--   engagement_burn_v1                 engagement x period, measured
--   engagement_financials_v1           engagement x period, billed
--   engagement_health_components_v1    engagement x period x component   <- here
--   engagement_health_v1               engagement x period               <- and here
--
-- At step 2 this file held the whole chain. At step 3 the measurement half moved out,
-- because get_engagement_burn and get_financials need the same trailing run rate, the
-- same margin and the same DSO that the components below are scored from, and two
-- definitions of a run rate is a tool and a score that disagree in front of a partner.
-- What stayed here is the only thing that was ever specific to scoring: the mapping from
-- a measurement to a risk, and the weighted sum of those risks.
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

-- Every measurement this view scores, already computed. The four columns below are the
-- step-2 expressions verbatim; they simply live one view down now.
raw as (
    select
        f.engagement_id,
        f.period_start,
        f.period_end,
        f.weeks_counted,
        f.projected_burn_ratio          as projected_burn_pct,
        f.margin_ratio                  as margin_pct,
        f.days_since_last_entry,
        f.payment_behaviour_change_ratio as dso_change_pct
    from engagement_financials_v1 f
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
