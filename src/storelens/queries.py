"""Business metrics over the event store: what the dashboard, API and report all read.

Metric glossary (also shown in the UI):
  visitors            anonymous customer visits tracked in the window (staff excluded)
  footfall            door entries (outside -> inside crossings)
  impressions         visits that entered a fixture's exposure zone (an "opportunity to see")
  engaged             impressions where the visitor stopped there for >= min_engage_s
                      (+ faced the display in stop_facing mode); decided at query time
  reach               impressions / visitors: share of visitors who came near the display
  engagement rate     engaged / impressions: how compelling the display is *once seen*
  engaged per 100     engaged / visitors * 100: traffic-normalised overall performance
"""
from __future__ import annotations

import json
import math
import warnings
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
from scipy.stats import norm

from .analysis import engaged_seconds
from .config import StoreConfig
from .store import EventStore


@dataclass
class Window:
    run_ids: list[int] | None = None
    t0: float | None = None
    t1: float | None = None

    def sql(self, tcol: str) -> tuple[str, list]:
        parts, args = [], []
        if self.run_ids:
            parts.append(f"run_id IN ({','.join('?' * len(self.run_ids))})")
            args += list(self.run_ids)
        if self.t0 is not None:
            parts.append(f"{tcol} >= ?")
            args.append(self.t0)
        if self.t1 is not None:
            parts.append(f"{tcol} <= ?")
            args.append(self.t1)
        return (" AND ".join(parts) if parts else "1=1"), args


# Defence in depth: the pipeline never writes staff zone visits or door crossings, but every business metric
# is restricted to customer visits at query time anyway, so a future importer/bug cannot inflate them.
_CUSTOMER_ONLY = "(run_id, visit_key) IN (SELECT run_id, visit_key FROM visits WHERE role='customer')"


def _med(x) -> float | None:
    return float(np.median(x)) if len(x) else None


