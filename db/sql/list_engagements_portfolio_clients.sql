-- Margin by client, for the deck's third slide.
--
-- The blend is a ratio of sums rather than a mean of the client's engagement margins, which
-- matters most here of anywhere: a client with one large engagement at 8% and one tiny one at
-- 60% is a client at roughly 8%, and the mean says 34%.
--
-- Ordered by client name rather than by margin. The chart sorts if it wants to; a query that
-- returns risk-ordered rows invites a reader to treat position as a finding.

select
    c.client_id,
    c.client_name,
    c.engagements_active,

    c.ceiling_hours,
    c.hours_to_date,
    c.ceiling_amount,
    c.revenue_to_date,
    c.cost_to_date,

    round(100 * c.burn_ratio, 1)                        as burn_pct,
    round(100 * c.margin_ratio, 1)                      as margin_pct
from portfolio_client_summary_v1 c
where c.period_start = date_trunc('month', :as_of_date::date)::date
order by c.client_name;
