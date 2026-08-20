-- get_financials. One engagement, one period, what it billed and what it collected.
--
-- Every ratio here is computed in SQL. No percentage that reaches a client-facing deck
-- should ever have been calculated by a model, and the way to guarantee that is for the
-- model never to be handed the operands.
--
-- margin_pct is the same definition the health score's margin component reads, because
-- both come from engagement_financials_v1. Fixed fee takes the fee as revenue; time and
-- materials takes billable value at rate card. Mess case 4 is constructed against the
-- first, and the contradiction it carries — negative margin beside a healthy burn — is
-- reported by these two tools without either resolving it.
--
-- payment_behaviour_change_pct is DSO against this client's own history rather than a
-- global threshold. A client that has always paid on day 45 is not a risk; one that used
-- to pay on day 10 and now pays on day 40 is. Null where there is no history to compare
-- against, which is not the same as zero and is not reported as zero.

select
    f.engagement_id,
    e.name,
    c.name                                              as client_name,
    f.fee_type,
    f.period_start,
    f.period_end,
    f.as_of,

    f.invoiced,
    f.paid,
    f.wip_unbilled,
    f.cost_to_date,
    f.billable_value_to_date,
    f.ceiling_amount,

    round(100 * f.margin_ratio, 1)                      as margin_pct,
    round(100 * f.realisation_ratio, 1)                 as realisation_pct,
    round(f.dso_days, 1)                                as dso_days,
    round(f.dso_baseline_days, 1)                       as dso_baseline_days,
    round(100 * f.payment_behaviour_change_ratio, 1)    as payment_behaviour_change_pct,

    (select version from scoring_model where is_active) as scoring_model_version
from engagement_financials_v1 f
join engagements e on e.id = f.engagement_id
join clients     c on c.id = f.client_id
where f.engagement_id = :engagement_id::int
  and f.period_start  = date_trunc('month', :period::date)::date;
