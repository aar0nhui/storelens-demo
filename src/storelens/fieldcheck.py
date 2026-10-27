"""Field validation: compare the pipeline with a human's hand tallies on the customer's OWN footage.

The simulator can only show that the logic works. Before a real number goes in front of a customer, someone has to
count by hand, on that store's footage, and this module turns those counts into error figures with confidence
intervals and PASS / FAIL against thresholds written down BEFORE looking at the results.

Inputs are small CSV files (see ``storelens fieldcheck --init``):

  door_tally   start,end,count_in            tally-clicker counts per window at the door
  crowd        t,people                      "how many customers are in view right now" at sampled instants
  stops        t_enter,t_exit,zone           every time someone stood still at a display (>= the engage threshold)
  walks        start,end,people              staged walks by volunteers who start at the door
  empty runs   (run ids)                     footage of the EMPTY store (night): everything detected is a false alarm
  demo_labels  start,end,age_band,gender,count   human-labelled apparent age band / perceived gender of door visitors
  demo_pairs   label_age,label_gender,est_age,est_gender   a staged volunteer session scored person by person

Times are epoch seconds or ISO 8601 (naive = the store's timezone). Statuses: PASS (point estimate and the whole 95%
interval inside the gate), UNCERTAIN (point estimate inside, interval crosses the gate: collect more), FAIL, and
INSUFFICIENT (fewer samples than the minimum written in the gates).
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import yaml
from scipy.stats import chi2

from .audience import wilson
from .config import StoreConfig
from .queries import Analytics, Window
from .store import EventStore

# Starting gates from claude/storelens-first-customer-backend-legal-validation-oct-2026.md section 6.
# Tune them WITH the customer, commit the file, then run: that is the "pre-registration".
DEFAULT_GATES: dict = {
    "door_min_windows": 30, "door_error_max_pct": 5.0, "door_bias_max_pct": 3.0,
    "crowd_levels": [[1, 3], [4, 8], [9, None]], "crowd_min_per_level": 30, "crowd_bias_max_pct": 10.0,
    "stops_min": 100, "stops_miss_max": 0.10, "stops_wrong_zone_max": 0.05,
    "walks_min_people": 30, "walks_dup_max": 0.03, "walks_miss_max": 0.05,
    "empty_min_hours": 8.0, "empty_false_per_hour_max": 0.25,
    "demo_min_labelled": 200, "demo_tvd_max": 0.10, "demo_female_share_abs_max": 0.08,
    "demo_min_pairs": 200, "demo_age_within_one_min": 0.85, "demo_gender_min": 0.85, "demo_subgroup_gap_max": 0.10,
}
ORDER = {"FAIL": 0, "UNCERTAIN": 1, "INSUFFICIENT": 2, "PASS": 3}


# ------------------------------------------------------------------------------------------------ helpers
def parse_time(s: str, tz: str) -> float:
    s = str(s).strip()
    try:
        return float(s)
    except ValueError:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt.timestamp()


def read_rows(path: str | Path) -> list[dict]:
    """CSV with a header row; blank lines and lines starting with '#' are ignored."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        lines = [r for r in csv.reader(fh) if r and any(c.strip() for c in r) and not r[0].lstrip().startswith("#")]
    if not lines:
        return []
    head = [c.strip().lower() for c in lines[0]]
    return [{h: (c or "").strip() for h, c in zip(head, r)} for r in lines[1:]]


def gates_digest(gates: dict) -> str:
    return hashlib.sha256(json.dumps(gates, sort_keys=True).encode()).hexdigest()[:12]


def judge(point: float, ci: tuple[float, float] | None, limit: float, n: int, n_min: int, two_sided: bool = True) -> str:
    """Status of a quantity that has to stay within ±limit (two_sided) or at or below it."""
    if n < n_min:
        return "INSUFFICIENT"
    if point != point:
        return "INSUFFICIENT"
    lo, hi = ci if ci else (point, point)
    pt = abs(point) if two_sided else point
    worst = max(abs(lo), abs(hi)) if two_sided else hi
    if pt > limit:
        return "FAIL"
    return "UNCERTAIN" if worst > limit else "PASS"


