-- get_time_summary. Forty thousand rows in, tens of rows out.
--
-- The aggregation is the tool. Nothing about handing a model forty thousand time entries
-- is a design preference to be defended; it does not fit in a context window and it does
-- not fit down the Data API's 1 MiB pipe either, so aggregating server side is the only
-- thing that works.
--
-- Two markers, {{group_select}} and {{group_by}}, are substituted from a fixed dictionary
-- in server/tools/get_time_summary.py — five entries, one per documented group_by mode. A
-- grouping expression is not a value, so it cannot be a bound parameter, and this is the
-- one place in db/sql/ where text is interpolated. It never touches a tool argument: the
-- argument selects a key, and an unknown key is rejected before the query is read.
--
-- The doubled braces above are not a typo. This file goes through str.format, so a marker
-- named in prose is substituted exactly like a marker in the query, and a multi-line
-- fragment dropped into a single-line comment puts the rest of itself into the statement.
--
-- Every mode returns the same six dimension columns with nulls where that dimension is not
-- part of the grouping, so one response shape covers all five and the caller does not have
-- to branch on what it asked for.
--
-- total_count is a window over the grouped rows, so it counts groups rather than entries
-- and is computed before the limit. A truncated response therefore says so, rather than
-- looking like a small period.

select
    {group_select},
    sum(t.hours)                                            as hours,
    coalesce(sum(t.hours) filter (where t.billable), 0)     as billable_hours,
    sum(t.hours * pe.cost_rate)                             as cost,
    coalesce(
        sum(t.hours * pe.bill_rate) filter (where t.billable), 0
    )                                                       as billable_value,
    count(*)                                                as entry_count,
    count(*) over ()                                        as total_count
from time_entries t
join people      pe on pe.id = t.person_id
join engagements e  on e.id  = t.engagement_id
where (:engagement_ids::int[] is null or t.engagement_id = any(:engagement_ids::int[]))
  and t.entry_date between :period_start::date and :period_end::date
group by {group_by}
order by {group_by}
limit :max_rows::int;
