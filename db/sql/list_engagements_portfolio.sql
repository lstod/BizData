-- The portfolio block that rides alongside list_engagements' rows, when asked for.
--
-- Two blocks at a different grain from the rows they travel with is an established shape in
-- this repository rather than a new one: get_time_summary carries data_quality and
-- data_completeness the same way, and for the same reason — the question "what does the whole
-- book look like" is prior to "which engagements should I open", and answering it in a second
-- tool call means it can be skipped.
--
-- Every ratio is scaled to 0..100 here rather than in the view, matching the convention the
-- rest of db/sql/ follows: a column ending _ratio runs 0 to 1 and lives in db/views/, a column
-- ending _pct runs 0 to 100 and is what the tools return. One conversion, in SQL, at the edge.
--
-- Unscoped on purpose. This query takes no status or client_id filter even though
-- list_engagements does, because a portfolio summary narrowed to one client is not a portfolio
-- summary and would be read as one. See the header of db/views/portfolio_summary_v1.sql.

select
    p.period_start,
    p.period_end,

    p.engagements_active,
    p.clients_active,
    p.engagements_green,

    p.ceiling_hours_total,
    p.hours_to_date_total,
    p.ceiling_amount_total,
    p.revenue_to_date_total,
    p.cost_to_date_total,

    round(100 * p.portfolio_burn_ratio, 1)              as portfolio_burn_pct,
    round(100 * p.blended_margin_ratio, 1)              as blended_margin_pct,
    round(p.mean_health_score, 1)                       as mean_health_score,

    round(100 * p.blended_margin_delta_vs_prior_period, 1)
                                                        as blended_margin_delta_pct,
    round(p.mean_health_score_delta_vs_prior_period, 1) as mean_health_score_delta,
    round(p.hours_to_date_delta_vs_prior_period, 1)     as hours_to_date_delta,
    p.engagements_active_delta_vs_prior_period          as engagements_active_delta,

    (select version from scoring_model where is_active) as scoring_model_version
from portfolio_summary_v1 p
where p.period_start = date_trunc('month', :as_of_date::date)::date;