class Analytics:
    def __init__(self, store: EventStore, cfg: StoreConfig):
        self.store, self.cfg = store, cfg
        self.tz = ZoneInfo(cfg.output.timezone)
        self.mode = cfg.thresholds.engagement_mode
        self.fixtures = [z for z in cfg.zones if z.kind == "fixture"]
        self.measured = cfg.measured_zones()

    # ------------------------------------------------------------------------------- basics
    def iso(self, t: float) -> str:
        return datetime.fromtimestamp(t, self.tz).isoformat(timespec="seconds")

    def runs(self) -> list[dict]:
        out = []
        for rid, label, cam, src, t0, t1, created, meta in self.store.query(
                "SELECT run_id,label,camera_id,source,t_start,t_end,created_at,meta FROM runs ORDER BY t_start"):
            out.append({"run_id": rid, "label": label, "camera_id": cam, "source": src, "t_start": t0, "t_end": t1,
                        "start_iso": self.iso(t0), "end_iso": self.iso(t1 or t0), "meta": json.loads(meta or "{}")})
        return out

    def _visits(self, w: Window) -> list[tuple[int, str, float, float]]:
        where, args = w.sql("t_first")
        return self.store.query(
            f"SELECT run_id,visit_key,t_first,duration_s FROM visits WHERE role='customer' AND {where}", tuple(args))

    def _zone_rows(self, w: Window):
        where, args = w.sql("t_enter")
        rows = self.store.query(
            f"SELECT run_id,visit_key,zone_id,t_enter,t_exit,dwell_s,stopped_s,facing_stopped_s "
            f"FROM zone_visits WHERE {where} AND {_CUSTOMER_ONLY}", tuple(args))
        zmeta = {z.id: (z.kind, self.cfg.engage_seconds(z)) for z in self.cfg.zones}
        out = []
        for rid, key, zid, te, tx, dwell, stp, fst in rows:
            kind, min_s = zmeta.get(zid, ("area", 1e9))
            eng_s = engaged_seconds(stp, fst, self.mode)
            out.append((rid, key, zid, te, tx, dwell, eng_s, kind == "fixture" and eng_s >= min_s))
        return out

    def _entries(self, w: Window) -> list[tuple[float, int]]:
        where, args = w.sql("t")
        return self.store.query(f"SELECT t,direction FROM entries WHERE {where} AND {_CUSTOMER_ONLY}", tuple(args))

    # ------------------------------------------------------------------------------- summary
    def summary(self, w: Window) -> dict:
        visits = self._visits(w)
        zr = [r for r in self._zone_rows(w) if self.cfg.zone(r[2]).kind == "fixture"]
        ent = self._entries(w)
        impressions = len({(r[0], r[1], r[2]) for r in zr})
        engaged_keys = {(r[0], r[1], r[2]) for r in zr if r[7]}
        engaged_visitors = {(r[0], r[1]) for r in zr if r[7]}
        eng_secs = [r[6] for r in zr if r[7]]
        staff = self.store.query(
            f"SELECT COUNT(*) FROM visits WHERE role='staff' AND {w.sql('t_first')[0]}", tuple(w.sql("t_first")[1]))[0][0]
        n_vis = len(visits)
        return {
            "visitors": n_vis,
            "footfall": sum(1 for _, d in ent if d > 0),
            "exits": sum(1 for _, d in ent if d < 0),
            "impressions": impressions,
            "engaged": len(engaged_keys),
            "engagement_rate": len(engaged_keys) / impressions if impressions else None,
            "engaged_visitors": len(engaged_visitors),
            "engaged_visitor_rate": len(engaged_visitors) / n_vis if n_vis else None,
            "median_engaged_s": _med(eng_secs),
            "median_visit_s": _med([v[3] for v in visits]),
            "staff_tracklets": staff,
            "engagement_mode": self.mode,
        }

    # ------------------------------------------------------------------------------- zones
    def zones(self, w: Window) -> list[dict]:
        n_vis = len(self._visits(w))
        rows = self._zone_rows(w)
        by_zone: dict[str, list] = {}
        for r in rows:
            by_zone.setdefault(r[2], []).append(r)
        out = []
        for z in self.measured:
            zr = by_zone.get(z.id, [])
            per_visit_dwell: dict[tuple, float] = {}
            eng_visit: dict[tuple, float] = {}
            for rid, key, _, te, tx, dwell, eng_s, eng in zr:
                k = (rid, key)
                per_visit_dwell[k] = per_visit_dwell.get(k, 0.0) + dwell
                if eng:
                    eng_visit[k] = eng_visit.get(k, 0.0) + eng_s
            impr = len(per_visit_dwell)
            engd = len(eng_visit)
            out.append({
                "zone_id": z.id, "name": z.name, "kind": z.kind,
                "impressions": impr,
                "engaged": engd if z.kind == "fixture" else None,
                "reach": impr / n_vis if n_vis else None,
                "engagement_rate": (engd / impr if impr else None) if z.kind == "fixture" else None,
                "engaged_per_100": (engd / n_vis * 100 if n_vis else None) if z.kind == "fixture" else None,
                "median_dwell_s": _med(list(per_visit_dwell.values())),
                "median_engaged_s": _med(list(eng_visit.values())) if z.kind == "fixture" else None,
            })
        return out

    # ------------------------------------------------------------------------------- series
    def timeseries(self, w: Window, zone_id: str | None = None) -> list[dict]:
        """Time buckets with activity. Short holes are zero-filled; long holes (between runs/days) are
        marked with ``gap: true`` on the next bucket instead of emitting thousands of empty buckets."""
        bucket = self.cfg.output.bucket_minutes * 60.0
        rows = [r for r in self._zone_rows(w)
                if (zone_id is None and self.cfg.zone(r[2]).kind == "fixture") or r[2] == zone_id]
        ent = self._entries(w)
        cells: dict[int, dict] = {}

        def cell(t: float) -> dict:
            return cells.setdefault(int(math.floor(t / bucket)), {"entries": 0, "impressions": 0, "engaged": 0})

        for t, d in ent:
            if d > 0:
                cell(t)["entries"] += 1
        seen_i, seen_e = set(), set()
        for rid, key, zid, te, tx, dwell, eng_s, e in rows:
            c = cell(te)
            i = int(math.floor(te / bucket))
            if (rid, key, zid, i) not in seen_i:
                seen_i.add((rid, key, zid, i))
                c["impressions"] += 1
            if e and (rid, key, zid, i) not in seen_e:
                seen_e.add((rid, key, zid, i))
                c["engaged"] += 1
        out: list[dict] = []
        prev = None
        for i in sorted(cells):
            if prev is not None and i - prev > 1:
                if i - prev <= 4:
                    for k in range(prev + 1, i):
                        out.append({"t": self.iso(k * bucket), "t_epoch": k * bucket, "entries": 0, "impressions": 0,
                                    "engaged": 0, "gap": False})
                else:
                    cells[i]["gap"] = True
            out.append({"t": self.iso(i * bucket), "t_epoch": i * bucket, "gap": cells[i].get("gap", False),
                        **{k: cells[i][k] for k in ("entries", "impressions", "engaged")}})
            prev = i
        return out

    # ------------------------------------------------------------------------------- heatmap
    def heatmap(self, w: Window) -> dict:
        cell = self.cfg.output.heat_cell_m
        nx = int(math.ceil(self.cfg.floor.width_m / cell))
        ny = int(math.ceil(self.cfg.floor.height_m / cell))
        grid = np.zeros((ny, nx))
        where, args = w.sql("bucket_ts")
        for gx, gy, s in self.store.query(f"SELECT gx,gy,SUM(seconds) FROM heat WHERE {where} GROUP BY gx,gy", tuple(args)):
            if 0 <= gx < nx and 0 <= gy < ny:
                grid[gy, gx] = s
        return {"cell_m": cell, "nx": nx, "ny": ny, "max": float(grid.max()), "total_s": float(grid.sum()),
                "grid": np.round(grid, 1).tolist()}

    # ------------------------------------------------------------------------------- flows
    def flows(self, w: Window, top: int = 12) -> dict:
        rows = [r for r in self._zone_rows(w) if self.cfg.zone(r[2]).kind != "staff"]
        seq: dict[tuple, list[tuple[float, str]]] = {}
        for rid, key, zid, te, *_ in rows:
            seq.setdefault((rid, key), []).append((te, zid))
        ent_where, ent_args = w.sql("t")
        entered = {(rid, key) for rid, key in self.store.query(
            f"SELECT run_id,visit_key FROM entries WHERE direction=1 AND {ent_where}", tuple(ent_args))}
        exited = {(rid, key) for rid, key in self.store.query(
            f"SELECT run_id,visit_key FROM entries WHERE direction=-1 AND {ent_where}", tuple(ent_args))}
        counts: dict[tuple[str, str], int] = {}
        for k, items in seq.items():
            path = [z for _, z in sorted(items)]
            comp = [path[0]] + [b for a, b in zip(path, path[1:]) if a != b]
            if k in entered:
                comp = ["ENTRANCE"] + comp
            if k in exited:
                comp = comp + ["EXIT"]
            for a, b in zip(comp, comp[1:]):
                counts[(a, b)] = counts.get((a, b), 0) + 1
        names = {z.id: z.name for z in self.cfg.zones}
        names.update({"ENTRANCE": "Entrance", "EXIT": "Exit"})
        edges = sorted(counts.items(), key=lambda kv: -kv[1])
        return {"edges": [{"from": a, "to": b, "from_name": names[a], "to_name": names[b], "count": c}
                          for (a, b), c in edges[:top]],
                "n_paths": len(seq)}

    # ------------------------------------------------------------------------------- lift
    def _outcomes(self, w: Window, zone_ids: list[str]) -> tuple[np.ndarray, np.ndarray]:
        """Per-visitor binary outcomes: engaged[v, z], reached[v, z] for each requested zone."""
        visits = self._visits(w)
        idx = {(rid, key): i for i, (rid, key, *_) in enumerate(visits)}
        eng = np.zeros((len(visits), len(zone_ids)), dtype=np.int8)
        rch = np.zeros_like(eng)
        zi = {z: i for i, z in enumerate(zone_ids)}
        for rid, key, zid, te, tx, dwell, eng_s, e in self._zone_rows(w):
            v, z = idx.get((rid, key)), zi.get(zid)
            if v is None or z is None:
                continue
            rch[v, z] = 1
            if e:
                eng[v, z] = 1
        return eng, rch

    def lift(self, a: Window, b: Window, zone_ids: list[str] | None = None, control_ids: list[str] | None = None,
             n_boot: int = 4000, seed: int = 7) -> dict:
        """Before/after comparison of *engaged visitors per 100 store visitors* per fixture.

        Normalising by store visitors removes the effect of a busier day. The bootstrap resamples
        visitors (not visits), so the CI reflects how few visitors a store really has.

        Two safeguards against fooling yourself:
          * the adjusted lift divides by the **median** lift of the other fixtures, so one display that changed a
            lot (the one you actually moved) cannot make its untouched neighbours look worse;
          * intervals are simultaneous across the displays compared (Bonferroni), so looking at five displays does
            not give five chances to see a 'win' by luck.
        """
        all_ids = [z.id for z in self.fixtures]
        zones = zone_ids or all_ids
        eng_a, rch_a = self._outcomes(a, all_ids)
        eng_b, rch_b = self._outcomes(b, all_ids)
        names = {z.id: z.name for z in self.fixtures}
        return lift_stats(eng_a, eng_b, rch_a, rch_b, all_ids, zones, names, control_ids, n_boot, seed)

    # ------------------------------------------------------------------------------- quality
    def quality(self, w: Window | None = None) -> dict:
        runs = self.runs()
        if w and w.run_ids:
            runs = [r for r in runs if r["run_id"] in w.run_ids]
        warnings = []
        for r in runs:
            m = r["meta"]
            lab = r["label"] or f"run {r['run_id']}"
            if m.get("calibration_rmse_cm", 0) > 10:
                warnings.append(f"{lab}: calibration error {m['calibration_rmse_cm']} cm: re-click calibration points")
            if m.get("mean_detections_per_frame", 1) < 0.2:
                warnings.append(f"{lab}: almost no detections: check camera/ detector")
            tot = max(m.get("customers", 0) + m.get("staff", 0), 1)
            if m.get("staff", 0) / tot > 0.5:
                warnings.append(f"{lab}: >50% of tracks classified as staff: check staff colour config")
        warnings.append("Engagement mode is '%s'. %s" % (
            self.mode, "Facing is not used (no orientation model yet): stops by passers-by or people on the phone "
                       "inside the zone count as engaged." if self.mode == "stop" else
            "Facing uses motion heading unless a pose/head estimator is plugged in."))
        return {"runs": runs, "warnings": warnings,
                "privacy": {"raw_frames_stored": 0, "faces_stored": 0, "trajectories_stored": False,
                            "appearance_reid": False, "blur_mode": self.cfg.privacy.blur,
                            "id_rotation_hours": self.cfg.privacy.id_rotation_hours}}

    def config_view(self) -> dict:
        c = self.cfg
        return {
            "name": c.name, "floor": c.floor.model_dump(), "timezone": c.output.timezone,
            "entrance": c.entrance.model_dump(),
            "zones": [z.model_dump() for z in c.zones],
            "engagement": {"mode": self.mode, "min_engage_s": c.thresholds.min_engage_s,
                           "v_slow_mps": c.thresholds.v_slow_mps, "facing_cone_deg": c.thresholds.facing_cone_deg},
            "bucket_minutes": c.output.bucket_minutes, "heat_cell_m": c.output.heat_cell_m,
        }


