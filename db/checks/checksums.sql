-- Row counts and a content checksum per table. This is what "the seed is deterministic"
-- is checked against, rather than asserted.
--
--   psql "$BIZDATA_DSN" -Aqt -f db/checks/checksums.sql > /tmp/run-1.txt
--   # reseed with the same --seed and --period
--   psql "$BIZDATA_DSN" -Aqt -f db/checks/checksums.sql > /tmp/run-2.txt
--   diff /tmp/run-1.txt /tmp/run-2.txt && echo identical
--
-- The timezone is set explicitly because time_entries.submitted_at is a timestamptz and
-- its text rendering follows the session TimeZone. Without this line the checksum would
-- depend on the client's locale rather than on the data, and the check would be
-- reassuring instead of true.

set timezone = 'UTC';

select 'clients'        as table_name, count(*) as rows, md5(string_agg(t::text, '|' order by t.id)) as checksum from clients t
union all
select 'people',         count(*), md5(string_agg(t::text, '|' order by t.id)) from people t
union all
select 'engagements',    count(*), md5(string_agg(t::text, '|' order by t.id)) from engagements t
union all
select 'sow_line_items', count(*), md5(string_agg(t::text, '|' order by t.id)) from sow_line_items t
union all
select 'time_entries',   count(*), md5(string_agg(t::text, '|' order by t.id)) from time_entries t
union all
select 'invoices',       count(*), md5(string_agg(t::text, '|' order by t.id)) from invoices t
order by table_name;
