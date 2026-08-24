-- list_engagements. One row per engagement live in the period, with enough to triage on.
--
-- The design choice worth explaining, and the one the README's "what broke" section is
-- about: the first version of this tool returned engagement metadata only, which forced a
-- fan-out of thirty get_engagement_burn calls just to decide what to look at. burn_pct and
-- the health band are computed in SQL and come back here, so triage is one call.
--
-- person_concentration_pct is on the row for that same reason, added at step 6. Mess case 8
-- is an engagement 85% delivered by one person while sitting at 67% burn in the green band,
-- so a fan-out triggered by burn or band never examines it and the continuity risk is
-- invisible. A triage row that cannot express one of the four risks the portfolio carries
-- is not a triage row. Both columns come off engagement_burn_v1, which already computed
-- them for get_engagement_burn, so this costs a projection and no new work.
--
-- days_since_last_entry and margin_pct join them at step 8, for the third and fourth
-- instances of the same problem. scope-escalation flags an active engagement that has not
-- logged time in fourteen days, and a fixed-fee engagement under water beside a healthy
-- burn — mess cases 3 and 4. Both of those engagements can sit in the green band under 70%
-- burn with one team and a live contract, clearing every examine trigger, and on six of the
-- seventeen fixture seeds they did: the detail call that would have exposed them was the
-- call the filter had already declined to make. A policy can only fire on what triage
-- carries. Both come off engagement_burn_v1 as well, and margin_ratio there is the same
-- column engagement_financials_v1 projects, so the triage margin and get_financials'
-- margin cannot disagree.
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
        round(100 * b.margin_ratio, 1)          as margin_pct,
        round(b.days_since_last_entry)::int     as days_since_last_entry,
        round(100 * b.person_concentration_ratio, 1) as person_concentration_pct,
        b.people_count,
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
