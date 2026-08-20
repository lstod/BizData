#!/usr/bin/env bash
# Reseed every fixture seed and run the SQL checks against each, writing one file per
# seed to the directory given as $1.
#
#   scripts/sweep.sh /tmp/baseline
#
# Steps 1 and 2 both ran their Done-when conditions across the whole fixture set rather
# than the demo seed, and both times that breadth found a defect invisible on seed 42.
# This is that sweep, as a file, so the step-3 view refactor can be proved against the
# recorded output rather than described as safe.

set -euo pipefail

OUT=${1:?usage: sweep.sh OUTPUT_DIR}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DSN=${BIZDATA_DSN:-postgresql://bizdata:bizdata@localhost:5433/bizdata}
PY=${PYTHON:-$REPO/.venv/bin/python}
SEEDS=${SEEDS:-"42 43 9001 9002 9003 9004 9005 9006 9007 9008 9009 9010 9011 9012 9013 9014 9015"}

mkdir -p "$OUT"

for seed in $SEEDS; do
    "$PY" "$REPO/scripts/seed.py" --seed "$seed" --period 2026-08 --dsn "$DSN" >/dev/null
    {
        for check in mess_cases checksums health_v1; do
            echo "-- $check"
            psql "$DSN" -X -q -f "$REPO/db/checks/$check.sql"
        done
    } > "$OUT/seed-$seed.txt"
    echo "seed $seed"
done
