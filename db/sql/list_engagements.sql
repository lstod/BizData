-- list_engagements. One row per engagement live in the period, with enough to triage on.
--
-- The design choice worth explaining, and the one the README's "what broke" section is
-- about: the first version of this tool returned engagement metadata only, which forced a
-- fan-out of thirty get_engagement_burn calls just to decide what to look at. burn_pct and
-- the health band are computed in SQL and come back here, so triage is one call.
--
-- Paging is keyset on engagement_id rather than an offset. total_count is counted over the
-- whole filtered set rather than what is left after the cursor, so it does not shrink as
-- the caller pages — assemble-delivery-pack's rule is to page until returned_count sums to
-- total_count, and a total that moves underneath that loop is a partial pack that looks
-- complete.
--
-- Rows come from engagement_burn_v1, so an engagement that was not contractually live in
-- the period is absent rather than present with null figures. "Live in the period" is what
-- as_of_date is asking about.
--
-- On the shape: the period is written as an expression in both CTEs rather than computed
-- once above and joined. That looks like the worse of the two and measures as the better
-- one — joined from a CTE the planner treats the period as opaque and builds all twelve
-- months of engagement_burn_v1's grid before discarding eleven, which costs about a third
-- again on the whole query.

with health as (
    select h.*
    from engagement_health_v1 h
    where h.period_start = date_trunc('month', :as_of_date::date)::date
),

burn as (
    select b.*
    from engagement_burn_v1 b
    where b.period_start = date_trunc('month', :as_of_date::date)::date
),

filtered as (
    select
        b.engagement_id,
        c.name                                  as client_name,
        e.name,
        e.sow_ref,
        b.fee_type,
        b.status,
        b.ceiling_hours,
        b.ceiling_amount,
        b.start_date,
        b.end_date,
        b.days_remaining,
        b.hours_to_date,
        round(100 * b.burn_ratio, 1)            as burn_pct,
        h.health_score,
        h.health_band,
        h.score_delta_vs_prior_period,
        h.top_risk_factor,
        h.scoring_model_version
    from engagements e
    join burn b    on b.engagement_id = e.id
    join clients c on c.id = e.client_id
    left join health h on h.engagement_id = b.engagement_id
    where (:status::text is null or e.status = :status::text)
      and (:client_id::int is null or e.client_id = :client_id::int)
),

-- Both counts as window aggregates in one pass, rather than a second CTE that counts
-- `filtered` again. remaining_count is taken after the cursor and before the limit, which
-- is what makes "is there another page" answerable without a second round trip: there is
-- one exactly when remaining_count exceeds the rows in hand. total_count cannot answer
-- that, because it deliberately includes the rows already paged past.
counted as (
    select
        f.*,
        count(*) over ()                                               as total_count,
        count(*) filter (where f.engagement_id > :cursor::int) over ()  as remaining_count
    from filtered f
)

select *
from counted
where engagement_id > :cursor::int
order by engagement_id
limit :limit::int;