def lift_stats(eng_a: np.ndarray, eng_b: np.ndarray, rch_a: np.ndarray, rch_b: np.ndarray, all_ids: list[str],
               zones: list[str], names: dict[str, str], control_ids: list[str] | None = None, n_boot: int = 4000,
               seed: int = 7, alpha: float = 0.05) -> dict:
    """The statistics behind ``Analytics.lift``, on per-visitor 0/1 outcome matrices (visitors x displays)."""
    na, nb = len(eng_a), len(eng_b)
    if na < 5 or nb < 5:
        return {"error": "not enough visitors in one of the windows", "visitors": {"a": na, "b": nb}}
    rng = np.random.default_rng(seed)
    ia = rng.integers(0, na, (n_boot, na))
    ib = rng.integers(0, nb, (n_boot, nb))
    col = {z: i for i, z in enumerate(all_ids)}
    k = max(len(zones), 1)
    q_lo, q_hi = 100 * alpha / (2 * k), 100 * (1 - alpha / (2 * k))
    ya = {f: eng_a[:, i].astype(float) for f, i in col.items()}
    yb = {f: eng_b[:, i].astype(float) for f, i in col.items()}
    ratio = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        for f in all_ids:
            ratio[f] = yb[f][ib].mean(axis=1) / ya[f][ia].mean(axis=1)  # bootstrap lift ratios, (n_boot,)
    results = []
    for z in zones:
        ctrl = [c for c in (control_ids or all_ids) if c != z]
        ra, rb = ya[z].mean(), yb[z].mean()
        entry = {
            "zone_id": z, "name": names.get(z, z), "controls": ctrl,
            "a": {"visitors": na, "engaged_visitors": int(ya[z].sum()), "per_100": ra * 100,
                  "reach_pct": float(rch_a[:, col[z]].mean() * 100)},
            "b": {"visitors": nb, "engaged_visitors": int(yb[z].sum()), "per_100": rb * 100,
                  "reach_pct": float(rch_b[:, col[z]].mean() * 100)},
        }
        entry.update(_raw_ci(ra, rb, ratio[z], q_lo, q_hi))
        entry["visitors_needed_per_period"] = _visitors_needed(ra, rb, k)
        pts = [yb[c].mean() / ya[c].mean() for c in ctrl if ya[c].mean() > 0]
        if pts and ra > 0:
            ctrl_point = float(np.median(pts))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                ctrl_boot = np.nanmedian(np.stack([np.where(np.isfinite(ratio[c]), ratio[c], np.nan) for c in ctrl]), axis=0)
            with np.errstate(divide="ignore", invalid="ignore"):
                adj = ratio[z] / ctrl_boot - 1
            adj = adj[np.isfinite(adj)]
            if ctrl_point > 0 and len(adj) > n_boot * 0.5:
                lo, hi = np.percentile(adj, [q_lo, q_hi])
                entry["adjusted"] = {"lift_pct": ((rb / ra) / ctrl_point - 1) * 100, "ci_low_pct": lo * 100,
                                     "ci_high_pct": hi * 100, "verdict": _verdict(lo, hi),
                                     "p_improved": float((adj > 0).mean())}
        results.append(entry)
    level = 100 * (1 - alpha / k)
    return {"visitors": {"a": na, "b": nb}, "zones": results, "n_boot": n_boot, "n_compared": k,
            "ci_level_pct": round(level, 1),
            "note": (f"Lift in engaged visitors per 100 store visitors. Intervals are {level:.0f}% bootstrap CIs over "
                     f"visitors, widened so that comparing {k} display{'s' if k != 1 else ''} keeps the family-wise error "
                     f"at {alpha:.0%}. The adjusted lift divides by the median change of the other displays.")}


