-- What has been billed and collected against an engagement, on top of what it cost.
--
--   psql "$BIZDATA_DSN" -f db/views/engagement_financials_v1.sql
--
-- Requires engagement_burn_v1, whose columns it carries forward unchanged. Two views
-- rather than one because the questions are different — how much work went in, versus
-- how much of it turned into cash — but one chain rather than two, because the health
-- score reads both and a second independent scan of the burn view would double the cost
-- of every scoring query.
--
-- DSO lives here rather than in engagement_burn_v1 because payment behaviour belongs to
-- the client, not to the hours. It is measured against the client's own history rather
-- than a global threshold: a client that has always paid on day 45 is not a risk, and
-- one that used to pay on day 10 and now pays on day 40 is. Ninety days of recent
-- behaviour against the nine months before it.
--
-- Both laterals are correlated, so the burn view is scanned once. That is the whole
-- reason for the shape.

create or replace view engagement_financials_v1 as
with client_paid as (
    select
        e.client_id,
        i.paid_at,
        (i.paid_at - i.issued_at) as days_to_pay
    from invoices i
    join engagements e on e.id = i.engagement_id
    where i.status = 'paid'
)

select
    b.*,

    bill.invoiced,
    bill.paid,

    -- Delivered but not yet on an invoice. Negative where billing has run ahead of
    -- delivery, which happens on fixed fee and is reported rather than clamped.
    b.billable_value_to_date - bill.invoiced        as wip_unbilled,

    d.dso_current                                   as dso_days,
    d.dso_baseline                                  as dso_baseline_days,
    case
        when d.dso_baseline > 0 and d.dso_current is not null
            then (d.dso_current - d.dso_baseline) / d.dso_baseline
    end                                             as payment_behaviour_change_ratio
from engagement_burn_v1 b

left join lateral (
    select
        coalesce(sum(i.amount) filter (
            where i.status in ('issued', 'paid') and i.issued_at <= b.as_of
        ), 0) as invoiced,
        coalesce(sum(i.amount) filter (
            where i.status = 'paid' and i.paid_at <= b.as_of
        ), 0) as paid
    from invoices i
    where i.engagement_id = b.engagement_id
) bill on true

left join lateral (
    select
        avg(cp.days_to_pay) filter (where cp.paid_at >  b.as_of - 90)  as dso_current,
        avg(cp.days_to_pay) filter (where cp.paid_at <= b.as_of - 90)  as dso_baseline
    from client_paid cp
    where cp.client_id = b.client_id
      and cp.paid_at  <= b.as_of
      and cp.paid_at   > b.as_of - 365
) d on true;
