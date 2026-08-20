-- The data_quality block that rides alongside get_time_summary's rows.
--
-- Individual bad records: entries filed after the period closed, entries where nobody set
-- the billable flag, and the same person logging the same hours on the same day twice.
-- Mess cases 1, 5 and 2 respectively.
--
-- This is a separate concern from data_completeness, which is the next file, and they are
-- deliberately two blocks rather than one. data_quality is about records that are wrong.
-- data_completeness is about whether the period can be read at all. An engagement can have
-- flawless records for a week nobody filed against.
--
-- Returning it in the same response as the figures is the point: the model cannot report
-- numbers without also being handed what is wrong with them.
--
-- The late boundary is written as an explicit UTC instant rather than a bare cast, so the
-- answer does not depend on the session timezone. It matches db/checks/mess_cases.sql.

with scoped as (
    select t.*
    from time_entries t
    where (:engagement_ids::int[] is null or t.engagement_id = any(:engagement_ids::int[]))
      and t.entry_date between :period_start::date and :period_end::date
),

duplicate_groups as (
    select count(*) - 1 as redundant_rows
    from scoped
    group by person_id, engagement_id, entry_date, hours
    having count(*) > 1
)

select
    (select count(*) from scoped)                                as entry_count,
    (select count(*) from scoped
      where submitted_at >= ((:period_end::date + 1)::timestamp at time zone 'UTC')
    )                                                            as late_entries,
    (select count(*) from scoped where billable is null)         as null_billable,
    (select count(*) from duplicate_groups)                      as duplicate_groups,
    coalesce((select sum(redundant_rows) from duplicate_groups), 0)
                                                                 as suspected_duplicates,
    case
        when (select count(*) from scoped) > 0 then round(
            100.0 * (select count(*) from scoped
                      where submitted_at >= ((:period_end::date + 1)::timestamp at time zone 'UTC'))
                  / (select count(*) from scoped), 1)
    end                                                          as late_entry_pct,
    (select version from scoring_model where is_active)          as scoring_model_version;
