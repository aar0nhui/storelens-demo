"""Validate pipeline output against simulator ground truth (the credibility slide).

Ground truth comes from the simulator's script: every simulated shopper has an *intent* at each display
(browse / stop-and-look-away / glance / pass). The TARGET for "engaged" is "this visit overlapped a scripted
browse at that display". We score three things against it:

  stop only             pipeline: stopped >= min_engage_s inside the zone        (default engagement_mode)
  stop + motion facing  pipeline: also heading (held while stationary) toward the display
  ceiling (stop only)   the SAME stop-only rule applied to the TRUE trajectories: what perfect perception
                        could ever reach with this definition (look-away stops are indistinguishable)

Pipeline vs ceiling isolates perception error (detection/tracking/calibration/staff filter) from
definition error (people who stop but don't look).
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from .analysis import analyze_samples, is_engaged
from .config import StoreConfig
from .store import EventStore


def load_gt(path: str | Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def gt_events(gt: dict, cfg: StoreConfig, t_start: float) -> list[dict]:
    """Run the analytics on every true trajectory."""
    fps = gt["fps"]
    out = []
    for p in gt["people"]:
        n = len(p["x"])
        t = t_start + (p["start_frame"] + np.arange(n)) / fps
        xy = np.stack([p["x"], p["y"]], axis=1)
        a = analyze_samples(t, xy, cfg, facing_override=np.asarray(p["h"]), compute_heat=(p["role"] == "customer"))
        browse = [g for g in p.get("script", []) if g.get("kind") == "browse"]
        intents = []
        for z in a.zone_visits:  # script times are relative to clip start
            te, tx = z.t_enter - t_start, z.t_exit - t_start
            intents.append(any(g["fixture"] == z.zone_id and g["t0"] <= tx and g["t1"] >= te for g in browse))
        out.append({"id": p["id"], "role": p["role"], "a": a, "n": n, "intent": intents})
    return out


def _iou(a0, a1, b0, b1) -> float:
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = (a1 - a0) + (b1 - b0) - inter
    return inter / union if union > 0 else 0.0


def _match(gt_rows: list[dict], pr_rows: list[dict], min_iou: float = 0.15):
    """Hungarian match by temporal IoU. Returns list of (gt_idx, pr_idx)."""
    if not gt_rows or not pr_rows:
        return []
    cost = np.ones((len(gt_rows), len(pr_rows)))
    for i, g in enumerate(gt_rows):
        for j, p in enumerate(pr_rows):
            cost[i, j] = 1.0 - _iou(g["t0"], g["t1"], p["t0"], p["t1"])
    r, c = linear_sum_assignment(cost)
    return [(i, j) for i, j in zip(r, c) if 1.0 - cost[i, j] >= min_iou]


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f = 2 * p * r / (p + r) if p == p and r == r and (p + r) > 0 else float("nan")
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f}


def _pct_err(pred: float, truth: float) -> float:
    return (pred - truth) / truth * 100.0 if truth else float("nan")


def validate(cfg: StoreConfig, store: EventStore, run_id: int, gt: dict) -> dict:
    row = store.query("SELECT t_start, meta FROM runs WHERE run_id=?", (run_id,))[0]
    t_start, meta = float(row[0]), json.loads(row[1] or "{}")
    ev = gt_events(gt, cfg, t_start)
    thr = cfg.thresholds

    # ----- GT / pipeline zone visits ------------------------------------------------------------
    gt_zv: dict[str, list[dict]] = {}
    for person in ev:
        for z, intent in zip(person["a"].zone_visits, person["intent"]):
            gt_zv.setdefault(z.zone_id, []).append({"t0": z.t_enter, "t1": z.t_exit, "z": z, "intent": intent,
                                                    "role": person["role"], "pid": person["id"]})
    pr_zv: dict[str, list[dict]] = {}
    for zid, t0, t1, dwell, stopped, fstop in store.query(
            "SELECT zone_id,t_enter,t_exit,dwell_s,stopped_s,facing_stopped_s FROM zone_visits WHERE run_id=?",
            (run_id,)):
        pr_zv.setdefault(zid, []).append({"t0": t0, "t1": t1, "dwell": dwell, "stopped": stopped, "fstop": fstop})

    mode = thr.engagement_mode
    zones = {}
    tot = {"impr_gt": 0, "impr_pr": 0, "leak": 0, "ghost": 0}
    conf = {k: {"tp": 0, "fp": 0, "fn": 0} for k in ("mode", "stop", "stop_facing", "ceiling")}
    cnt = {"gt": 0, "mode": 0}
    dwell_err: list[float] = []
    for z in cfg.zones:
        if z.kind == "staff":
            continue
        g_all = gt_zv.get(z.id, [])
        g_cust = [g for g in g_all if g["role"] == "customer"]
        pr = pr_zv.get(z.id, [])
        pairs = _match(g_all, pr)  # match against ALL GT visits so staff leakage is attributable
        used_g = {i for i, _ in pairs}
        used_p = {j for _, j in pairs}
        tp_visit = [(i, j) for i, j in pairs if g_all[i]["role"] == "customer"]
        leak = [(i, j) for i, j in pairs if g_all[i]["role"] == "staff"]
        ghosts = [j for j in range(len(pr)) if j not in used_p]
        entry = {"name": z.name, "kind": z.kind,
                 "impressions": {"gt": len(g_cust), "pipeline": len(pr), "error_pct": _pct_err(len(pr), len(g_cust)),
                                 "matched": len(tp_visit), "staff_leak": len(leak), "ghosts": len(ghosts)}}
        tot["impr_gt"] += len(g_cust); tot["impr_pr"] += len(pr); tot["leak"] += len(leak); tot["ghost"] += len(ghosts)
        if z.kind == "fixture":
            min_s = cfg.engage_seconds(z)

            def tgt(g):  # target: the shopper really intended to browse this display
                return g["intent"]

            def variant(p, name):
                return is_engaged(p["stopped"], p["fstop"], min_s, {"mode": mode, "stop": "stop",
                                                                      "stop_facing": "stop_facing"}[name])
            res = {}
            for name in ("mode", "stop", "stop_facing"):
                tp = sum(1 for i, j in tp_visit if tgt(g_all[i]) and variant(pr[j], name))
                fp = sum(1 for i, j in tp_visit if not tgt(g_all[i]) and variant(pr[j], name))
                fn = sum(1 for i, j in tp_visit if tgt(g_all[i]) and not variant(pr[j], name))
                fn += sum(1 for i, g in enumerate(g_all) if g["role"] == "customer" and i not in used_g and tgt(g))
                fp += sum(1 for j in ghosts if variant(pr[j], name)) + sum(1 for i, j in leak if variant(pr[j], name))
                res[name] = _prf(tp, fp, fn)
                for k, v in (("tp", tp), ("fp", fp), ("fn", fn)):
                    conf[name][k] += v
            ceil = _prf(
                sum(1 for g in g_cust if tgt(g) and g["z"].stopped_s >= min_s),
                sum(1 for g in g_cust if not tgt(g) and g["z"].stopped_s >= min_s),
                sum(1 for g in g_cust if tgt(g) and g["z"].stopped_s < min_s))
            for k in ("tp", "fp", "fn"):
                conf["ceiling"][k] += ceil[k]
            gt_eng = sum(1 for g in g_cust if tgt(g))
            pr_eng = sum(1 for p in pr if variant(p, "mode"))
            cnt["gt"] += gt_eng; cnt["mode"] += pr_eng
            for i, j in tp_visit:
                if tgt(g_all[i]) and variant(pr[j], "mode"):
                    dwell_err.append(abs(pr[j]["stopped" if mode == "stop" else "fstop"] -
                                         (g_all[i]["z"].stopped_s if mode == "stop" else g_all[i]["z"].facing_stopped_s)))
            entry["engaged"] = {"gt": gt_eng, "pipeline": pr_eng, "error_pct": _pct_err(pr_eng, gt_eng), **res["mode"]}
            entry["engaged_stop_only"] = res["stop"]
            entry["engaged_stop_facing"] = res["stop_facing"]
            entry["engaged_ceiling"] = ceil
            entry["engagement_rate"] = {"gt": gt_eng / len(g_cust) if g_cust else float("nan"),
                                        "pipeline": pr_eng / len(pr) if pr else float("nan")}
        zones[z.id] = entry

    # ----- footfall ------------------------------------------------------------------------------
    gt_in = sorted(t for p in ev if p["role"] == "customer" for t, d in p["a"].entries if d > 0)
    pr_in = sorted(r[0] for r in store.query("SELECT t FROM entries WHERE run_id=? AND direction=1", (run_id,)))
    f_tp = _greedy_time_match(gt_in, pr_in, tol=3.0)
    footfall = {"gt": len(gt_in), "pipeline": len(pr_in), "error_pct": _pct_err(len(pr_in), len(gt_in)),
                **_prf(f_tp, len(pr_in) - f_tp, len(gt_in) - f_tp)}

    # ----- heat ----------------------------------------------------------------------------------
    nx = int(np.ceil(cfg.floor.width_m / cfg.output.heat_cell_m))
    ny = int(np.ceil(cfg.floor.height_m / cfg.output.heat_cell_m))
    gt_heat = np.zeros((ny, nx))
    for p in ev:
        if p["role"] != "customer":
            continue
        for (_, gx, gy), s in p["a"].heat.items():
            gt_heat[gy, gx] += s
    pr_heat = np.zeros((ny, nx))
    for gx, gy, s in store.query("SELECT gx,gy,SUM(seconds) FROM heat WHERE run_id=? GROUP BY gx,gy", (run_id,)):
        pr_heat[gy, gx] += s
    corr = float(np.corrcoef(gt_heat.ravel(), pr_heat.ravel())[0, 1]) if gt_heat.std() and pr_heat.std() else float("nan")

    n_gt_cust = sum(1 for p in ev if p["role"] == "customer" and p["a"].dropped is None)
    return {
        "run_id": run_id,
        "footfall": footfall,
        "zones": zones,
        "totals": {
            "impressions": {"gt": tot["impr_gt"], "pipeline": tot["impr_pr"],
                            "error_pct": _pct_err(tot["impr_pr"], tot["impr_gt"]), "staff_leak": tot["leak"],
                            "ghosts": tot["ghost"]},
            "engaged": {"gt": cnt["gt"], "pipeline": cnt["mode"], "error_pct": _pct_err(cnt["mode"], cnt["gt"]),
                        **_prf(**conf["mode"])},
            "engaged_stop_only": _prf(**conf["stop"]),
            "engaged_stop_facing": _prf(**conf["stop_facing"]),
            "engaged_ceiling": _prf(**conf["ceiling"]),
            "engagement_mode": mode,
        },
        "engaged_dwell_mae_s": float(np.mean(dwell_err)) if dwell_err else float("nan"),
        "tracking": {"gt_customers": n_gt_cust, "pipeline_customers": meta.get("customers"),
                     "pipeline_staff_tracklets": meta.get("staff"), "stitched": meta.get("stitched"),
                     "tracklets": meta.get("tracklets_committed")},
        "heatmap": {"pearson_r": corr, "occupancy_ratio_pipeline_over_gt":
                    float(pr_heat.sum() / gt_heat.sum()) if gt_heat.sum() else float("nan")},
        "calibration_rmse_cm": meta.get("calibration_rmse_cm"),
    }


def _greedy_time_match(a: list[float], b: list[float], tol: float) -> int:
    used = set()
    tp = 0
    for x in a:
        best, bd = None, tol
        for j, y in enumerate(b):
            if j in used:
                continue
            d = abs(x - y)
            if d <= bd:
                best, bd = j, d
        if best is not None:
            used.add(best)
            tp += 1
    return tp


def _f(x, pct=False, nd=2):
    if x is None or (isinstance(x, float) and x != x):
        return "  n/a"
    return f"{x:.0f}%" if pct else f"{x:.{nd}f}"


def format_report(r: dict, title: str = "") -> str:
    L = []
    L.append(f"=== Validation {title} (run {r['run_id']}) ===")
    ff = r["footfall"]
    L.append(f"Footfall (door entries): GT {ff['gt']}  pipeline {ff['pipeline']}  error {_f(ff['error_pct'], True)}  "
             f"precision {_f(ff['precision'])}  recall {_f(ff['recall'])}")
    L.append("")
    L.append(f"{'zone':<14}{'impr GT':>8}{'PL':>5}{'err':>6}  | engaged vs scripted intent:{'':<18}"
             f"{'GT':>3}{'PL':>4} | stop-only P / R / F1 | stop+motion-facing P / R / F1")
    for zid, z in r["zones"].items():
        i = z["impressions"]
        base = f"{zid:<14}{i['gt']:>8}{i['pipeline']:>5}{_f(i['error_pct'], True):>6}  |"
        if "engaged" in z:
            e, s, f = z["engaged"], z["engaged_stop_only"], z["engaged_stop_facing"]
            L.append(f"{base} {'':<44}{e['gt']:>3}{e['pipeline']:>4} | "
                     f"{_f(s['precision'])} / {_f(s['recall'])} / {_f(s['f1'])}  | "
                     f"{_f(f['precision'])} / {_f(f['recall'])} / {_f(f['f1'])}")
        else:
            L.append(f"{base} (area zone: impressions and dwell only)")
    t = r["totals"]
    s, f = t["engaged_stop_only"], t["engaged_stop_facing"]
    L.append("-" * 110)
    L.append(f"{'ALL':<14}{t['impressions']['gt']:>8}{t['impressions']['pipeline']:>5}{_f(t['impressions']['error_pct'], True):>6}  | "
             f"{'':<44}{t['engaged']['gt']:>3}{t['engaged']['pipeline']:>4} | "
             f"{_f(s['precision'])} / {_f(s['recall'])} / {_f(s['f1'])}  | "
             f"{_f(f['precision'])} / {_f(f['recall'])} / {_f(f['f1'])}")
    c = t["engaged_ceiling"]
    L.append(f"(headline 'engaged' uses engagement_mode={t['engagement_mode']}; stop-only CEILING with perfect perception: "
             f"P {_f(c['precision'])} / R {_f(c['recall'])} / F1 {_f(c['f1'])})")
    L.append("")
    L.append(f"Staff leakage into customer impressions: {t['impressions']['staff_leak']}   "
             f"ghost/duplicate impressions: {t['impressions']['ghosts']}")
    L.append(f"Engaged-time MAE on matched engaged visits: {_f(r['engaged_dwell_mae_s'])} s")
    tr = r["tracking"]
    L.append(f"Tracking: GT customers {tr['gt_customers']}  pipeline customers {tr['pipeline_customers']}  "
             f"staff tracklets {tr['pipeline_staff_tracklets']}  stitched {tr['stitched']}")
    h = r["heatmap"]
    L.append(f"Heatmap: Pearson r {_f(h['pearson_r'])}  occupancy ratio (pipeline/GT) {_f(h['occupancy_ratio_pipeline_over_gt'])}")
    L.append(f"Calibration reprojection RMSE: {r['calibration_rmse_cm']} cm")
    return "\n".join(L)