def _raw_ci(ra: float, rb: float, boot_ratio: np.ndarray, q_lo: float, q_hi: float) -> dict:
    if ra == 0:
        return {"lift_pct": None, "ci_low_pct": None, "ci_high_pct": None, "verdict": "n/a (no baseline engagement)",
                "p_improved": None}
    boot = boot_ratio[np.isfinite(boot_ratio)] - 1
    lo, hi = np.percentile(boot, [q_lo, q_hi]) if len(boot) else (float("nan"), float("nan"))
    return {"lift_pct": (rb / ra - 1) * 100, "ci_low_pct": lo * 100, "ci_high_pct": hi * 100,
            "verdict": _verdict(lo, hi), "p_improved": float((boot > 0).mean()) if len(boot) else None}


def _visitors_needed(p1: float, p2: float, k: int = 1) -> int | None:
    """Rough guide: store visitors needed in EACH period to confirm a change from engaged-visitor rate p1 to p2
    (two-proportion test, 80% power, 95% family-wise confidence across ``k`` displays), rounded up to 10."""
    if not (0 < p1 < 1 and 0 < p2 < 1) or abs(p2 - p1) < 1e-9:
        return None
    z_a = float(norm.ppf(1 - 0.05 / (2 * max(k, 1))))
    pbar = (p1 + p2) / 2
    n = (z_a * math.sqrt(2 * pbar * (1 - pbar)) + 0.8416 * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2 / (p2 - p1) ** 2
    return int(math.ceil(n / 10.0) * 10)


def _verdict(lo: float, hi: float) -> str:
    if lo > 0:
        return "improved"
    if hi < 0:
        return "worse"
    return "inconclusive"