def worst(statuses: list[str]) -> str:
    return min(statuses, key=lambda s: ORDER[s]) if statuses else "INSUFFICIENT"


def _boot(stat, n: int, rng: np.random.Generator, reps: int = 2000) -> tuple[float, float]:
    idx = rng.integers(0, n, (reps, n))
    v = stat(idx)
    v = v[np.isfinite(v)]
    return (float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))) if len(v) else (float("nan"), float("nan"))


def _intervals(store: EventStore, run_ids: list[int] | None) -> list[tuple[float, float]]:
    rows = store.query("SELECT run_id,t_start,t_end FROM runs ORDER BY t_start")
    iv = sorted((t0, t1 if t1 else t0) for rid, t0, t1 in rows if not run_ids or rid in run_ids)
    merged: list[list[float]] = []
    for a, b in iv:
        if merged and a <= merged[-1][1] + 2.0:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _covered(t0: float, t1: float, iv: list[tuple[float, float]]) -> bool:
    return any(a - 1.0 <= t0 and t1 <= b + 1.0 for a, b in iv)


def _ci_pct(pair) -> list[float]:
    return [round(100 * pair[0], 2), round(100 * pair[1], 2)]


# ------------------------------------------------------------------------------------------------ the checks
def check_door(an: Analytics, run_ids, rows: list[dict], tz: str, g: dict, rng) -> dict:
    iv = _intervals(an.store, run_ids)
    ent = np.array(sorted(t for t, d in an._entries(Window(run_ids)) if d > 0), float)
    man, pl, skipped = [], [], 0
    for r in rows:
        t0, t1 = parse_time(r["start"], tz), parse_time(r["end"], tz)
        if not _covered(t0, t1, iv):
            skipped += 1
            continue
        man.append(float(r.get("count_in") or r.get("count") or 0))
        pl.append(float(np.searchsorted(ent, t1, "left") - np.searchsorted(ent, t0, "left")))
    man, pl = np.asarray(man), np.asarray(pl)
    n = len(man)
    out = {"name": "door_count", "n": n, "skipped_outside_footage": skipped, "gates": {
        "window_error_max_pct": g["door_error_max_pct"], "bias_max_pct": g["door_bias_max_pct"], "min_windows": g["door_min_windows"]}}
    if n == 0 or man.sum() == 0:
        return {**out, "status": "INSUFFICIENT", "summary": "no usable tally windows (all outside the processed footage?)"}
    bias = float((pl.sum() - man.sum()) / man.sum() * 100)
    wape = float(np.abs(pl - man).sum() / man.sum() * 100)
    bias_ci = _boot(lambda i: (pl[i].sum(1) - man[i].sum(1)) / man[i].sum(1) * 100, n, rng)
    wape_ci = _boot(lambda i: np.abs(pl[i] - man[i]).sum(1) / man[i].sum(1) * 100, n, rng)
    rel = (pl - man)[man > 0] / man[man > 0]
    sd = float(rel.std(ddof=1)) if len(rel) > 1 else float("nan")
    need = int(math.ceil((1.96 * sd / 0.02) ** 2)) if sd == sd else None
    st_bias = judge(bias, bias_ci, g["door_bias_max_pct"], n, g["door_min_windows"])
    st_err = judge(wape, wape_ci, g["door_error_max_pct"], n, g["door_min_windows"], two_sided=False)
    return {**out, "status": worst([st_bias, st_err]), "status_bias": st_bias, "status_window_error": st_err,
            "manual_total": int(man.sum()), "pipeline_total": int(pl.sum()), "bias_pct": round(bias, 2),
            "bias_ci_pct": [round(bias_ci[0], 2), round(bias_ci[1], 2)], "window_error_pct": round(wape, 2),
            "window_error_ci_pct": [round(wape_ci[0], 2), round(wape_ci[1], 2)],
            "exact_windows": int((pl == man).sum()), "windows_for_pm2pts": need,
            "summary": f"{n} windows, manual {int(man.sum())} vs pipeline {int(pl.sum())}: bias {bias:+.1f}% "
                       f"[{bias_ci[0]:+.1f}, {bias_ci[1]:+.1f}], window error {wape:.1f}% (95% CI up to {wape_ci[1]:.1f}%)"
                       + (f"; about {need} windows would pin the bias to ±2 points" if need else "")}


