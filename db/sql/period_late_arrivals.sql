-- Everything filed since a given instant that this period's pack would read.
--
-- The second half of get_run_ledger. period_watermark.sql says *whether* anything changed;
-- this says what, by name, so the answer to "how do you handle late data?" is a list of
-- rows rather than a policy. Three entries arrived after the period closed, here they are.
--
-- Scoped cumulatively to period_end, matching period_watermark.sql exactly. The reason is
-- the same and is worth repeating rather than cross-referencing: an entry backdated into an
-- earlier month still moves hours_to_date, and hours_to_date is on this period's deck.
--
-- Two flags rather than one, because they are different findings and a reader who only
-- gets a count cannot tell them apart:
--
--   filed_after_period_close  the classic late timesheet, mess case 1
--   backdated                 work dated before this period that only just showed up
--
-- The late boundary is the same explicit UTC instant used in db/sql/get_time_summary_quality.sql
-- and db/views/engagement_burn_v1.sql, so all three agree on what "after the period closed"
-- means regardless of the session's timezone.
--
-- count(*) over () is evaluated before the limit, so total_count is the true number even
-- when the rows are capped. A caller that reported the length of the list instead would
-- quietly understate a large backfill, which is the one case where the number matters most.

select
    count(*) over ()                                as total_count,

    t.id,
    t.person_id,
    p.name                                          as person_name,
    t.engagement_id,
    e.name                                          as engagement_name,
    t.entry_date,
    t.hours,
    t.billable,
    t.submitted_at,

    (t.submitted_at >= ((:period_end::date + 1)::timestamp at time zone 'UTC'))
                                                    as filed_after_period_close,
    (t.entry_date < :period_start::date)             as backdated

from time_entries t
join people      p on p.id = t.person_id
join engagements e on e.id = t.engagement_id

where t.entry_date <= :period_end::date
  and t.submitted_at > :since::timestamptz

order by t.submitted_at, t.id
limit 200;
