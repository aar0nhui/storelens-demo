"""The default synthetic store: an 18 m x 12 m shop seen by one overhead CCTV camera.

Floor coordinates are metres, x right, y down; the door is on the bottom wall. Everything here is
*simulation-side truth*. The analytics pipeline never imports this module: it only sees the
generated ``store.yaml`` (zones + a noisy camera calibration) and the video.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from ..config import (CameraConfig, EntranceConfig, FloorPlan, OutputConfig, StaffConfig,
                      StoreConfig, ZoneConfig)

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1

PPM_FLOOR = 60  # pixels per metre in the pre-warp floor canvas
CAM_SIZE = (960, 640)  # camera frame (w, h)
# Where the floor-canvas corners land in the camera image (mild keystone + rotation).
CAM_DST = np.float32([[55, 40], [905, 22], [935, 600], [25, 622]])


def rect_poly(r: Rect) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = r
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


@dataclass
class Fixture:
    id: str
    name: str
    obstacle: Rect
    exposure: Rect
    target: tuple[float, float]
    stand_rects: list[Rect]
    # behaviour parameters (period-dependent values are overridden by Period)
    p_visit: float = 0.5
    p_browse: float = 0.3
    p_away: float = 0.05
    p_glance: float = 0.25
    dwell_median_s: float = 9.0


@dataclass
class Layout:
    width: float = 18.0
    height: float = 12.0
    fixtures: list[Fixture] = field(default_factory=list)
    checkout_obstacle: Rect = (14.6, 8.0, 17.6, 9.2)
    checkout_zone: Rect = (13.6, 9.2, 17.9, 11.0)
    checkout_stand: Rect = (14.8, 9.7, 17.2, 10.3)
    checkout_target: tuple[float, float] = (16.0, 8.6)
    staff_zone: Rect = (14.6, 6.6, 17.9, 8.0)
    door_x: tuple[float, float] = (7.8, 10.2)
    door_y: float = 11.2
    mannequins: list[tuple[float, float]] = field(default_factory=lambda: [(2.2, 10.2), (15.6, 1.6)])
    extra_obstacles: list[Rect] = field(default_factory=list)

    def obstacles(self) -> list[Rect]:
        return [f.obstacle for f in self.fixtures] + [self.checkout_obstacle] + list(self.extra_obstacles)

    def fixture(self, fid: str) -> Fixture:
        return next(f for f in self.fixtures if f.id == fid)


def default_layout() -> Layout:
    fx = [
        Fixture("front_table", "Front Table", (4.5, 7.4, 7.5, 8.6), (3.7, 8.6, 8.3, 9.9), (6.0, 8.0),
                [(4.0, 9.2, 8.0, 9.5)], p_visit=0.80, p_browse=0.35, p_away=0.04, dwell_median_s=8.0),
        Fixture("wall_a", "Wall Display A", (0.0, 2.5, 0.7, 8.0), (0.7, 2.3, 2.0, 8.2), (0.35, 5.25),
                [(1.2, 3.0, 1.5, 7.5)], p_visit=0.45, p_browse=0.40, p_away=0.05, dwell_median_s=14.0),
        Fixture("gondola_c", "Gondola C", (3.0, 3.3, 8.2, 4.2), (3.0, 4.2, 8.2, 5.5), (5.6, 3.75),
                [(3.5, 4.8, 7.8, 5.1)], p_visit=0.55, p_browse=0.30, p_away=0.06, dwell_median_s=10.0),
        Fixture("endcap_b", "Endcap B", (11.0, 4.6, 13.0, 5.6), (9.9, 3.7, 14.1, 6.7), (12.0, 5.1),
                [(10.8, 6.1, 13.2, 6.4), (10.8, 3.8, 13.2, 4.1), (10.1, 4.8, 10.4, 5.4), (13.6, 4.8, 13.9, 5.4)],
                p_visit=0.50, p_browse=0.22, p_away=0.05, dwell_median_s=8.0),
        Fixture("back_promo", "Back Promo", (6.0, 0.0, 12.0, 0.8), (5.0, 0.8, 13.0, 2.2), (9.0, 0.4),
                [(6.5, 1.3, 11.5, 1.6)], p_visit=0.30, p_browse=0.45, p_away=0.05, p_glance=0.20,
                dwell_median_s=12.0),
    ]
    return Layout(fixtures=fx)


def cam_homography() -> np.ndarray:
    """3x3 matrix mapping *floor metres* to camera pixels (ground truth of the simulated camera)."""
    w, h = 18.0 * PPM_FLOOR, 12.0 * PPM_FLOOR
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    Hc = cv2.getPerspectiveTransform(src, CAM_DST)
    S = np.diag([PPM_FLOOR, PPM_FLOOR, 1.0])
    return Hc @ S


def landmark_points(layout: Layout) -> list[tuple[float, float]]:
    """Points an installer could click on a floor plan: store corners and fixture corners."""
    pts = [(0.0, 0.0), (layout.width, 0.0), (layout.width, layout.height), (0.0, layout.height)]
    for f in layout.fixtures[:3]:
        x0, y0, x1, y1 = f.obstacle
        pts += [(x0, y1), (x1, y1)]
    x0, y0, x1, y1 = layout.checkout_obstacle
    pts += [(x0, y1), (x1, y0)]
    return pts


def build_store_config(layout: Layout, calib_noise_px: float = 1.5, seed: int = 0,
                       name: str = "Demo Store (synthetic)") -> StoreConfig:
    """What an installer would write: zones in metres + a *noisy* click-based calibration."""
    rng = np.random.default_rng(seed)
    Hm = cam_homography()
    floor_pts = np.array(landmark_points(layout), dtype=np.float64)
    px = cv2.perspectiveTransform(floor_pts.reshape(-1, 1, 2), Hm).reshape(-1, 2)
    px = px + rng.normal(0, calib_noise_px, px.shape)

    zones: list[ZoneConfig] = []
    for f in layout.fixtures:
        zones.append(ZoneConfig(id=f.id, name=f.name, kind="fixture", polygon=rect_poly(f.exposure),
                                target=f.target))
    zones.append(ZoneConfig(id="checkout", name="Checkout", kind="area", polygon=rect_poly(layout.checkout_zone)))
    zones.append(ZoneConfig(id="staff_area", name="Back of house", kind="staff", polygon=rect_poly(layout.staff_zone)))

    cx = sum(layout.door_x) / 2
    ent = EntranceConfig(p1=(layout.door_x[0], layout.door_y), p2=(layout.door_x[1], layout.door_y),
                         inside=(cx, layout.door_y - 3.0))
    staff = StaffConfig(hsv_ranges=[((0, 120, 80), (8, 255, 255)), ((172, 120, 80), (179, 255, 255))])
    cam = CameraConfig(id="cam1", anchor="center", pixel_points=[tuple(map(float, p)) for p in px],
                       floor_points=[tuple(map(float, p)) for p in floor_pts])
    return StoreConfig(name=name, floor=FloorPlan(width_m=layout.width, height_m=layout.height),
                       cameras=[cam], zones=zones, entrance=ent, staff=staff,
                       output=OutputConfig(bucket_minutes=5))
