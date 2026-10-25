"""Simulated live view: a store-camera feed next to a live floor plan, driven by the real pipeline.

``make_live_clip`` simulates a short busy stretch of the demo store, renders two cameras from the same ground
truth (overhead for analysis, a perspective "webcam" for people to look at), runs the actual pipeline on the
overhead video, and renders two synchronised videos:

  camera.mp4     the perspective feed, heads mosaicked, anonymous track tags, zones that light up when engaged
  floorplan.mp4  a live floor plan: tracks, short trails, recent-traffic glow, live counters

Everything here is transient demo output: the pipeline still persists only events, and the per-frame track
snapshots used for drawing live only in memory while the clip is rendered.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from .config import StoreConfig
from .geometry import points_in_polygon
from .pipeline import Pipeline
from .sim.generate import Period, generate_period
from .sim.layout import build_store_config, default_layout
from .sim.webcam import WebcamScene
from .store import EventStore

# BGR "monitor" palette for the floor-plan panel (it sits on a dark monitor frame in either theme)
BG, FLOOR, GRID = (30, 25, 21), (42, 36, 31), (58, 51, 45)
FIX_FILL, FIX_EDGE = (70, 63, 56), (96, 88, 80)
ZONE, ZONE_ENG, AREA, STAFFZ = (255, 170, 90), (110, 215, 90), (170, 200, 70), (225, 130, 205)
DOT, DOT_ENG, DOT_STAFF = (255, 150, 60), (110, 220, 80), (235, 120, 190)
TEXT, MUTED = (236, 232, 228), (170, 160, 150)
PANEL = (720, 540)
BAR_H = 60
PPM = 40  # px per metre on the panel


# =============================================================================== streaming analytics
class LiveState:
    """A streaming approximation of the batch analysis, only for the live counters and highlights.

    Same thresholds as the batch pipeline (stop speed, minimum zone visit, engage seconds), evaluated online.
    The numbers that reach the database always come from the batch analysis, not from here.
    """

    def __init__(self, cfg: StoreConfig):
        self.cfg = cfg
        self.thr = cfg.thresholds
        self.fix = [z for z in cfg.zones if z.kind == "fixture"]
        self.staff_zone = next((z for z in cfg.zones if z.kind == "staff"), None)
        self.tracks: dict[int, dict] = {}
        self.visitors: set[int] = set()
        self.impr: set[tuple[int, str]] = set()
        self.eng: set[tuple[int, str]] = set()
        self.prev_t: float | None = None

    def update(self, t: float, items: list) -> dict:
        dt = 0.1 if self.prev_t is None else max(t - self.prev_t, 1e-3)
        self.prev_t = t
        out: dict[int, dict] = {}
        eng_zones: set[str] = set()
        in_store = 0
        for uid, xy, vote in items:
            tr = self.tracks.setdefault(uid, {"t0": t, "hist": deque(maxlen=14), "votes": 0, "n": 0, "sz": 0.0,
                                              "zones": {}})
            tr["hist"].append((t, float(xy[0]), float(xy[1])))
            tr["n"] += 1
            tr["votes"] += int(vote)
            if self.staff_zone is not None and points_in_polygon(np.array([xy], float), self.staff_zone.polygon)[0]:
                tr["sz"] += dt
            age = t - tr["t0"]
            staff = (tr["n"] >= 8 and tr["votes"] / tr["n"] >= self.cfg.staff.vote_ratio) or \
                (age >= 6.0 and tr["sz"] / max(age, 1e-6) >= self.cfg.staff.zone_time_fraction)
            h = tr["hist"]
            speed = None
            if len(h) >= 6:
                (ta, xa, ya), (tb, xb, yb) = h[0], h[-1]
                speed = math.hypot(xb - xa, yb - ya) / max(tb - ta, 1e-6)
            stopped = speed is not None and speed < self.thr.v_slow_mps
            eng_here = None
            if not staff and age >= 1.5:
                self.visitors.add(uid)
                in_store += 1
                for z in self.fix:
                    zs = tr["zones"].setdefault(z.id, {"p": 0.0, "s": 0.0})
                    if points_in_polygon(np.array([xy], float), z.polygon)[0]:
                        zs["p"] += dt
                        zs["s"] += dt if stopped else 0.0
                        if zs["p"] >= self.thr.min_zone_visit_s:
                            self.impr.add((uid, z.id))
                        need = self.cfg.engage_seconds(z)
                        if zs["s"] >= need:
                            self.eng.add((uid, z.id))
                            if stopped:
                                eng_here = z.id
                                eng_zones.add(z.id)
            out[uid] = {"x": float(xy[0]), "y": float(xy[1]), "staff": bool(staff), "stopped": bool(stopped),
                        "engaged": eng_here}
        return {"tracks": out, "engaged_zones": eng_zones,
                "counts": {"in_store": in_store, "visitors": len(self.visitors), "impressions": len(self.impr),
                           "engaged": len(self.eng)}}


# =============================================================================== floor-plan panel
class FloorPanel:
    def __init__(self, cfg: StoreConfig, layout):
        self.cfg, self.layout = cfg, layout
        self.W, self.H = PANEL
        self.heat = np.zeros((int(layout.height / 0.25), int(layout.width / 0.25)), np.float32)
        self.trails: dict[int, deque] = {}
        self.base = self._base()

    def _px(self, x: float, y: float) -> tuple[int, int]:
        return int(round(x * PPM)), int(round(BAR_H + y * PPM))

    def _dashed(self, img, poly, col):
        for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
            n = max(int(math.hypot(x1 - x0, y1 - y0) * PPM / 8), 1)
            for i in range(0, n, 2):
                a, b = i / n, min((i + 1) / n, 1.0)
                cv2.line(img, self._px(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a),
                         self._px(x0 + (x1 - x0) * b, y0 + (y1 - y0) * b), col, 1, cv2.LINE_AA)

    def _base(self) -> np.ndarray:
        L = self.layout
        img = np.full((self.H, self.W, 3), BG, np.uint8)
        cv2.rectangle(img, self._px(0, 0), self._px(L.width, L.height), FLOOR, -1)
        for gx in range(int(L.width) + 1):
            cv2.line(img, self._px(gx, 0), self._px(gx, L.height), GRID, 1)
        for gy in range(int(L.height) + 1):
            cv2.line(img, self._px(0, gy), self._px(L.width, gy), GRID, 1)
        for r in L.obstacles():
            cv2.rectangle(img, self._px(r[0], r[1]), self._px(r[2], r[3]), FIX_FILL, -1)
            cv2.rectangle(img, self._px(r[0], r[1]), self._px(r[2], r[3]), FIX_EDGE, 1)
        for z in self.cfg.zones:
            poly = [tuple(p) for p in z.polygon]
            if z.kind == "staff":
                self._dashed(img, poly, STAFFZ)
            else:
                col = ZONE if z.kind == "fixture" else AREA
                cv2.polylines(img, [np.array([self._px(*p) for p in poly], np.int32)], True, col, 1, cv2.LINE_AA)
        for z in self.cfg.zones:
            x0, y0 = min(p[0] for p in z.polygon), min(p[1] for p in z.polygon)
            cv2.putText(img, z.name, (self._px(x0, y0)[0] + 4, self._px(x0, y0)[1] + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        TEXT, 1, cv2.LINE_AA)
        e = self.cfg.entrance
        cv2.line(img, self._px(e.p1[0], L.height - 0.04), self._px(e.p2[0], L.height - 0.04), (80, 80, 235), 4)
        cv2.putText(img, "Entrance", (self._px(e.p1[0], 0)[0] + 24, self._px(0, L.height - 0.25)[1]),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, MUTED, 1, cv2.LINE_AA)
        return img

    def render(self, tracks: dict[int, dict], engaged_zones: set[str], counts: dict, clock: str) -> np.ndarray:
        img = self.base.copy()
        # recent-traffic glow
        for uid, tr in tracks.items():
            if tr["staff"]:
                continue
            gx, gy = int(tr["x"] / 0.25), int(tr["y"] / 0.25)
            if 0 <= gy < self.heat.shape[0] and 0 <= gx < self.heat.shape[1]:
                self.heat[gy, gx] += 0.05
        self.heat *= 0.9992
        h = cv2.GaussianBlur(self.heat, (0, 0), 1.5)
        h = np.clip(h / 0.9, 0, 1)
        if h.max() > 0.02:
            big = cv2.resize(h, (int(self.layout.width * PPM), int(self.layout.height * PPM)), interpolation=cv2.INTER_CUBIC)
            big = np.clip(big, 0, 1)[..., None]
            region = img[BAR_H:BAR_H + big.shape[0], :big.shape[1]]
            glow = np.array([40, 120, 255], np.float32) * (1 - big) + np.array([30, 60, 235], np.float32) * big
            region[:] = (region.astype(np.float32) * (1 - 0.55 * big ** 0.8) + glow * 0.55 * big ** 0.8).astype(np.uint8)
        # engaged zones
        ov = img.copy()
        for z in self.cfg.zones:
            if z.id in engaged_zones:
                cv2.fillPoly(ov, [np.array([self._px(*p) for p in z.polygon], np.int32)], ZONE_ENG)
        img = cv2.addWeighted(ov, 0.32, img, 0.68, 0)
        for z in self.cfg.zones:
            if z.id in engaged_zones:
                cv2.polylines(img, [np.array([self._px(*p) for p in z.polygon], np.int32)], True, ZONE_ENG, 2, cv2.LINE_AA)
        # Ghost tracks pile up around people who stand still (bgsub weakness); the batch metrics already exclude
        # staff, so for readability draw one staff marker per 1 m cluster.
        kept_staff: list[tuple[float, float]] = []
        tracks = dict(tracks)
        for uid in [u for u, t in tracks.items() if t["staff"]]:
            t = tracks[uid]
            if any(math.hypot(t["x"] - sx, t["y"] - sy) < 1.0 for sx, sy in kept_staff):
                del tracks[uid]
            else:
                kept_staff.append((t["x"], t["y"]))
        # trails + dots
        gone = set(self.trails) - set(tracks)
        for uid in gone:
            self.trails.pop(uid, None)
        for uid, tr in tracks.items():
            self.trails.setdefault(uid, deque(maxlen=70)).append((tr["x"], tr["y"]))
        for uid, tr in tracks.items():
            col = DOT_STAFF if tr["staff"] else (DOT_ENG if tr["engaged"] else DOT)
            pts = [self._px(*p) for p in self.trails[uid]]
            for i in range(1, len(pts)):
                k = i / len(pts)
                c = tuple(int(BG[j] + (col[j] - BG[j]) * k * 0.8) for j in range(3))
                cv2.line(img, pts[i - 1], pts[i], c, 2, cv2.LINE_AA)
        for uid, tr in tracks.items():
            col = DOT_STAFF if tr["staff"] else (DOT_ENG if tr["engaged"] else DOT)
            c = self._px(tr["x"], tr["y"])
            cv2.circle(img, c, 8, col, -1, cv2.LINE_AA)
            cv2.circle(img, c, 8, (245, 245, 245), 1, cv2.LINE_AA)
            if tr["stopped"] and not tr["staff"]:
                cv2.circle(img, c, 12, col, 1, cv2.LINE_AA)
            cv2.putText(img, "staff" if tr["staff"] else f"#{uid}", (c[0] + 11, c[1] - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        TEXT, 1, cv2.LINE_AA)
        # HUD bar
        cv2.rectangle(img, (0, 0), (self.W, BAR_H), (24, 20, 17), -1)
        cv2.putText(img, "FLOOR PLAN", (14, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT, 1, cv2.LINE_AA)
        cv2.circle(img, (118, 19), 5, (60, 60, 235), -1, cv2.LINE_AA)
        cv2.putText(img, "LIVE", (128, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT, 1, cv2.LINE_AA)
        cv2.putText(img, clock, (14, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.42, MUTED, 1, cv2.LINE_AA)
        stats = [("IN STORE", counts["in_store"]), ("VISITORS", counts["visitors"]),
                 ("IMPRESSIONS", counts["impressions"]), ("ENGAGED", counts["engaged"])]
        x0 = 222
        for i, (label, val) in enumerate(stats):
            x = x0 + i * 124
            cv2.putText(img, str(val), (x, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.8, DOT_ENG if label == "ENGAGED" else TEXT, 2, cv2.LINE_AA)
            cv2.putText(img, label, (x, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.36, MUTED, 1, cv2.LINE_AA)
        cv2.putText(img, "SIMULATED", (self.W - 94, self.H - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.4, MUTED, 1, cv2.LINE_AA)
        return img


# =============================================================================== clip generation
def _h264(src: Path, dst: Path, crf: int = 27, scale: str | None = None) -> None:
    if shutil.which("ffmpeg") is None:
        shutil.move(str(src), str(dst))
        return
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an"]
    if scale:
        cmd += ["-vf", f"scale={scale}"]
    subprocess.run(cmd + [str(dst)], check=True)
    src.unlink(missing_ok=True)


def _lerp_angle(a: float, b: float, t: float) -> float:
    d = (b - a + math.pi) % (2 * math.pi) - math.pi
    return a + d * t


def _person_arrays(p, fps: int) -> dict:
    x, y = np.asarray(p.x, float), np.asarray(p.y, float)
    step = np.hypot(np.diff(x, prepend=x[0]), np.diff(y, prepend=y[0]))
    speed = np.convolve(step * fps, np.ones(5) / 5, mode="same")
    return {"speed": speed, "cum": np.cumsum(step)}


def make_live_clip(out_dir: str | Path, seconds: float = 60.0, warmup_s: float = 55.0, seed: int = 11,
                   fps_out: int = 20, base_per_min: float = 8.0, start: str = "2026-11-02T10:00:00",
                   progress: bool = True) -> dict:
    """Simulate, analyse and render the live view. Returns file paths and a few stats."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_work"
    work.mkdir(exist_ok=True)
    fps = 10
    t_wall = time.time()
    say = (lambda m: print(m, flush=True)) if progress else (lambda m: None)

    layout = default_layout()
    cfg = build_store_config(layout, seed=seed)
    minutes = (warmup_s + seconds + 100 + 15) / 60.0
    period = Period("live", minutes=minutes, seed=seed + 100, base_per_min=base_per_min)
    say(f"live view: simulating {minutes:.1f} min of store traffic...")
    video, _gt, people = generate_period(layout, period, work, fps=fps, verbose=False)

    tz = ZoneInfo(cfg.output.timezone)
    start_ts = datetime.fromisoformat(start).replace(tzinfo=tz).timestamp()
    say("live view: running the analytics pipeline on the overhead camera...")
    store = EventStore(work / "live.db")
    live = LiveState(cfg)
    states: dict[int, dict] = {}

    def hook(t: float, items: list) -> None:
        states[int(round((t - start_ts) * fps))] = live.update(t, items)

    meta = Pipeline(cfg, store, label="live").run(video, start_ts, progress=False, frame_hook=hook)
    store.close()

    arrays = {p.pid: _person_arrays(p, fps) for p in people}
    scene = WebcamScene(layout)
    panel = FloorPanel(cfg, layout)
    n_out = int(seconds * fps_out)
    cam_tmp, fp_tmp = work / "camera.tmp.mp4", work / "floorplan.tmp.mp4"
    cam_w = cv2.VideoWriter(str(cam_tmp), cv2.VideoWriter_fourcc(*"mp4v"), fps_out, (scene.W, scene.H))
    fp_w = cv2.VideoWriter(str(fp_tmp), cv2.VideoWriter_fourcc(*"mp4v"), fps_out, PANEL)
    k_last = max(states)
    say(f"live view: rendering {n_out} frames at {fps_out} fps...")
    for j in range(n_out):
        kf = (warmup_s + j / fps_out) * fps
        k = min(int(math.floor(kf)), k_last - 1)
        a = min(kf - k, 1.0)
        s0, s1 = states.get(k), states.get(k + 1) or states.get(k)
        if s0 is None:
            s0 = s1 = {"tracks": {}, "engaged_zones": set(), "counts": {"in_store": 0, "visitors": 0, "impressions": 0, "engaged": 0}}
        # tracks interpolated between the two analysed frames
        tr = {}
        for uid, v0 in s0["tracks"].items():
            v1 = s1["tracks"].get(uid, v0)
            tr[uid] = dict(v0, x=v0["x"] + (v1["x"] - v0["x"]) * a, y=v0["y"] + (v1["y"] - v0["y"]) * a)
        clock = datetime.fromtimestamp(start_ts + kf / fps, tz).strftime("%Y-%m-%d %H:%M:%S")
        fp_w.write(panel.render(tr, s0["engaged_zones"], s0["counts"], clock))

        figs = []
        for p in people:
            i0 = k - p.start_frame
            if i0 < 0 or i0 + 1 >= len(p.x):
                continue
            x = float(p.x[i0] + (p.x[i0 + 1] - p.x[i0]) * a)
            y = float(p.y[i0] + (p.y[i0 + 1] - p.y[i0]) * a)
            if p.role == "customer" and not (0.0 <= x <= layout.width and 0.0 <= y <= layout.height):
                continue
            ar = arrays[p.pid]
            staff = p.role == "staff"
            best, best_d = None, 0.9
            for uid, t in tr.items():
                d = math.hypot(t["x"] - x, t["y"] - y)
                if d < best_d:
                    best, best_d = uid, d
            tag = None
            if best is not None:
                tag = "STAFF" if tr[best]["staff"] else f"#{best}"
            figs.append({"pid": p.pid, "x": x, "y": y, "h": _lerp_angle(float(p.h[i0]), float(p.h[i0 + 1]), a),
                         "speed": float(ar["speed"][i0]), "dist": float(ar["cum"][i0]), "role": p.role,
                         "shirt": (70, 70, 78) if staff else p.clothing, "hair": p.hair, "tag": tag})
        for n, (mx, my) in enumerate(layout.mannequins):
            figs.append({"pid": 900 + n, "x": mx, "y": my, "h": 1.2 if n else -0.6, "mannequin": True, "role": "mannequin"})
        cam_w.write(scene.render(figs, {z: True for z in s0["engaged_zones"]}, clock))
        if progress and j % 400 == 399:
            say(f"  frame {j + 1}/{n_out}")
    cam_w.release()
    fp_w.release()
    cam_out, fp_out = out / "camera.mp4", out / "floorplan.mp4"
    _h264(cam_tmp, cam_out, crf=29)
    _h264(fp_tmp, fp_out, crf=26)
    shutil.rmtree(work, ignore_errors=True)
    last = states.get(min(int((warmup_s + seconds) * fps), k_last), {"counts": {}})
    info = {"camera": cam_out.name, "floorplan": fp_out.name, "seconds": seconds, "fps": fps_out, "simulated": True,
            "start": start, "clip_start_offset_s": warmup_s,
            "counts_at_end": last["counts"], "pipeline": {"detector": meta["detector"], "customers": meta["customers"],
                                                          "staff": meta["staff"], "elapsed_s": meta["elapsed_s"]},
            "note": ("Both views are rendered from the same simulation. The pipeline analyses the overhead camera; "
                     "the perspective feed is for people to watch. Heads are mosaicked; tags are anonymous track ids.")}
    import json
    (out / "live.json").write_text(json.dumps(info, indent=1))
    say(f"live view done in {time.time() - t_wall:.0f}s -> {cam_out}, {fp_out}")
    return info
