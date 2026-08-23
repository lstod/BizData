-- The portfolio as one row, and as one row per client. Step 7.
--
--   psql "$BIZDATA_DSN" -f db/views/portfolio_summary_v1.sql
--
-- Requires engagement_financials_v1 and engagement_health_v1.
--
-- Why this exists. The partner deck's second and third slides want figures no per-engagement
-- tool returns: total hours, blended margin, movement against last month, and margin by
-- client. Every other number in the pack is copied out of a tool response, and the workbook
-- gets its portfolio totals from Excel formulas over the Engagements tab. A deck has no
-- formula layer, so the choice was to let the deck builder add the numbers up or to compute
-- them here. Adding them up in the builder would put a second place figures come from into
-- the one artifact a partner actually reads out loud.
--
-- **Blended margin is a ratio of sums, not a mean of ratios.** Averaging eighteen margin
-- percentages weights a £40k engagement the same as a £900k one and is a different quantity
-- with the same name. So the fee-type branch from engagement_burn_v1.margin_ratio is carried
-- here as its two operands rather than as the ratio, summed, and divided once at the end.
--
-- The fee-type branch itself is inherited rather than re-decided, for the third time in this
-- schema: fixed fee takes the fee as revenue, time and materials takes billable value at rate
-- card. Restating it as a single formula is how mess case 4 stops being a contradiction.
--
-- Scope: active engagements, firm-wide, deliberately not narrowed to whatever the caller
-- filtered its rows to. This is the same decision get_time_summary's data_completeness block
-- makes, for the same reason — a portfolio summary computed over the three engagements
-- somebody asked about is not a portfolio summary. The field names say `active` so a reader
-- cannot mistake which denominator is in play.

create or replace view portfolio_summary_v1 as
with per_engagement as (
    select
        f.period_start,
        f.period_end,
        f.engagement_id,
        f.client_id,
        f.ceiling_hours,
        f.hours_to_date,
        f.cost_to_date,
        f.ceiling_amount,

        -- The numerator and denominator of margin_ratio, kept apart so they can be summed.
        -- A T&M engagement that has billed nothing contributes zero revenue and its real
        -- cost, which is the honest treatment: engagement_financials_v1 reports its margin
        -- as null because one engagement's margin is undefined there, but the portfolio's
        -- is not undefined and the cost is still real money.
        case
            when f.fee_type = 'fixed' then f.ceiling_amount
            else f.billable_value_to_date
        end                                             as revenue_to_date
    from engagement_financials_v1 f
    where f.status = 'active'
),

health as (
    select
        h.period_start,
        avg(h.health_score)                             as mean_health_score,
        count(*) filter (where h.health_band = 'green') as engagements_green
    from engagement_health_v1 h
    join engagements e on e.id = h.engagement_id
    where e.status = 'active'
    group by h.period_start
),

rolled as (
    select
        p.period_start,
        max(p.period_end)                               as period_end,
        count(*)                                        as engagements_active,
        count(distinct p.client_id)                     as clients_active,
        sum(p.ceiling_hours)                            as ceiling_hours_total,
        sum(p.hours_to_date)                            as hours_to_date_total,
        sum(p.ceiling_amount)                           as ceiling_amount_total,
        sum(p.revenue_to_date)                          as revenue_to_date_total,
        sum(p.cost_to_date)                             as cost_to_date_total
    from per_engagement p
    group by p.period_start
),

computed as (
    select
        r.*,
        r.hours_to_date_total / nullif(r.ceiling_hours_total, 0)    as portfolio_burn_ratio,
        (r.revenue_to_date_total - r.cost_to_date_total)
            / nullif(r.revenue_to_date_total, 0)                    as blended_margin_ratio,
        h.mean_health_score,
        h.engagements_green
    from rolled r
    left join health h on h.period_start = r.period_start
)

-- Movement against the portfolio's own prior month, the same shape and the same reasoning as
-- engagement_health_v1.score_delta_vs_prior_period: a window over this view's own history
-- rather than a second scan, and null in the first period rather than zero, because "no
-- change" and "nothing to compare against" are different answers.
select
    period_start,
    period_end,
    engagements_active,
    clients_active,
    engagements_green,
    ceiling_hours_total,
    hours_to_date_total,
    ceiling_amount_total,
    revenue_to_date_total,
    cost_to_date_total,
    portfolio_burn_ratio,
    blended_margin_ratio,
    mean_health_score,

    blended_margin_ratio - lag(blended_margin_ratio) over w
                                                    as blended_margin_delta_vs_prior_period,
    mean_health_score - lag(mean_health_score) over w
                                                    as mean_health_score_delta_vs_prior_period,
    hours_to_date_total - lag(hours_to_date_total) over w
                                                    as hours_to_date_delta_vs_prior_period,
    engagements_active - lag(engagements_active) over w
                                                    as engagements_active_delta_vs_prior_period
from computed
window w as (order by period_start);


-- One row per client per period. Slide 3 is a chart of this, and it is a separate view rather
-- than a grouping set on the one above because the two grains are read by different callers
-- and a nullable dimension column would make every consumer branch on which shape it got.

create or replace view portfolio_client_summary_v1 as
with per_engagement as (
    select
        f.period_start,
        f.client_id,
        f.ceiling_hours,
        f.hours_to_date,
        f.cost_to_date,
        f.ceiling_amount,
        case
            when f.fee_type = 'fixed' then f.ceiling_amount
            else f.billable_value_to_date
        end                                             as revenue_to_date
    from engagement_financials_v1 f
    where f.status = 'active'
),

rolled as (
    select
        p.period_start,
        p.client_id,
        count(*)                                        as engagements_active,
        sum(p.ceiling_hours)                            as ceiling_hours,
        sum(p.hours_to_date)                            as hours_to_date,
        sum(p.ceiling_amount)                           as ceiling_amount,
        sum(p.revenue_to_date)                          as revenue_to_date,
        sum(p.cost_to_date)                             as cost_to_date
    from per_engagement p
    group by p.period_start, p.client_id
)

select
    r.period_start,
    r.client_id,
    c.name                                              as client_name,
    r.engagements_active,
    r.ceiling_hours,
    r.hours_to_date,
    r.ceiling_amount,
    r.revenue_to_date,
    r.cost_to_date,
    r.hours_to_date / nullif(r.ceiling_hours, 0)        as burn_ratio,
    (r.revenue_to_date - r.cost_to_date)
        / nullif(r.revenue_to_date, 0)                  as margin_ratio
from rolled r
join clients c on c.id = r.client_id;
