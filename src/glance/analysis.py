"""Turn a floor-coordinate trajectory into business events.

Definitions (these ARE the product, so they are explicit and configurable):

* **Impression**   a customer's presence inside a fixture's *exposure zone* for >= ``min_zone_visit_s``
                   (IAB: Opportunity To See / gross impression).
* **Engaged**      within that visit, the person was *stopped* (speed < v_slow) for a cumulative
                   >= ``min_engage_s`` (and, in ``stop_facing`` mode, also *facing the display*: heading
                   within ``facing_cone_deg`` of the bearing to it). A proxy for IAB Likelihood To See.
* **Entry**        crossing the entrance line from outside to inside (with hysteresis against jitter).

Facing is the weakest link on CCTV. The default estimator is *motion heading held while stationary*:
deployable anywhere, but it cannot tell someone who stopped and turned away. ``validate`` quantifies
that gap and ``FacingEstimator`` is the seam where pose/head-orientation models plug in.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import uniform_filter1d

from .config import EntranceConfig, StoreConfig
from .geometry import angle_diff, points_in_polygon

NAN = float("nan")


@dataclass
class ZoneVisit:
    zone_id: str
    t_enter: float
    t_exit: float
    dwell_s: float
    stopped_s: float  # time inside the zone moving slower than v_slow
    facing_stopped_s: float  # ... and with heading within the cone towards the display


def engaged_seconds(stopped_s: float, facing_stopped_s: float, mode: str) -> float:
    return facing_stopped_s if mode == "stop_facing" else stopped_s


def is_engaged(stopped_s: float, facing_stopped_s: float, min_s: float, mode: str) -> bool:
    """Engagement is decided at query time from stored seconds, so the mode can change without reprocessing."""
    return engaged_seconds(stopped_s, facing_stopped_s, mode) >= min_s


@dataclass
class TrackAnalysis:
    t_first: float
    t_last: float
    duration_s: float
    zone_visits: list[ZoneVisit] = field(default_factory=list)
    entries: list[tuple[float, int]] = field(default_factory=list)  # (t, +1 entry / -1 exit)
    heat: dict[tuple[float, int, int], float] = field(default_factory=dict)
    staff_zone_fraction: float = 0.0
    dropped: str | None = None


# ------------------------------------------------------------------------------------ kinematics

def resample(t: np.ndarray, xy: np.ndarray, fps: float) -> tuple[np.ndarray, np.ndarray]:
    """Uniform time grid; linear interpolation across short detection gaps."""
    if len(t) < 2:
        return t.copy(), xy.copy()
    grid = np.arange(t[0], t[-1] + 1e-9, 1.0 / fps)
    return grid, np.stack([np.interp(grid, t, xy[:, 0]), np.interp(grid, t, xy[:, 1])], axis=1)


def smooth(xy: np.ndarray, fps: float, seconds: float = 0.5) -> np.ndarray:
    n = max(int(round(seconds * fps)), 1)
    if n <= 1 or len(xy) < 2:
        return xy.copy()
    return uniform_filter1d(xy, size=n, axis=0, mode="nearest")


def kinematics(xy: np.ndarray, fps: float, window_s: float) -> tuple[np.ndarray, np.ndarray]:
    """Displacement-based speed (m/s) and heading (rad, atan2(dy, dx)) over a centred window."""
    n = len(xy)
    if n < 2:
        return np.zeros(n), np.full(n, NAN)
    h = max(int(round(window_s * fps / 2)), 1)
    lo = np.clip(np.arange(n) - h, 0, n - 1)
    hi = np.clip(np.arange(n) + h, 0, n - 1)
    span = (hi - lo) / fps
    span[span == 0] = np.inf
    d = xy[hi] - xy[lo]
    speed = np.hypot(d[:, 0], d[:, 1]) / span
    heading = np.arctan2(d[:, 1], d[:, 0])
    return speed, heading


class FacingEstimator:
    """Interface: per-sample facing direction (rad, NaN = unknown) from kinematics (+ pixels, later)."""

    def facing(self, speed: np.ndarray, heading: np.ndarray, v_move: float) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError


class MotionFacing(FacingEstimator):
    """Heading while moving; held (unchanged) while stationary; unknown before the first movement."""

    def facing(self, speed, heading, v_move):
        out = np.full(len(speed), NAN)
        valid = speed >= v_move
        if not valid.any():
            return out
        idx = np.where(valid, np.arange(len(speed)), -1)
        np.maximum.accumulate(idx, out=idx)
        has = idx >= 0
        out[has] = heading[idx[has]]
        return out


# ------------------------------------------------------------------------------------ intervals

def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive-exclusive index runs where mask is True."""
    if not mask.any():
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
    return list(zip(starts.tolist(), ends.tolist()))