def check_crowd(an: Analytics, run_ids, rows: list[dict], tz: str, g: dict, rng) -> dict:
    iv = _intervals(an.store, run_ids)
    vis = an._visits(Window(run_ids))
    tf = np.array([v[2] for v in vis], float)
    tl = tf + np.array([v[3] for v in vis], float)
    snaps = []
    for r in rows:
        t = parse_time(r["t"], tz)
        if _covered(t, t, iv):
            snaps.append((float(r.get("people") or r.get("count") or 0), float(((tf <= t) & (tl >= t)).sum())))
    levels = []
    for lo, hi in g["crowd_levels"]:
        sel = [(m, p) for m, p in snaps if m >= lo and (hi is None or m <= hi)]
        label = f"{lo}-{hi}" if hi is not None else f"{lo}+"
        if not sel:
            levels.append({"level": label, "n": 0, "status": "INSUFFICIENT"})
            continue
        man, pl = np.array([s[0] for s in sel]), np.array([s[1] for s in sel])
        bias = float((pl.sum() - man.sum()) / man.sum() * 100)
        ci = _boot(lambda i: (pl[i].sum(1) - man[i].sum(1)) / man[i].sum(1) * 100, len(man), rng)
        levels.append({"level": label, "n": len(man), "manual": int(man.sum()), "pipeline": int(pl.sum()),
                       "bias_pct": round(bias, 2), "bias_ci_pct": [round(ci[0], 2), round(ci[1], 2)],
                       "direction": "undercounts" if bias < -1 else ("overcounts" if bias > 1 else "balanced"),
                       "status": judge(bias, ci, g["crowd_bias_max_pct"], len(man), g["crowd_min_per_level"])})
    empty = [(m, p) for m, p in snaps if m == 0]
    return {"name": "crowd_counts", "n": len(snaps), "levels": levels,
            "empty_view_snapshots": len(empty), "empty_view_false_people": int(sum(p for _, p in empty)),
            "status": worst([lv["status"] for lv in levels]),
            "gates": {"bias_max_pct": g["crowd_bias_max_pct"], "min_per_level": g["crowd_min_per_level"]},
            "summary": "; ".join(f"{lv['level']} people: n={lv['n']}" + (f", {lv['bias_pct']:+.1f}% ({lv['direction']})"
                                                                           if lv.get("n") else "") for lv in levels)}


