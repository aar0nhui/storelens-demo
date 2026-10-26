"""Several cameras, one floor plan: calibration checks, coverage, and who measures what.

The rule that keeps counts honest without ever recognising anyone: **every spot on the floor, and every display, has
exactly one owning camera**. A camera only tracks people standing in the area it owns, so two cameras can overlap
without counting the same person twice, and nothing links a person across cameras (no appearance matching).

* ``calibration_report``   reprojection error per point, leave-one-out error (how well the fit would predict a point it
                           did not use), how much of the floor the points cover, and pixels per metre.
* ``CameraNetwork``        per-camera visibility and quality on a floor grid, display ownership (explicit ``owns`` or
                           automatic), coverage / overlap / blind-spot fractions, and ``accepts`` (the ownership test the
                           pipeline applies to each detection before tracking).

A person who walks from one camera's area into another's is, to the analytics, two short tracks. Visitors are counted at
the entrance camera and displays are measured by their owners, so this costs nothing for the headline numbers; what it
cannot do is follow a path across the boundary (the Activity flows stop at the boundary).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .config import CameraConfig, StoreConfig
from .geometry import Homography, points_in_polygon

LOBBY_M = 1.5  # around the door line, the entrance camera always owns the floor
MIN_ZONE_COVERAGE = 0.6  # a display seen less than this by its owner is flagged


# ----------------------------------------------------------------------------------------------- calibration

def _scale_px_per_m(h: Homography, floor_xy: np.ndarray, d: float = 0.25) -> np.ndarray:
    """Local image scale (pixels per metre) at floor points, by finite differences through the homography."""
    p = np.asarray(floor_xy, float).reshape(-1, 2)
    base = h.to_pixel(p)
    dx = h.to_pixel(p + [d, 0]) - base
    dy = h.to_pixel(p + [0, d]) - base
    return 0.5 * (np.hypot(dx[:, 0], dx[:, 1]) + np.hypot(dy[:, 0], dy[:, 1])) / d


def _area_scale_px2_per_m2(h: Homography, floor_xy: np.ndarray, d: float = 0.25) -> np.ndarray:
    """Local area scale (pixels^2 per metre^2) = |det| of the floor->image Jacobian, by finite differences."""
    p = np.asarray(floor_xy, float).reshape(-1, 2)
    base = h.to_pixel(p)
    dx = (h.to_pixel(p + [d, 0]) - base) / d
    dy = (h.to_pixel(p + [0, d]) - base) / d
    return np.abs(dx[:, 0] * dy[:, 1] - dx[:, 1] * dy[:, 0])


# One person seen from above covers about this many square metres of image-plane floor (calibrated on the simulator's
# overhead people: ~330 px at 53 px/m). A real store should check it against a frame: it is a starting point.
PERSON_FOOTPRINT_M2 = 0.1175


def suggest_detector_params(cfg: StoreConfig, cam: CameraConfig) -> dict:
    """Starting values for the background-subtraction detector's pixel-size parameters, from the camera's calibration.

    ``typical_area`` is how many pixels one person covers; it is what lets the detector decide that a big blob is two
    people. Left at a value tuned for another camera it splits one person in two (every count doubles) or merges
    groups. Overhead cameras only: oblique views need a learned detector instead."""
    h = Homography.from_camera(cam)
    fl = np.asarray(cam.floor_points, float)
    centre = np.array([[float(fl[:, 0].mean()), float(fl[:, 1].mean())]])
    a = float(_area_scale_px2_per_m2(h, centre)[0])
    typical = PERSON_FOOTPRINT_M2 * a
    return {"typical_area": round(typical), "min_area": round(0.33 * typical), "max_area": round(18 * typical)}


def calibration_report(cfg: StoreConfig, cam: CameraConfig) -> dict:
    """How trustworthy is this camera's pixel-to-floor mapping? Honest about what four points cannot tell you."""
    px = np.asarray(cam.pixel_points, float)
    fl = np.asarray(cam.floor_points, float)
    n = len(px)
    h = Homography(px, fl)
    err = np.hypot(*(h.to_floor(px) - fl).T) * 100.0
    out = {"camera_id": cam.id, "n_points": n, "rmse_cm": float(np.sqrt((err ** 2).mean())),
           "max_cm": float(err.max()), "worst_point": int(err.argmax()), "per_point_cm": [round(float(e), 1) for e in err],
           "warnings": []}
    # Leave-one-out: refit without a point and see where the fit puts it. With exactly 4 points the fit is exact, so
    # the reprojection error is ~0 whatever the clicks were: it says nothing, and we say so.
    if n >= 5:
        loo = []
        for i in range(n):
            m = np.ones(n, bool)
            m[i] = False
            try:
                hi = Homography(px[m], fl[m])
                loo.append(float(np.hypot(*(hi.to_floor(px[i:i + 1])[0] - fl[i])) * 100.0))
            except ValueError:
                loo.append(float("nan"))
        loo = np.asarray(loo)
        out["loo_rmse_cm"] = float(np.sqrt(np.nanmean(loo ** 2)))
        out["loo_per_point_cm"] = [round(float(x), 1) for x in loo]
    else:
        out["loo_rmse_cm"] = None
        out["warnings"].append("Only 4 calibration points: the fit is exact by construction, so the error shown is "
                               "meaningless. Use 8+ points spread over the floor to get a real error estimate.")
    # coverage of the floor by the calibration points (their convex hull), and collinearity
    hull = cv2.convexHull(fl.astype(np.float32))
    area = float(cv2.contourArea(hull))
    out["hull_fraction"] = area / (cfg.floor.width_m * cfg.floor.height_m)
    if out["hull_fraction"] < 0.02:
        out["warnings"].append("Calibration points are nearly on a line: the mapping is unreliable. Click points that "
                               "form a wide spread over the floor.")
    elif out["hull_fraction"] < 0.25:
        out["warnings"].append(f"Calibration points cover only {out['hull_fraction'] * 100:.0f}% of the floor: places "
                               f"outside them are extrapolated and may be off by much more than the error above.")
    # image scale at the floor centre of what the camera sees
    centre = np.array([[float(np.mean(fl[:, 0])), float(np.mean(fl[:, 1]))]])
    scale = float(_scale_px_per_m(h, centre)[0])
    out["suggested_detector_params"] = suggest_detector_params(cfg, cam)
    out["px_per_m_centre"] = scale
    out["cm_per_px_centre"] = 100.0 / scale if scale > 0 else None
    if scale < 25:
        out["warnings"].append(f"Only ~{scale:.0f} pixels per metre near the middle of the view: a person is about "
                               f"{0.5 * scale:.0f} px wide. Detection gets unreliable below ~15 px.")
    worst = max(out["rmse_cm"], out["loo_rmse_cm"] or 0.0)
    out["status"] = "good" if worst <= 8 else ("fair" if worst <= 15 else "poor")
    if worst > 15:
        out["warnings"].append("Calibration error is large (> 15 cm): displays can be mis-assigned. Re-click the points.")
    return out


