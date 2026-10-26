"""A two-camera version of the simulated store, for testing cameras that overlap.

``front`` is the entrance camera and sees the front 8 m of the floor; ``back`` is a floor camera and sees the back
8.4 m. They overlap in a 4.4 m band across the middle, where a person is visible to both: the ownership rule
(cameras.py) must still count that person once. The simulated people, their ground truth and the store layout are
the same as the one-camera demo; only the cameras differ.
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from ..config import (CameraConfig, CameraPose, DemographicsConfig, EntranceConfig, FloorPlan, OutputConfig,
                      StaffConfig, StoreConfig, ZoneConfig)
from ..cameras import suggest_detector_params
from .generate import Period, apply_overrides, build_people, write_ground_truth
from .layout import CAM_SIZE, CamSpec, Layout, rect_poly, spec_homography
from .render import Renderer


def multicam_specs() -> list[CamSpec]:
    return [
        CamSpec("front", "Front camera (entrance)", "entrance", (0.0, 4.0, 18.0, 12.0),
                [[40, 30], [920, 18], [940, 610], [20, 625]], (9.0, 8.0, 3.0, 270.0, 120.0)),
        CamSpec("back", "Back camera (floor)", "floor", (0.0, 0.0, 18.0, 8.4),
                [[30, 20], [935, 35], [925, 615], [35, 600]], (9.0, 4.0, 3.0, 90.0, 120.0)),
    ]


def _calibration_points(layout: Layout, spec: CamSpec) -> list[tuple[float, float]]:
    """Points an installer could click: the corners and mid-edges of what the camera sees, plus fixture corners."""
    x0, y0, x1, y1 = spec.rect
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), ((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1), (x0, (y0 + y1) / 2),
           (x1, (y0 + y1) / 2)]
    for f in layout.fixtures:
        fx0, fy0, fx1, fy1 = f.obstacle
        for p in [(fx0, fy1), (fx1, fy1)]:
            if x0 + 0.3 < p[0] < x1 - 0.3 and y0 + 0.3 < p[1] < y1 - 0.3:
                pts.append(p)
    return pts


def build_multicam_config(layout: Layout, specs: list[CamSpec] | None = None, calib_noise_px: float = 1.5, seed: int = 0,
                          name: str = "Demo Store (two cameras, synthetic)") -> StoreConfig:
    specs = specs or multicam_specs()
    rng = np.random.default_rng(seed)
    cams = []
    for sp in specs:
        floor_pts = np.array(_calibration_points(layout, sp), dtype=np.float64)
        px = cv2.perspectiveTransform(floor_pts.reshape(-1, 1, 2), spec_homography(sp)).reshape(-1, 2)
        px = px + rng.normal(0, calib_noise_px, px.shape)
        cams.append(CameraConfig(id=sp.id, name=sp.name, role=sp.role, anchor="center",
                                 pixel_points=[tuple(map(float, p)) for p in px],
                                 floor_points=[tuple(map(float, p)) for p in floor_pts], image_size=CAM_SIZE,
                                 pose=CameraPose(x=sp.pose[0], y=sp.pose[1], height_m=sp.pose[2],
                                                 heading_deg=sp.pose[3], fov_deg=sp.pose[4])))
    zones = [ZoneConfig(id=f.id, name=f.name, kind="fixture", polygon=rect_poly(f.exposure), target=f.target)
             for f in layout.fixtures]
    zones.append(ZoneConfig(id="checkout", name="Checkout", kind="area", polygon=rect_poly(layout.checkout_zone)))
    zones.append(ZoneConfig(id="staff_area", name="Back of house", kind="staff", polygon=rect_poly(layout.staff_zone)))
    cx = sum(layout.door_x) / 2
    ent = EntranceConfig(p1=(layout.door_x[0], layout.door_y), p2=(layout.door_x[1], layout.door_y),
                         inside=(cx, layout.door_y - 3.0), camera=specs[0].id)
    staff = StaffConfig(hsv_ranges=[((0, 120, 80), (8, 255, 255)), ((172, 120, 80), (179, 255, 255))])
    cfg = StoreConfig(name=name, floor=FloorPlan(width_m=layout.width, height_m=layout.height), cameras=cams,
                      zones=zones, entrance=ent, staff=staff, output=OutputConfig(bucket_minutes=5),
                      demographics=DemographicsConfig(enabled=False))
    for cam in cfg.cameras:  # what an installer does after running `glance cameras`: apply the suggestion
        cam.detector_params = suggest_detector_params(cfg, cam)
    return cfg


def generate_multicam_period(layout: Layout, period: Period, out_dir: str | Path, specs: list[CamSpec] | None = None,
                             fps: int = 10, verbose: bool = True) -> dict:
    """One simulated stretch rendered by every camera at once (same people, same frames) + one ground-truth file."""
    specs = specs or multicam_specs()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lay = apply_overrides(layout, period.overrides)
    people = build_people(lay, period, fps)
    n_frames = int(period.minutes * 60 * fps)
    renderers = {sp.id: Renderer(lay, seed=period.seed + 100 + i, spec=sp) for i, sp in enumerate(specs)}
    videos = {sp.id: out / f"{period.label}_{sp.id}.mp4" for sp in specs}
    writers = {cid: cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), fps, CAM_SIZE) for cid, p in videos.items()}
    if not all(w.isOpened() for w in writers.values()):
        raise RuntimeError("OpenCV could not open a video writer (mp4v)")
    by_start = sorted(people, key=lambda p: p.start_frame)
    active, nxt, t0 = [], 0, time.time()
    for fi in range(n_frames):
        while nxt < len(by_start) and by_start[nxt].start_frame <= fi:
            active.append(by_start[nxt])
            nxt += 1
        active = [p for p in active if fi < p.end_frame]
        states = [(float(p.x[fi - p.start_frame]), float(p.y[fi - p.start_frame]), float(p.h[fi - p.start_frame]),
                   p.clothing, p.hair) for p in active]
        for cid, r in renderers.items():
            writers[cid].write(r.frame(states, fi / fps))
        if verbose and fi % 3000 == 0 and fi:
            print(f"  [{period.label}] frame {fi}/{n_frames} ({fi / max(time.time() - t0, 1e-6):.0f} fps)")
    for w in writers.values():
        w.release()
    gt = out / f"{period.label}_gt.json.gz"
    write_ground_truth(gt, people, fps, n_frames, period)
    return {"videos": {k: str(v) for k, v in videos.items()}, "gt": str(gt), "people": people}