def check_stops(an: Analytics, cfg: StoreConfig, run_ids, rows: list[dict], tz: str, g: dict, exhaustive: bool,
                tol_s: float = 2.0) -> dict:
    iv = _intervals(an.store, run_ids)
    stop_an = Analytics(an.store, cfg)
    stop_an.mode = "stop"  # annotators mark who STOOD STILL; facing is not visible to them either
    pr = [{"zone": z, "t0": te, "t1": tx} for _, _, z, te, tx, _, _, eng in stop_an._zone_rows(Window(run_ids)) if eng]
    labels = []
    skipped = 0
    for r in rows:
        t0, t1 = parse_time(r["t_enter"], tz), parse_time(r["t_exit"], tz)
        if _covered(t0, t1, iv):
            labels.append({"zone": r["zone"], "t0": t0, "t1": t1})
        else:
            skipped += 1
    n = len(labels)
    out = {"name": "zone_stops", "n": n, "skipped_outside_footage": skipped,
           "gates": {"miss_max": g["stops_miss_max"], "wrong_zone_max": g["stops_wrong_zone_max"], "min_labelled": g["stops_min"]}}
    if n == 0:
        return {**out, "status": "INSUFFICIENT", "summary": "no labelled stops inside the processed footage"}

    def overlap(a, b):
        return min(a["t1"], b["t1"]) - max(a["t0"], b["t0"]) + tol_s

    def assign(pairs, used_l, used_p):
        for _, i, j in sorted(pairs, reverse=True):
            if i not in used_l and j not in used_p:
                used_l.add(i)
                used_p.add(j)

    used_l: set[int] = set()
    used_p: set[int] = set()
    assign([(overlap(l, p), i, j) for i, l in enumerate(labels) for j, p in enumerate(pr)
            if l["zone"] == p["zone"] and overlap(l, p) > 0], used_l, used_p)
    matched = len(used_l)
    # A stop the pipeline put at ANOTHER display counts as wrong-zone only if it is otherwise unexplained (no label of
    # its own) and the labels are exhaustive; otherwise it may simply be somebody else's unlabelled stop.
    wrong_l: set[int] = set()
    if exhaustive:
        w_l, w_p = set(used_l), set(used_p)
        assign([(overlap(l, p), i, j) for i, l in enumerate(labels) for j, p in enumerate(pr)
                if i not in used_l and j not in used_p and overlap(l, p) > 0], w_l, w_p)
        wrong_l = w_l - used_l
    wrong = len(wrong_l)
    missed = n - matched - wrong
    rec_ci, wr_ci, ms_ci = wilson(matched, n), wilson(wrong, n), wilson(missed, n)
    if exhaustive:
        st_miss = judge(missed / n, ms_ci, g["stops_miss_max"], n, g["stops_min"], False)
        st_wrong = judge(wrong / n, wr_ci, g["stops_wrong_zone_max"], n, g["stops_min"], False)
    else:  # cannot tell a missed stop from a wrong-zone one: gate the two together
        st_miss = judge(missed / n, ms_ci, g["stops_miss_max"] + g["stops_wrong_zone_max"], n, g["stops_min"], False)
        st_wrong = "PASS"
    res = {**out, "status": worst([st_miss, st_wrong]), "matched": matched, "wrong_zone": wrong if exhaustive else None,
           "missed": missed, "recall": round(matched / n, 4), "recall_ci": [round(rec_ci[0], 4), round(rec_ci[1], 4)],
           "wrong_zone_rate": round(wrong / n, 4) if exhaustive else None,
           "wrong_zone_ci": [round(wr_ci[0], 4), round(wr_ci[1], 4)] if exhaustive else None}
    extra = ""
    if exhaustive and labels:
        lo, hi = min(l["t0"] for l in labels), max(l["t1"] for l in labels)
        span = [j for j, p in enumerate(pr) if p["t1"] >= lo and p["t0"] <= hi]
        explained = len([j for j in span if j in used_p]) + wrong
        extra_n = len(span) - explained
        prec = explained / len(span) if span else float("nan")
        res.update({"exhaustive": True, "pipeline_stops_in_span": len(span), "unlabelled_pipeline_stops": extra_n,
                    "precision": round(prec, 4) if prec == prec else None})
        extra = f"; precision {prec:.2f} ({extra_n} pipeline stops nobody labelled)"
    by_zone: dict[str, list[int]] = {}
    for i, l in enumerate(labels):
        by_zone.setdefault(l["zone"], [0, 0])[0] += 1
        by_zone[l["zone"]][1] += i in used_l
    res["by_zone"] = {z: {"labelled": a, "matched": b} for z, (a, b) in sorted(by_zone.items())}
    res["summary"] = (f"{n} labelled stops: found {matched} ({matched / n:.0%}, CI {rec_ci[0]:.0%}-{rec_ci[1]:.0%}), "
                      + (f"attributed to the wrong display {wrong} ({wrong / n:.1%}), missed {missed}" if exhaustive else
                         f"not found at that display {missed} (missed or wrong display: label every stop and set stops_exhaustive to split them)")
                      + extra)
    return res