def _merge_runs(runs: list[tuple[int, int]], max_gap: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in runs:
        if out and s - out[-1][1] <= max_gap:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def entry_crossings(t: np.ndarray, xy: np.ndarray, ent: EntranceConfig) -> list[tuple[float, int]]:
    a, b, ins = np.array(ent.p1), np.array(ent.p2), np.array(ent.inside)
    d = b - a
    L2 = float(d @ d)
    n = np.array([-d[1], d[0]]) / math.sqrt(L2)
    if (ins - a) @ n < 0:
        n = -n
    sd = (xy - a) @ n  # + inside
    u = ((xy - a) @ d) / L2
    h = ent.hysteresis_m
    state = 0
    out: list[tuple[float, int]] = []
    for i in range(len(t)):
        s = 1 if sd[i] > h else (-1 if sd[i] < -h else 0)
        if s == 0:
            continue
        if state != 0 and s != state and -0.35 <= u[i] <= 1.35:
            out.append((float(t[i]), +1 if s > 0 else -1))
        state = s
    return out


# ------------------------------------------------------------------------------------ main

def analyze_samples(t: np.ndarray, xy: np.ndarray, cfg: StoreConfig, *,
                    facing_override: np.ndarray | None = None,
                    estimator: FacingEstimator | None = None,
                    compute_heat: bool = True) -> TrackAnalysis:
    """Full analysis of one trajectory. ``t``/``xy`` are raw samples (any spacing)."""
    thr = cfg.thresholds
    fps = thr.process_fps
    res = TrackAnalysis(t_first=float(t[0]), t_last=float(t[-1]), duration_s=float(t[-1] - t[0]))
    if len(t) < 2 or res.duration_s < thr.min_track_s:
        res.dropped = "short"
        return res
    med = np.median(xy, axis=0)
    if res.duration_s >= thr.static_min_duration_s and np.hypot(*(xy - med).T).max() < thr.static_max_disp_m:
        res.dropped = "static"
        return res

    tg, xyg = resample(t, xy, fps)
    xs = smooth(xyg, fps, 0.5)
    speed, vheading = kinematics(xs, fps, thr.smooth_window_s)
    if facing_override is not None:
        # externally supplied headings (ground truth in validation): nearest-sample onto our grid
        fo = np.asarray(facing_override, float)
        facing = fo[np.round(np.linspace(0, len(fo) - 1, len(tg))).astype(int)]
    else:
        facing = (estimator or MotionFacing()).facing(speed, vheading, thr.v_move_mps)
    dt = 1.0 / fps
    cone = math.radians(thr.facing_cone_deg)
    max_gap = int(round(thr.zone_gap_merge_s * fps))
    min_n = max(int(round(thr.min_zone_visit_s * fps)), 1)

    res.entries = entry_crossings(tg, xs, cfg.entrance)

    staff_time = 0.0
    for z in cfg.zones:
        inside = points_in_polygon(xs, np.asarray(z.polygon))
        if z.kind == "staff":
            staff_time += float(inside.sum()) * dt
            continue
        runs = [r for r in _merge_runs(_runs(inside), max_gap) if inside[r[0]:r[1]].sum() >= min_n]
        for s, e in runs:
            seg = slice(s, e)
            ins = inside[seg]
            slow = speed[seg] < thr.v_slow_mps
            stopped = ins & slow
            if z.kind == "fixture" and z.target is not None:
                bearing = np.arctan2(z.target[1] - xs[seg, 1], z.target[0] - xs[seg, 0])
                f = facing[seg]
                ok = ~np.isnan(f) & (angle_diff(np.nan_to_num(f), bearing) <= cone)
                fs = stopped & ok
            else:
                fs = np.zeros_like(stopped)
            res.zone_visits.append(ZoneVisit(
                zone_id=z.id, t_enter=float(tg[s]), t_exit=float(tg[e - 1]), dwell_s=float(ins.sum()) * dt,
                stopped_s=float(stopped.sum()) * dt, facing_stopped_s=float(fs.sum()) * dt))
    res.staff_zone_fraction = staff_time / max(res.duration_s, dt)

    if compute_heat:
        res.heat = heat_cells(tg, xs, cfg)
    return res


def heat_cells(t: np.ndarray, xy: np.ndarray, cfg: StoreConfig) -> dict[tuple[float, int, int], float]:
    cell = cfg.output.heat_cell_m
    bucket = cfg.output.bucket_minutes * 60.0
    nx = int(math.ceil(cfg.floor.width_m / cell))
    ny = int(math.ceil(cfg.floor.height_m / cell))
    gx = np.clip((xy[:, 0] / cell).astype(int), 0, nx - 1)
    gy = np.clip((xy[:, 1] / cell).astype(int), 0, ny - 1)
    bt = np.floor(t / bucket) * bucket
    dt = 1.0 / cfg.thresholds.process_fps
    keys = np.stack([bt, gx, gy], axis=1)
    uniq, inv = np.unique(keys, axis=0, return_inverse=True)
    secs = np.bincount(inv.ravel(), minlength=len(uniq)) * dt
    return {(float(u[0]), int(u[1]), int(u[2])): float(s) for u, s in zip(uniq, secs)}
