#!/usr/bin/env python
"""Write an example set of hand-label files for ``storelens fieldcheck`` from the simulator's ground truth.

On a real deployment a person produces these files by watching the footage. Here an imperfect "annotator" is
simulated from the truth (a few miscounted people, missed stops, apparent-age errors) so the harness can be shown end to
end. The numbers it then prints are about the HARNESS working, not about how accurate a real store's counts are.

    python scripts/make_fieldcheck_example.py --demo demo_out --out examples/fieldcheck

``--demo`` is a folder produced by ``storelens demo`` (store.yaml, demo.db, <label>_gt.json.gz).
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from storelens.config import load_config
from storelens.store import EventStore
from storelens.validate import gt_events, load_gt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", required=True)
    ap.add_argument("--out", default="examples/fieldcheck")
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--window-min", type=float, default=5.0, help="length of each door-tally window")
    ap.add_argument("--snapshot-s", type=float, default=30.0, help="spacing of the 'how many people are visible' snapshots")
    a = ap.parse_args()
    demo, out = Path(a.demo), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = load_config(demo / "store.yaml")
    store = EventStore(demo / "demo.db")
    tz = ZoneInfo(cfg.output.timezone)
    rng = np.random.default_rng(a.seed)
    iso = lambda t: datetime.fromtimestamp(t, tz).isoformat(timespec="seconds")  # noqa: E731
    bands = list(cfg.demographics.age_bands)

    door, crowd, stops, demo_rows = [], [], [], []
    run_ids = []
    for rid, label, t0, t1 in store.query("SELECT run_id,label,t_start,t_end FROM runs ORDER BY run_id"):
        gt_path = demo / f"{label}_gt.json.gz"
        if not gt_path.exists():
            continue
        run_ids.append(rid)
        gt = load_gt(gt_path)
        fps = gt["fps"]
        ev = gt_events(gt, cfg, t0)
        attrs = {p["id"]: (p.get("age_band"), p.get("gender")) for p in gt["people"]}
        start = {p["id"]: p["start_frame"] for p in gt["people"]}
        span = gt["n_frames"] / fps
        cust = [p for p in ev if p["role"] == "customer" and p["a"].dropped is None]
        entries = np.array(sorted(t for p in cust for t, d in p["a"].entries if d > 0))

        # door tally: windows of --window-min minutes; a human miscounts by one about one window in six
        w = a.window_min * 60
        for k in range(int(span // w)):
            lo, hi = t0 + k * w, t0 + (k + 1) * w
            n = int(((entries >= lo) & (entries < hi)).sum())
            n = max(0, n + int(rng.choice([-1, 0, 1], p=[0.08, 0.84, 0.08])))
            door.append((iso(lo), iso(hi), n))

        # crowd snapshots: how many customers are in view; people miscount more when it is busy
        spans = np.array([(t0 + start[p["id"]] / fps, t0 + (start[p["id"]] + p["n"]) / fps) for p in cust])
        for t in np.arange(t0 + 60, t0 + span - 60, a.snapshot_s):
            n = int(((spans[:, 0] <= t) & (spans[:, 1] >= t)).sum())
            if n >= 4 and rng.random() < 0.25:
                n += int(rng.choice([-1, 1]))
            crowd.append((iso(t), max(n, 0)))

        # every stop at a display (stood still at least the engage threshold); a human misses ~4% and is a second off
        for p in cust:
            for z in p["a"].zone_visits:
                zc = cfg.zone(z.zone_id)
                if zc.kind == "fixture" and z.stopped_s >= cfg.engage_seconds(zc) and rng.random() > 0.04:
                    stops.append((iso(z.t_enter + rng.normal(0, 0.7)), iso(z.t_exit + rng.normal(0, 0.7)), z.zone_id))

        # apparent age band / perceived gender of everyone who came in, tallied per window of the whole run
        tally: dict[tuple[str, str], int] = {}
        for p in cust:
            age, gen = attrs[p["id"]]
            if age not in bands:
                continue
            i = bands.index(age)
            if rng.random() < 0.2:
                i = int(np.clip(i + rng.choice([-1, 1]), 0, len(bands) - 1))
            if rng.random() < 0.03:
                gen = "male" if gen == "female" else "female"
            tally[(bands[i], gen)] = tally.get((bands[i], gen), 0) + 1
        for (age, gen), n in sorted(tally.items()):
            demo_rows.append((iso(t0), iso(t0 + span), age, gen, n))

    def write(name: str, head: list[str], rows: list[tuple], note: str) -> None:
        with open(out / name, "w", newline="") as fh:
            fh.write(f"# {note}\n")
            wr = csv.writer(fh)
            wr.writerow(head)
            wr.writerows(sorted(rows) if name != "demo_labels.csv" else rows)

    write("door_tally.csv", ["start", "end", "count_in"], door, "simulated annotator: door tally per window")
    write("crowd.csv", ["t", "people"], crowd, "simulated annotator: customers visible at each instant")
    write("stops.csv", ["t_enter", "t_exit", "zone"], stops, "simulated annotator: every stop at a display")
    write("demo_labels.csv", ["start", "end", "age_band", "gender", "count"], demo_rows,
          "simulated annotator: apparent age band and perceived gender of everyone who came in")
    (out / "fieldcheck.yaml").write_text(
        "# Example spec. On a real store: commit the gates BEFORE looking at results, then run\n"
        "#   storelens fieldcheck --config store.yaml --db storelens.db --spec fieldcheck.yaml\n"
        f"runs: {run_ids}\n"
        "empty_runs: [EMPTY_RUN_ID]   # replaced by scripts/run_fieldcheck_example.sh after processing the empty-store clip\n"
        "door_tally: door_tally.csv\ncrowd: crowd.csv\nstops: stops.csv\nstops_exhaustive: true\ndemo_labels: demo_labels.csv\n"
        "gates:\n  door_min_windows: 30\n  door_error_max_pct: 5.0\n  door_bias_max_pct: 3.0\n")
    print(f"wrote {len(door)} door windows, {len(crowd)} snapshots, {len(stops)} stops, "
          f"{sum(r[-1] for r in demo_rows)} labelled people to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