def check_walks(an: Analytics, run_ids, rows: list[dict], tz: str, g: dict) -> dict:
    iv = _intervals(an.store, run_ids)
    vis = an._visits(Window(run_ids))
    spans = [(v[2], v[2] + v[3]) for v in vis]
    man_t = dup_t = miss_t = n_win = skipped = 0
    for r in rows:
        t0, t1 = parse_time(r["start"], tz), parse_time(r["end"], tz)
        if not _covered(t0, t1, iv):
            skipped += 1
            continue
        people = int(float(r.get("people") or r.get("count") or 0))
        got = sum(1 for a, b in spans if min(b, t1) - max(a, t0) > 1.0)
        man_t += people
        dup_t += max(0, got - people)
        miss_t += max(0, people - got)
        n_win += 1
    out = {"name": "staged_walks", "n": man_t, "windows": n_win, "skipped_outside_footage": skipped,
           "gates": {"duplicate_max": g["walks_dup_max"], "miss_max": g["walks_miss_max"], "min_people": g["walks_min_people"]}}
    if not man_t:
        return {**out, "status": "INSUFFICIENT", "summary": "no staged-walk windows inside the processed footage"}
    d_ci, m_ci = wilson(dup_t, man_t), wilson(miss_t, man_t)
    st_d = judge(dup_t / man_t, d_ci, g["walks_dup_max"], man_t, g["walks_min_people"], False)
    st_m = judge(miss_t / man_t, m_ci, g["walks_miss_max"], man_t, g["walks_min_people"], False)
    return {**out, "status": worst([st_d, st_m]), "duplicates": dup_t, "missed": miss_t,
            "duplicate_rate": round(dup_t / man_t, 4), "duplicate_ci": [round(d_ci[0], 4), round(d_ci[1], 4)],
            "miss_rate": round(miss_t / man_t, 4), "miss_ci": [round(m_ci[0], 4), round(m_ci[1], 4)],
            "summary": f"{man_t} walkers in {n_win} windows: {dup_t} extra tracks ({dup_t / man_t:.1%}, CI up to {d_ci[1]:.1%}), "
                       f"{miss_t} not counted ({miss_t / man_t:.1%})"}


def check_empty(store: EventStore, run_ids: list[int], g: dict) -> dict:
    hours, vis, zv, ent = 0.0, 0, 0, 0
    zones: dict[str, int] = {}
    for rid in run_ids:
        t0, t1 = store.query("SELECT t_start, COALESCE(t_end, t_start) FROM runs WHERE run_id=?", (rid,))[0]
        hours += max(t1 - t0, 0.0) / 3600
        vis += store.query("SELECT COUNT(*) FROM visits WHERE run_id=?", (rid,))[0][0]
        ent += store.query("SELECT COUNT(*) FROM entries WHERE run_id=? AND direction=1", (rid,))[0][0]
        for z, c in store.query("SELECT zone_id,COUNT(*) FROM zone_visits WHERE run_id=? GROUP BY 1", (rid,)):
            zones[z] = zones.get(z, 0) + c
            zv += c
    out = {"name": "empty_store", "n": vis, "hours": round(hours, 2), "false_visits": vis, "false_zone_visits": zv,
           "false_entries": ent, "false_zone_visits_by_zone": dict(sorted(zones.items(), key=lambda kv: -kv[1])),
           "gates": {"false_per_hour_max": g["empty_false_per_hour_max"], "min_hours": g["empty_min_hours"]}}
    if hours <= 0:
        return {**out, "status": "INSUFFICIENT", "summary": "empty-store runs have no duration"}
    rate = vis / hours
    upper = float(chi2.ppf(0.975, 2 * (vis + 1)) / 2 / hours)  # exact Poisson 95% upper bound
    if hours < g["empty_min_hours"]:
        status = "INSUFFICIENT"
    elif rate > g["empty_false_per_hour_max"]:
        status = "FAIL"
    else:
        status = "PASS" if upper <= g["empty_false_per_hour_max"] else "UNCERTAIN"
    return {**out, "status": status, "false_per_hour": round(rate, 3), "false_per_hour_upper95": round(upper, 3),
            "summary": f"{hours:.1f} h of empty store: {vis} tracks, {zv} zone visits, {ent} door entries "
                       f"({rate:.2f}/h, 95% upper bound {upper:.2f}/h)"
                       + (" - zero events needs about 12 h to show <0.25/h" if vis == 0 and status != "PASS" else "")}