# ----------------------------------------------------------------------------------------------- network

@dataclass
class _Cam:
    cfg: CameraConfig
    h: Homography
    size: tuple[int, int]
    privacy: list[np.ndarray]


class CameraNetwork:
    """Which camera sees (and is responsible for) each part of the floor."""

    def __init__(self, store_cfg: StoreConfig, cell_m: float = 0.25):
        self.cfg = store_cfg
        self.cell = cell_m
        self.cams: list[_Cam] = []
        self.warnings: list[str] = []
        for c in store_cfg.cameras:
            px = np.asarray(c.pixel_points, float)
            if c.image_size:
                size = (int(c.image_size[0]), int(c.image_size[1]))
            else:
                size = (int(px[:, 0].max() * 1.1), int(px[:, 1].max() * 1.1))  # guess; the calibration box says otherwise
                self.warnings.append(f"{c.id}: image_size not set; assuming {size[0]}x{size[1]} from the calibration points.")
            self.cams.append(_Cam(c, Homography.from_camera(c), size, [np.asarray(p, float) for p in c.privacy_mask_polygons]))
        self.ids = [c.cfg.id for c in self.cams]
        W, H = store_cfg.floor.width_m, store_cfg.floor.height_m
        self.nx, self.ny = int(math.ceil(W / cell_m)), int(math.ceil(H / cell_m))
        gx, gy = np.meshgrid((np.arange(self.nx) + 0.5) * cell_m, (np.arange(self.ny) + 0.5) * cell_m)
        self.grid_xy = np.stack([gx.ravel(), gy.ravel()], axis=1)
        self.q = np.stack([self._quality(c, self.grid_xy) for c in self.cams])  # (n_cam, n_cell)
        self._zone_owner = self._assign_zones()

    # -- per-camera quality at floor points -------------------------------------------------------
    def _quality(self, cam: _Cam, pts: np.ndarray) -> np.ndarray:
        """0 = not seen (outside the image, behind a privacy mask, outside the valid region); else pixels per metre,
        faded near the image border where lens distortion and cut-off people make measurements worse."""
        pts = np.asarray(pts, float).reshape(-1, 2)
        px = cam.h.to_pixel(pts)
        w, h = cam.size
        m = 0.02 * min(w, h)
        inside = (px[:, 0] >= m) & (px[:, 0] <= w - m) & (px[:, 1] >= m) & (px[:, 1] <= h - m)
        border = np.minimum.reduce([px[:, 0], w - px[:, 0], px[:, 1], h - px[:, 1]])
        fade = np.clip(border / (0.12 * min(w, h)), 0.15, 1.0)
        q = np.where(inside, _scale_px_per_m(cam.h, pts) * fade, 0.0)
        vr = cam.cfg.valid_region
        if vr:
            q = np.where(points_in_polygon(pts, np.asarray(vr)), q, 0.0)
        for poly in cam.privacy:
            hit = points_in_polygon(px, poly)
            q = np.where(hit, 0.0, q)
        return np.where(np.isfinite(q), q, 0.0)

    # -- ownership -----------------------------------------------------------------------------------
    def _zone_cells(self, zone_id: str) -> np.ndarray:
        return points_in_polygon(self.grid_xy, np.asarray(self.cfg.zone(zone_id).polygon))

    def _assign_zones(self) -> dict[str, str | None]:
        owners: dict[str, str | None] = {}
        explicit = {z: c.cfg.id for c in self.cams for z in (c.cfg.owns or [])}
        for z in self.cfg.zones:
            cells = self._zone_cells(z.id)
            if not cells.any():
                owners[z.id] = explicit.get(z.id)
                continue
            seen = (self.q[:, cells] > 0).mean(axis=1)  # share of the zone each camera sees
            if z.id in explicit:
                owners[z.id] = explicit[z.id]
                cov = seen[self.ids.index(explicit[z.id])]
            else:
                score = self.q[:, cells].sum(axis=1)  # sees most of it, from close up
                best = int(score.argmax())
                owners[z.id] = self.ids[best] if score[best] > 0 else None
                cov = seen[best] if score[best] > 0 else 0.0
            if owners[z.id] is None:
                self.warnings.append(f"zone {z.id}: no camera sees it, so it cannot be measured.")
            elif cov < MIN_ZONE_COVERAGE:
                self.warnings.append(f"zone {z.id}: its owner {owners[z.id]} sees only {cov * 100:.0f}% of it; "
                                     f"visits near the unseen part are missed. Move a camera or change ownership.")
        return owners

    def zone_owners(self) -> dict[str, str | None]:
        return dict(self._zone_owner)

    def entrance_camera(self) -> str:
        e = self.cfg.entrance
        if e.camera:
            return e.camera
        ents = [c.cfg.id for c in self.cams if c.cfg.role in ("entrance", "both")]
        return ents[0] if ents else self.ids[0]

    def owner_index(self, floor_xy: np.ndarray) -> np.ndarray:
        """Index of the owning camera for each floor point (-1 = nobody sees it)."""
        pts = np.asarray(floor_xy, float).reshape(-1, 2)
        if len(pts) == 0:
            return np.zeros(0, int)
        q = np.stack([self._quality(c, pts) for c in self.cams])
        own = np.where(q.max(axis=0) > 0, q.argmax(axis=0), -1)
        # the entrance camera owns the lobby: a door crossing must be counted exactly once, by one camera
        e = self.cfg.entrance
        a, b = np.asarray(e.p1, float), np.asarray(e.p2, float)
        ab = b - a
        t = np.clip(((pts - a) @ ab) / max(float(ab @ ab), 1e-9), 0, 1)
        near_door = np.hypot(*(pts - (a + t[:, None] * ab)).T) <= LOBBY_M
        ent = self.ids.index(self.entrance_camera())
        own = np.where(near_door & (q[ent] > 0), ent, own)
        # a display belongs wholly to its owner (a person standing in it is that camera's, whatever the grid says)
        for z in self.cfg.zones:
            o = self._zone_owner.get(z.id)
            if o is None:
                continue
            inside = points_in_polygon(pts, np.asarray(z.polygon))
            oi = self.ids.index(o)
            own = np.where(inside & (q[oi] > 0), oi, own)
            own = np.where(inside & (q[oi] <= 0), -1, own)
        return own

    def accepts(self, camera_id: str, floor_xy: np.ndarray) -> np.ndarray:
        """True for floor points this camera is responsible for. A single-camera store accepts everything."""
        pts = np.asarray(floor_xy, float).reshape(-1, 2)
        if len(self.cams) == 1:
            return np.ones(len(pts), bool)
        return self.owner_index(pts) == self.ids.index(camera_id)

    # -- report ------------------------------------------------------------------------------------
    def coverage(self) -> dict:
        seen = self.q > 0
        n_seen = seen.sum(axis=0)
        own = np.where(self.q.max(axis=0) > 0, self.q.argmax(axis=0), -1)
        total = len(n_seen)
        out = {
            "cell_m": self.cell, "nx": self.nx, "ny": self.ny,
            "covered_fraction": float((n_seen > 0).mean()),
            "overlap_fraction": float((n_seen > 1).mean()),
            "blind_fraction": float((n_seen == 0).mean()),
            "per_camera_fraction": {cid: float(seen[i].mean()) for i, cid in enumerate(self.ids)},
            "owned_fraction": {cid: float((own == i).mean()) for i, cid in enumerate(self.ids)},
            "owner_grid": own.reshape(self.ny, self.nx).tolist(),
            "zone_owner": self.zone_owners(),
            "zone_coverage": {}, "warnings": list(self.warnings),
        }
        for z in self.cfg.zones:
            cells = self._zone_cells(z.id)
            o = self._zone_owner.get(z.id)
            if cells.any() and o is not None:
                out["zone_coverage"][z.id] = float(seen[self.ids.index(o)][cells].mean())
        # blind regions of non-trivial size (fraction of the whole floor, ignoring staff-only area)
        blind = (n_seen == 0)
        for z in self.cfg.zones:
            if z.kind == "staff":
                blind &= ~self._zone_cells(z.id)
        out["blind_fraction"] = float(blind.mean())
        if out["blind_fraction"] > 0.05:
            out["warnings"].append(f"{out['blind_fraction'] * 100:.0f}% of the floor is not seen by any camera: people "
                                   f"there are missed (heat map and flows will show holes).")
        if total and out["covered_fraction"] < 0.8:
            out["warnings"].append(f"Only {out['covered_fraction'] * 100:.0f}% of the floor is covered.")
        return out

    def describe(self) -> dict:
        """What the dashboard shows in 'Cameras & coverage' (no calibration numbers that need a video)."""
        cams = []
        for i, c in enumerate(self.cams):
            cams.append({"id": c.cfg.id, "name": c.cfg.name or c.cfg.id, "role": c.cfg.role,
                         "pose": c.cfg.pose.model_dump() if c.cfg.pose else None,
                         "calibration": {k: v for k, v in calibration_report(self.cfg, c.cfg).items()
                                         if k in ("rmse_cm", "loo_rmse_cm", "n_points", "status", "warnings",
                                                  "hull_fraction", "cm_per_px_centre")}})
        cov = self.coverage()
        return {"cameras": cams, "coverage": cov, "layout_version": self.cfg.layout_version,
                "multi_camera": self.cfg.multi_camera}


