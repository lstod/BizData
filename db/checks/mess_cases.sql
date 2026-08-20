-- One assertion per seeded mess case. Eight rows out, every count non-zero.
--
--   psql "$BIZDATA_DSN" -f db/checks/mess_cases.sql
--
-- The period is derived from the data rather than hardcoded, so this file works against
-- any --period the generator was run with, and against any of the reserved fixture
-- seeds without editing.
--
-- If a case ever returns zero, the generator changed and a Skill lost the case it was
-- written against. That is the whole point of the file.

set timezone = 'UTC';

with params as (
    select
        date_trunc('month', max(entry_date))::date                                       as period_start,
        (date_trunc('month', max(entry_date)) + interval '1 month - 1 day')::date        as period_end
    from time_entries
),

period_entries as (
    select t.*
    from time_entries t, params p
    where t.entry_date between p.period_start and p.period_end
),

active as (
    select * from engagements where status = 'active'
),

active_count as (
    select count(*)::numeric as n from active
),

-- Cost and hours over the whole window, for the margin test in case 4.
engagement_totals as (
    select
        t.engagement_id,
        sum(t.hours)                as hours,
        sum(t.hours * pe.cost_rate) as cost
    from time_entries t
    join people pe on pe.id = t.person_id
    group by t.engagement_id
),

-- Per person share of an engagement's period hours, for case 8.
person_hours as (
    select engagement_id, person_id, sum(hours) as hours
    from period_entries
    group by engagement_id, person_id
),

engagement_period_hours as (
    select engagement_id, sum(hours) as hours
    from person_hours
    group by engagement_id
),

-- Reporting coverage per week, for case 7.
weekly_coverage as (
    select
        date_trunc('week', t.entry_date)::date                      as week_start,
        count(distinct t.engagement_id)                             as engagements_reporting
    from period_entries t
    join active e on e.id = t.engagement_id
    group by 1
),

-- Case 1 is asserted on the property the tools depend on rather than on a headcount of
-- seeded engagements. Some time always lands after month end simply because entries in
-- the last few days of the period are filed in the first few days of the next one, so
-- "has late entries" does not distinguish the deliberate cases from ordinary lag. What
-- has to be true is that at least one engagement crosses the 10% mark where
-- get_engagement_burn drops projection_confidence to low, and that the firm-wide rate
-- stays under the 20% at which assemble-delivery-pack refuses to review the period.
late_by_engagement as (
    select
        t.engagement_id,
        count(*)                                                                    as entries,
        count(*) filter (where t.submitted_at >= (p.period_end + 1)::timestamptz)   as late
    from period_entries t, params p
    group by t.engagement_id
),

case_1 as (
    select
        -- Restricted to engagements with a month's worth of entries. On an engagement
        -- with nine entries in the period, one filed late is 11%, which is arithmetic
        -- rather than a reporting problem.
        count(*) filter (where entries >= 30 and late::numeric / entries > 0.10)  as n,
        count(*) filter (where late > 0)                                          as engagements_late,
        sum(late)                                                                 as late_rows,
        round(100.0 * sum(late) / sum(entries), 1)                                as firm_pct
    from late_by_engagement
),

case_2 as (
    select
        count(*)                          as n,
        coalesce(sum(d.c) - count(*), 0)  as extra_rows
    from (
        select count(*) as c
        from time_entries
        group by person_id, engagement_id, entry_date, hours
        having count(*) > 1
    ) d
),

case_3 as (
    select
        count(*)          as n,
        min(e.id)         as engagement_id,
        min(p.period_end - 21) as silent_from
    from active e, params p
    where exists (
        select 1 from time_entries t
        where t.engagement_id = e.id
          and t.entry_date >= p.period_start
          and t.entry_date <  p.period_end - 21
    )
    and not exists (
        select 1 from time_entries t
        where t.engagement_id = e.id
          and t.entry_date >= p.period_end - 21
          and t.entry_date <= p.period_end
    )
),

case_4 as (
    select
        count(*)  as n,
        min(e.id) as engagement_id
    from engagements e
    join engagement_totals x on x.engagement_id = e.id
    where e.fee_type = 'fixed'
      and x.hours / e.ceiling_hours between 0.50 and 0.80   -- burn looks healthy
      and e.ceiling_amount - x.cost < 0                     -- and the fee is under water
),

case_5 as (
    select count(*) as n
    from time_entries
    where billable is null
),

case_6 as (
    select
        count(*)  as n,
        min(e.id) as engagement_id
    from active e, params p
    where e.end_date between p.period_start and p.period_end
),

case_7 as (
    select
        count(*)             as n,
        min(w.week_start)    as week_start,
        min(round(100 * w.engagements_reporting / a.n, 1)) as pct_reporting
    from weekly_coverage w, active_count a, params p
    where w.week_start >= p.period_start
      and w.engagements_reporting / a.n < 0.20
),

-- Scoped the way scope-escalation scopes it: concentration above 70% on an engagement
-- above the median fee. A small engagement run by one person is concentrated by
-- arithmetic and is not a continuity risk worth a partner's attention.
median_fee as (
    select percentile_cont(0.5) within group (order by ceiling_amount) as fee from active
),

concentration as (
    select
        ph.engagement_id,
        max(ph.hours / eph.hours) as top_share
    from person_hours ph
    join engagement_period_hours eph using (engagement_id)
    join active e on e.id = ph.engagement_id
    cross join median_fee m
    where e.ceiling_amount > m.fee
      and eph.hours >= 40
    group by ph.engagement_id
),

case_8 as (
    select
        count(*) filter (where top_share > 0.70) as n,
        (select engagement_id from concentration order by top_share desc, engagement_id limit 1)
            as engagement_id,
        (select round(100 * top_share, 1) from concentration order by top_share desc, engagement_id limit 1)
            as top_share_pct
    from concentration
)

select 1 as case_no, 'late submissions after period close' as case_name,
       n as count,
       format(
           '%s engagement(s) over the 10%% confidence threshold; %s filed late at all, %s entries, %s%% firm-wide',
           n, engagements_late, late_rows, firm_pct
       ) as detail
from case_1
union all
select 2, 'duplicated time entry',
       n,
       format('%s duplicate group(s), %s redundant row(s)', n, extra_rows)
from case_2
union all
select 3, 'engagement silent for three weeks',
       n,
       format('engagement %s has no entries from %s to period end', engagement_id, silent_from)
from case_3
union all
select 4, 'fixed fee, negative margin, healthy burn',
       n,
       format('engagement %s', engagement_id)
from case_4
union all
select 5, 'null billable flag',
       n,
       format('%s entries with billable not set', n)
from case_5
union all
select 6, 'end_date falls mid-period',
       n,
       format('engagement %s', engagement_id)
from case_6
union all
select 7, 'week under 20% reporting coverage',
       n,
       format('week of %s at %s%% of active engagements reporting', week_start, pct_reporting)
from case_7
union all
select 8, 'single person over 70% of engagement hours',
       n,
       format('engagement %s at %s%%', engagement_id, top_share_pct)
from case_8
order by case_no;