def check_demo_labels(store: EventStore, cfg: StoreConfig, run_ids, rows: list[dict], tz: str, g: dict) -> dict:
    """Population-level check of the stored audience mix against a human-labelled sample (no per-person data needed)."""
    bands = list(cfg.demographics.age_bands)
    lab_age = {b: 0 for b in bands}
    lab_g = {"female": 0, "male": 0}
    windows = []
    for r in rows:
        t0, t1 = parse_time(r["start"], tz), parse_time(r["end"], tz)
        windows.append((t0, t1))
        n = int(float(r.get("count") or 1))
        if r.get("age_band") in lab_age:
            lab_age[r["age_band"]] += n
        if r.get("gender") in lab_g:
            lab_g[r["gender"]] += n
    n_age, n_g = sum(lab_age.values()), sum(lab_g.values())
    out = {"name": "audience_mix", "n": n_age, "gates": {"tvd_max": g["demo_tvd_max"], "female_share_abs_max": g["demo_female_share_abs_max"],
                                                           "min_labelled": g["demo_min_labelled"]}}
    span = cfg.demographics.bucket_hours * 3600
    sys_age = {b: 0 for b in bands}
    sys_g = {"female": 0, "male": 0}
    sys_unknown = sys_total = 0
    where = "metric='visitor' AND zone_id=''" + (f" AND run_id IN ({','.join('?' * len(run_ids))})" if run_ids else "")
    for b, age, gen, n in store.query(f"SELECT bucket_ts,age_band,gender,SUM(n) FROM audience WHERE {where} GROUP BY 1,2,3",
                                      tuple(run_ids or ())):
        if not any(b < t1 and b + span > t0 for t0, t1 in windows):
            continue
        sys_total += n
        if age in sys_age:
            sys_age[age] += n
        else:
            sys_unknown += n
        if gen in sys_g:
            sys_g[gen] += n
    if n_age == 0 or sum(sys_age.values()) == 0:
        return {**out, "status": "INSUFFICIENT", "summary": "no labelled people, or no stored audience buckets overlap the labelled windows"}
    la = {b: v / n_age for b, v in lab_age.items()}
    sa = {b: v / sum(sys_age.values()) for b, v in sys_age.items()}
    tvd = 0.5 * sum(abs(la[b] - sa[b]) for b in bands)
    per_band = {b: {"labelled": round(la[b], 4), "labelled_ci": [round(x, 4) for x in wilson(lab_age[b], n_age)],
                    "system": round(sa[b], 4)} for b in bands}
    inconsistent = [b for b in bands if not (per_band[b]["labelled_ci"][0] <= sa[b] <= per_band[b]["labelled_ci"][1])]
    lf = lab_g["female"] / n_g if n_g else float("nan")
    sf = sys_g["female"] / max(sum(sys_g.values()), 1)
    f_ci = wilson(lab_g["female"], n_g) if n_g else (0.0, 1.0)
    diff = sf - lf
    st_t = judge(tvd, None, g["demo_tvd_max"], n_age, g["demo_min_labelled"], False)
    st_f = judge(diff, (sf - f_ci[1], sf - f_ci[0]), g["demo_female_share_abs_max"], n_g, g["demo_min_labelled"])
    st = worst([st_t, st_f])
    if st == "PASS" and inconsistent:
        st = "UNCERTAIN"
    return {**out, "status": st, "tvd": round(tvd, 4), "per_band": per_band, "bands_outside_label_ci": inconsistent,
            "female_share_labelled": round(lf, 4), "female_share_system": round(sf, 4),
            "female_share_diff": round(diff, 4), "system_visitors": sys_total,
            "system_unknown_share": round(sys_unknown / sys_total, 4) if sys_total else None,
            "summary": f"labelled {n_age} people vs {sys_total} stored visitors: age-mix TVD {tvd:.2f}, female share "
                       f"{lf:.2f} labelled vs {sf:.2f} system" + (f"; outside the labelled interval: {', '.join(inconsistent)}" if inconsistent else "")}