# ----------------------------------------------------------------------------------------------- picture

_PALETTE = [(237, 149, 100), (90, 180, 90), (80, 150, 230), (190, 110, 200), (200, 200, 90)]  # BGR


def render_coverage_png(cfg: StoreConfig, net: "CameraNetwork", path: str, ppm: int = 50) -> str:
    """Floor plan coloured by owning camera, overlap darker, blind spots hatched, zones and camera positions on top."""
    W, H = int(cfg.floor.width_m * ppm), int(cfg.floor.height_m * ppm)
    img = np.full((H, W, 3), 250, np.uint8)
    seen = (net.q > 0)
    n_seen = seen.sum(axis=0).reshape(net.ny, net.nx)
    own = np.where(net.q.max(axis=0) > 0, net.q.argmax(axis=0), -1).reshape(net.ny, net.nx)
    c = net.cell * ppm
    for j in range(net.ny):
        for i in range(net.nx):
            x0, y0, x1, y1 = int(i * c), int(j * c), int((i + 1) * c), int((j + 1) * c)
            if own[j, i] < 0:
                img[y0:y1, x0:x1] = (225, 225, 240)
                if (i + j) % 2 == 0:
                    img[y0:y1, x0:x1] = (200, 200, 230)
            else:
                col = np.array(_PALETTE[own[j, i] % len(_PALETTE)], float)
                a = 0.55 if n_seen[j, i] > 1 else 0.28
                img[y0:y1, x0:x1] = (250 * (1 - a) + col * a).astype(np.uint8)
    for z in cfg.zones:
        poly = (np.asarray(z.polygon) * ppm).astype(np.int32)
        cv2.polylines(img, [poly], True, (60, 60, 60), 2 if z.kind == "fixture" else 1, cv2.LINE_AA)
        o = net.zone_owners().get(z.id)
        label = f"{z.name} [{o or 'none'}]"
        cx, cy = poly.mean(axis=0).astype(int)
        cv2.putText(img, label, (int(cx) - 6 * len(label) // 2 // 1 + 10, int(cy)), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    (30, 30, 30), 1, cv2.LINE_AA)
    e = cfg.entrance
    cv2.line(img, tuple((np.asarray(e.p1) * ppm).astype(int)), tuple((np.asarray(e.p2) * ppm).astype(int)),
             (40, 40, 220), 4, cv2.LINE_AA)
    for i, cam in enumerate(cfg.cameras):
        if cam.pose is None:
            continue
        p = cam.pose
        ang = math.radians(p.heading_deg)
        x, y = p.x * ppm, p.y * ppm
        for da in (-p.fov_deg / 2, p.fov_deg / 2):
            a2 = ang + math.radians(da)
            cv2.line(img, (int(x), int(y)), (int(x + 2.5 * ppm * math.cos(a2)), int(y + 2.5 * ppm * math.sin(a2))),
                     _PALETTE[i % len(_PALETTE)], 1, cv2.LINE_AA)
        cv2.circle(img, (int(x), int(y)), 7, _PALETTE[i % len(_PALETTE)], -1, cv2.LINE_AA)
        cv2.circle(img, (int(x), int(y)), 7, (30, 30, 30), 1, cv2.LINE_AA)
        cv2.putText(img, f"{cam.id} ({cam.role})", (int(x) + 10, int(y) - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (30, 30, 30), 1, cv2.LINE_AA)
    cv2.imwrite(path, img)
    return path
