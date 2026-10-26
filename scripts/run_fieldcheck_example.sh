#!/usr/bin/env bash
# Build the worked fieldcheck example from a `glance demo` folder (needs store.yaml, demo.db and *_gt.json.gz).
#   scripts/run_fieldcheck_example.sh demo_out [examples/fieldcheck]
# It writes the simulated annotator's labels, processes a short clip of the EMPTY store into a COPY of the demo
# database (the original is never touched), and runs the harness. The numbers show the harness working, not real accuracy.
set -euo pipefail
DEMO="${1:?usage: $0 DEMO_DIR [OUT_DIR]}"
OUT="${2:-examples/fieldcheck}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python scripts/make_fieldcheck_example.py --demo "$DEMO" --out "$OUT"
cp "$DEMO/demo.db" "$WORK/example.db"
glance sim --empty --out "$WORK/empty" --minutes 15 > /dev/null
glance run --config "$DEMO/store.yaml" --video "$WORK/empty/empty_store.mp4" --start 2026-11-14T02:00 \
  --db "$WORK/example.db" --label empty_store > /dev/null
EMPTY_ID="$(python - "$WORK/example.db" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT run_id FROM runs WHERE label='empty_store'").fetchone()[0])
PY
)"
sed -i "s/EMPTY_RUN_ID/${EMPTY_ID}/" "$OUT/fieldcheck.yaml"
glance fieldcheck --config "$DEMO/store.yaml" --db "$WORK/example.db" --spec "$OUT/fieldcheck.yaml" \
  --json "$OUT/result.json" | tee "$OUT/result.txt"