def check_demo_pairs(cfg: StoreConfig, rows: list[dict], g: dict) -> dict:
    """Person-by-person confusion from a STAGED volunteer session (consent first; delete the pairs file afterwards)."""
    bands = list(cfg.demographics.age_bands)
    order = {b: i for i, b in enumerate(bands)}
    n = len(rows)
    out = {"name": "audience_accuracy", "n": n, "gates": {"age_within_one_min": g["demo_age_within_one_min"],
                                                           "gender_min": g["demo_gender_min"], "min_pairs": g["demo_min_pairs"]}}
    known_a = [r for r in rows if r.get("est_age") in order and r.get("label_age") in order]
    known_g = [r for r in rows if r.get("est_gender") in ("female", "male") and r.get("label_gender") in ("female", "male")]
    if not n or not known_a:
        return {**out, "status": "INSUFFICIENT", "summary": "no usable label/estimate pairs"}
    exact = sum(r["est_age"] == r["label_age"] for r in known_a)
    near = sum(abs(order[r["est_age"]] - order[r["label_age"]]) <= 1 for r in known_a)
    gc = sum(r["est_gender"] == r["label_gender"] for r in known_g)
    confusion = {b: {c: 0 for c in bands} for b in bands}
    for r in known_a:
        confusion[r["label_age"]][r["est_age"]] += 1
    per_band = {b: {"n": sum(confusion[b].values()), "recall": round(confusion[b][b] / sum(confusion[b].values()), 3)
                    if sum(confusion[b].values()) else None} for b in bands}
    per_gender = {}
    for gen in ("female", "male"):
        sub = [r for r in known_g if r["label_gender"] == gen]
        per_gender[gen] = {"n": len(sub), "recall": round(sum(r["est_gender"] == gen for r in sub) / len(sub), 3) if sub else None}
    gaps = [abs(per_gender["female"]["recall"] - per_gender["male"]["recall"])] if all(v["recall"] is not None for v in per_gender.values()) else []
    n_ok = min(len(known_a), len(known_g)) if known_g else 0
    ci_near, ci_gen = wilson(near, len(known_a)), wilson(gc, len(known_g)) if known_g else (0.0, 1.0)
    st = [judge(1 - near / len(known_a), (1 - ci_near[1], 1 - ci_near[0]), 1 - g["demo_age_within_one_min"], len(known_a), g["demo_min_pairs"], False),
          judge(1 - gc / max(len(known_g), 1), (1 - ci_gen[1], 1 - ci_gen[0]), 1 - g["demo_gender_min"], len(known_g), g["demo_min_pairs"], False)]
    if gaps and max(gaps) > g["demo_subgroup_gap_max"] and n_ok >= g["demo_min_pairs"]:
        st.append("FAIL")
    unknown = 1 - len(known_a) / n
    return {**out, "status": worst(st), "coverage_age": round(1 - unknown, 3), "age_exact": round(exact / len(known_a), 3),
            "age_within_one_band": round(near / len(known_a), 3), "age_within_one_ci": [round(ci_near[0], 3), round(ci_near[1], 3)],
            "gender_correct": round(gc / len(known_g), 3) if known_g else None,
            "gender_ci": [round(ci_gen[0], 3), round(ci_gen[1], 3)], "per_band": per_band, "per_gender": per_gender,
            "confusion_age": confusion,
            "summary": f"{n} volunteers, estimate available for {1 - unknown:.0%}: age within one band {near / len(known_a):.0%} "
                       f"(exact {exact / len(known_a):.0%}), gender {gc / max(len(known_g), 1):.0%}"
                       + (f"; female/male recall gap {max(gaps):.0%}" if gaps else "")}


# ------------------------------------------------------------------------------------------------ driver
def load_spec(path: str | Path) -> dict:
    spec = yaml.safe_load(Path(path).read_text()) or {}
    spec["_dir"] = str(Path(path).resolve().parent)
    spec["gates"] = {**DEFAULT_GATES, **(spec.get("gates") or {})}
    return spec


