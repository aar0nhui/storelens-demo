#!/usr/bin/env python
"""Internal-consistency check of the recommender on simulated worlds where the true answer is known.

    python scripts/backtest_recommender.py [--seeds 200] [--json out.json]

What this CAN show: given the shopper behaviour the world was built from, how often does ``recommend`` flag a display
that really is weak (power), how often does it flag one when every display is identical (false alarms), and is the
"extra engaged shoppers per visitor" it quotes close to what really happens once the weak display is brought up to the
store's median (bias and spread).

What it CANNOT show: that a real store's sales respond like this. The money figures multiply by a purchase rate and a
basket size that are assumptions, and a real change in a real store brings effects (price, product, season) that are not in
this simulation. Treat a clean result here as "the arithmetic and the statistics behave", nothing more.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

from glance.analysis import ZoneVisit
from glance.queries import Analytics, Window
from glance.recommend import recommend
from glance.sim.layout import build_store_config, default_layout
from glance.store import EventStore

T0 = 1_800_000_000.0
FIX = ["front_table", "wall_a", "gondola_c", "endcap_b", "back_promo"]
# fixture -> (share of visitors who come near it, share of those who stop)
TYPICAL = (0.60, 0.40)
PLANTED = {"endcap_b": (0.60, 0.12),    # seen, rarely stopped at: the conversion problem
           "back_promo": (0.20, 0.55)}  # engaging, rarely reached: the reach problem


def build_store(path: Path, profile: dict[str, tuple[float, float]], n: int, seed: int) -> EventStore:
    st = EventStore(path)
    rid = st.begin_run("sim", "cam1", "sim.mp4", T0)
    rng = np.random.default_rng(seed)
    t = T0
    for k in range(n):
        key = f"{k + 1:012x}"
        st.add_visit(rid, key, "customer", t, t + 60)
        zvs, tz = [], t + 5
        for z in FIX:
            reach, rate = profile[z]
            if rng.random() < reach:
                stopped = 10.0 if rng.random() < rate else 0.5
                zvs.append(ZoneVisit(z, tz, tz + 12.0, 12.0, stopped, 0.0))
                tz += 13.0
        st.add_zone_visits(rid, key, zvs)
        st.add_entries(rid, key, [(t, 1), (t + 59, -1)])
        t += 12.0
    st.finish_run(rid, t, {"customers": n})
    st.commit()
    return st


def realised_engaged_per_visitor(st: EventStore, cfg, zone: str) -> float:
    an = Analytics(st, cfg)
    n = an.summary(Window())["visitors"]
    z = next(z for z in an.zones(Window()) if z["zone_id"] == zone)
    return z["engaged"] / n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--sizes", type=int, nargs="+", default=[300, 600, 1200], help="visitors per simulated period")
    ap.add_argument("--json")
    a = ap.parse_args()
    cfg = build_store_config(default_layout(), seed=1)
    out: dict = {"seeds": a.seeds, "sizes": {}}
    tmp = Path(tempfile.mkdtemp())

    planted_world = {z: PLANTED.get(z, TYPICAL) for z in FIX}
    # the true gain per visitor of the full scenario (display reaches the typical rate) for each planted display
    true_gain = {"endcap_b": PLANTED["endcap_b"][0] * (TYPICAL[1] - PLANTED["endcap_b"][1]),
                 "back_promo": (TYPICAL[0] - PLANTED["back_promo"][0]) * PLANTED["back_promo"][1]}
    # the fix for a conversion problem raises the stop rate; the fix for a reach problem raises reach and keeps its rate
    fixed_world = {**planted_world, "endcap_b": TYPICAL, "back_promo": (TYPICAL[0], PLANTED["back_promo"][1])}
    null_world = {z: TYPICAL for z in FIX}

    for n in a.sizes:
        row: dict = {"visitors": n}
        flagged = {z: 0 for z in PLANTED}
        wrong_type = {z: 0 for z in PLANTED}
        pred_full: dict[str, list[float]] = {z: [] for z in PLANTED}
        pred_half: dict[str, list[float]] = {z: [] for z in PLANTED}
        measured_gain: dict[str, list[float]] = {z: [] for z in PLANTED}
        false_alarm_runs = 0
        false_rev: list[float] = []
        for s in range(a.seeds):
            base = build_store(tmp / f"b{n}_{s}.db", planted_world, n, 1000 + s)
            after = build_store(tmp / f"a{n}_{s}.db", fixed_world, n, 5000 + s)
            rec = recommend(Analytics(base, cfg), Window())
            by = {r["zone_id"]: r for r in rec["recommendations"] if r["lever"] == "layout"}
            for z, want in (("endcap_b", "improve_conversion"), ("back_promo", "increase_reach")):
                r = by.get(z)
                if not r:
                    continue
                flagged[z] += 1
                wrong_type[z] += r["type"] != want
                V = rec["assumptions"]["visitors_per_day"]
                lo, hi = r["effect"]["extra_engaged_per_day"]
                pred_half[z].append(lo / V)
                pred_full[z].append(hi / V)
                measured_gain[z].append(realised_engaged_per_visitor(after, cfg, z) - realised_engaged_per_visitor(base, cfg, z))
            nul = build_store(tmp / f"n{n}_{s}.db", null_world, n, 9000 + s)
            nr = recommend(Analytics(nul, cfg), Window())
            lay = [r for r in nr["recommendations"] if r["lever"] == "layout"]
            if lay:
                false_alarm_runs += 1
                false_rev.append(float(np.mean([sum(r["effect"]["revenue_per_day"]) / 2 for r in lay])))
            for p in tmp.glob(f"*{n}_{s}.db*"):
                p.unlink()
        for z in PLANTED:
            m = lambda xs: float(np.mean(xs)) if xs else None  # noqa: E731
            row[z] = {"flagged_share": flagged[z] / a.seeds, "right_type_when_flagged":
                      (1 - wrong_type[z] / flagged[z]) if flagged[z] else None,
                      "true_gain_per_visitor": true_gain[z], "quoted_full_target_per_visitor": m(pred_full[z]),
                      "quoted_half_target_per_visitor": m(pred_half[z]), "measured_gain_per_visitor": m(measured_gain[z]),
                      "quoted_full_target_sd": float(np.std(pred_full[z])) if len(pred_full[z]) > 1 else None}
        row["identical_displays"] = {"runs_with_any_layout_flag": false_alarm_runs / a.seeds,
                                     "mean_revenue_per_day_midpoint_when_flagged": float(np.mean(false_rev)) if false_rev else 0.0}
        out["sizes"][str(n)] = row

    print(f"{a.seeds} simulated worlds per size. Weak displays are planted; 'identical' means no display is weak.\n")
    print(f"{'visitors':>9}  {'display':<11} {'flagged':>8} {'right type':>10} {'true gain':>10} {'quoted (full)':>14} "
          f"{'+/- sd':>7} {'measured':>9}")
    for n, row in out["sizes"].items():
        for z in PLANTED:
            d = row[z]
            fmt = lambda v: "   -" if v is None else f"{v:.3f}"  # noqa: E731
            print(f"{n:>9}  {z:<11} {d['flagged_share'] * 100:7.0f}% "
                  f"{'-' if d['right_type_when_flagged'] is None else format(d['right_type_when_flagged'] * 100, '.0f') + '%':>10} "
                  f"{d['true_gain_per_visitor']:>10.3f} {fmt(d['quoted_full_target_per_visitor']):>14} "
                  f"{fmt(d['quoted_full_target_sd']):>7} {fmt(d['measured_gain_per_visitor']):>9}")
        ia = row["identical_displays"]
        print(f"{'':>9}  identical displays: a layout flag appears in {ia['runs_with_any_layout_flag'] * 100:.1f}% of runs\n")
    print("gains are extra engaged shoppers per visitor. 'quoted (full)' is the top of the scenario range (the store median as\n"
          "target); 'measured' is what the simulated store really gained after the weak display was brought to the median.\n"
          "This checks the arithmetic and the statistics, not whether a real store's sales respond this way.")
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
