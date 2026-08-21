#!/usr/bin/env bash
# Burn and health-band distribution across every fixture seed, for one generator.
#
#   scripts/ceiling_report.sh
#
# Reseeds each seed locally and reports the two numbers the ceiling_hours defect was
# about: how much of the active portfolio has blown its budget, and whether the health
# score still sorts the book or has collapsed into a single band.

set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DSN=${BIZDATA_DSN:-postgresql://bizdata:bizdata@localhost:5433/bizdata}
PY=${PYTHON:-$REPO/.venv/bin/python}
SEEDS=${SEEDS:-"42 43 9001 9002 9003 9004 9005 9006 9007 9008 9009 9010 9011 9012 9013 9014 9015"}

export BIZDATA_DB_BACKEND=local

printf '%-7s %-6s %-8s %-8s %-9s %-6s %-6s %-5s %s\n' \
    seed over median max green amber red case4 case4_top

for seed in $SEEDS; do
    "$PY" "$REPO/scripts/seed.py" --seed "$seed" --period 2026-08 --dsn "$DSN" >/dev/null
    psql "$DSN" -X -q -t -A -F' ' -c "
    with burn as (
        select * from engagement_burn_v1
        where period_start = '2026-08-01' and status = 'active'
    ),
    band as (
        select h.health_band
        from engagement_health_v1 h
        join engagements e on e.id = h.engagement_id
        where h.period_start = '2026-08-01' and e.status = 'active'
    ),
    c4 as (
        select b.burn_ratio, h.top_risk_factor
        from burn b
        join engagement_health_v1 h
          on h.engagement_id = b.engagement_id and h.period_start = b.period_start
        where b.margin_ratio < 0 and b.fee_type = 'fixed'
        order by b.margin_ratio
        limit 1
    )
    select
        format('%-7s', '$seed'),
        format('%-6s', (select count(*) filter (where burn_ratio > 1) || '/' || count(*) from burn)),
        format('%-8s', (select round(percentile_cont(0.5) within group (order by burn_ratio) * 100) || '%' from burn)),
        format('%-8s', (select round(max(burn_ratio) * 100) || '%' from burn)),
        format('%-9s', (select count(*) from band where health_band = 'green')),
        format('%-6s', (select count(*) from band where health_band = 'amber')),
        format('%-6s', (select count(*) from band where health_band = 'red')),
        format('%-5s', (select round(burn_ratio * 100) || '%' from c4)),
        (select top_risk_factor from c4);
    "
done