def run_fieldcheck(cfg: StoreConfig, store: EventStore, spec: dict, seed: int = 0) -> dict:
    g, tz, base = spec["gates"], cfg.output.timezone, Path(spec["_dir"])
    rng = np.random.default_rng(seed)
    empty = [int(x) for x in spec.get("empty_runs") or []]
    run_ids = [int(x) for x in spec["runs"]] if spec.get("runs") else \
        [r[0] for r in store.query("SELECT run_id FROM runs ORDER BY run_id") if r[0] not in empty]
    an = Analytics(store, cfg)
    checks: list[dict] = []

    def rows(key: str) -> list[dict] | None:
        return read_rows(base / spec[key]) if spec.get(key) else None

    if (r := rows("door_tally")) is not None:
        checks.append(check_door(an, run_ids, r, tz, g, rng))
    if (r := rows("crowd")) is not None:
        checks.append(check_crowd(an, run_ids, r, tz, g, rng))
    if (r := rows("stops")) is not None:
        checks.append(check_stops(an, cfg, run_ids, r, tz, g, bool(spec.get("stops_exhaustive", False))))
    if (r := rows("walks")) is not None:
        checks.append(check_walks(an, run_ids, r, tz, g))
    if empty:
        checks.append(check_empty(store, empty, g))
    if cfg.demographics.enabled:
        if (r := rows("demo_labels")) is not None:
            checks.append(check_demo_labels(store, cfg, run_ids, r, tz, g))
        if (r := rows("demo_pairs")) is not None:
            checks.append(check_demo_pairs(cfg, r, g))
    return {"layout_version": cfg.layout_version, "runs": run_ids, "empty_runs": empty, "gates": g,
            "gates_sha": gates_digest(g), "checks": checks,
            "overall": worst([c["status"] for c in checks]) if checks else "INSUFFICIENT",
            "caveat": "Labels are only as good as the annotators: have a second person label a subset and report their "
                      "agreement. Judge on held-out days chosen BEFORE seeing the results."}


def format_fieldcheck(r: dict) -> str:
    L = [f"=== Field check (gates {r['gates_sha']}, layout {r['layout_version']}, runs {r['runs']}"
         + (f", empty-store runs {r['empty_runs']}" if r["empty_runs"] else "") + ") ==="]
    for c in r["checks"]:
        L.append(f"[{c['status']:<12}] {c['name']}: {c['summary']}")
        for lv in c.get("levels", []):
            if lv.get("n"):
                L.append(f"{'':16}level {lv['level']:<5} n={lv['n']:<4} bias {lv['bias_pct']:+.1f}% "
                         f"[{lv['bias_ci_pct'][0]:+.1f}, {lv['bias_ci_pct'][1]:+.1f}]  {lv['direction']}  -> {lv['status']}")
    L.append("")
    L.append(f"OVERALL: {r['overall']}   ({r['caveat']})")
    return "\n".join(L)


TEMPLATES = {
    "door_tally.csv": "start,end,count_in\n# one row per tally window, e.g. a full hour; times in the store's timezone\n"
                      "2026-11-14T10:00,2026-11-14T11:00,0\n",
    "crowd.csv": "t,people\n# at each sampled instant (freeze the frame): how many CUSTOMERS are visible? exclude staff\n"
                 "2026-11-14T10:07:30,0\n",
    "stops.csv": "t_enter,t_exit,zone\n# every time someone stood still at a display for at least the engage threshold "
                 "(see `storelens cameras`); label ALL of them inside the sampled stretch\n2026-11-14T10:12:05,2026-11-14T10:12:21,front_table\n",
    "walks.csv": "start,end,people\n# staged walks: volunteers enter through the door, one group per window, windows apart\n"
                 "2026-11-14T07:30,2026-11-14T07:34,3\n",
    "demo_labels.csv": "start,end,age_band,gender,count\n# a tally of everyone who entered in the window: apparent age band and "
                       "perceived gender (one row per combination)\n2026-11-14T10:00,2026-11-14T11:00,25-44,female,7\n",
    "demo_pairs.csv": "label_age,label_gender,est_age,est_gender\n# STAGED volunteer session only, with written consent; "
                      "delete this file after scoring\n25-44,female,25-44,female\n",
    "fieldcheck.yaml": "# Commit this file BEFORE looking at any result. Tune the gates with the customer.\n"
                       "runs: []            # run ids to score (empty = all except empty_runs)\n"
                       "empty_runs: []      # run ids of footage of the empty store\n"
                       "door_tally: door_tally.csv\ncrowd: crowd.csv\nstops: stops.csv\nstops_exhaustive: false\n"
                       "walks: walks.csv\n# demo_labels: demo_labels.csv\n# demo_pairs: demo_pairs.csv\n"
                       "gates:\n  door_min_windows: 30\n  door_error_max_pct: 5.0\n  door_bias_max_pct: 3.0\n",
}


def write_templates(out: str | Path) -> list[str]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    names = []
    for name, text in TEMPLATES.items():
        p = out / name
        if not p.exists():
            p.write_text(text)
            names.append(name)
    return names
