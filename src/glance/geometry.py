"""Camera calibration (pixel <-> floor homography) and small vectorised geometry helpers."""
from __future__ import annotations

import cv2
import numpy as np

from .config import CameraConfig, StoreConfig


class Homography:
    """Maps camera pixels to floor-plan metres (and back).

    Calibration quality is the classic *silent* failure of CCTV analytics: a bad homography
    still produces plausible-looking heatmaps. We therefore expose the reprojection error and
    ship ``glance calibrate`` to overlay the zones on a real frame for a human to eyeball.
    """

    def __init__(self, pixel_pts, floor_pts):
        px = np.asarray(pixel_pts, dtype=np.float64).reshape(-1, 2)
        fl = np.asarray(floor_pts, dtype=np.float64).reshape(-1, 2)
        method = 0 if len(px) == 4 else cv2.LMEDS
        H, _ = cv2.findHomography(px, fl, method)
        if H is None:
            raise ValueError("could not estimate homography from calibration points")
        self.H = H
        self.Hinv = np.linalg.inv(H)
        self._px, self._fl = px, fl

    @classmethod
    def from_camera(cls, cam: CameraConfig) -> "Homography":
        return cls(cam.pixel_points, cam.floor_points)

    def to_floor(self, pts) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
        if len(pts) == 0:
            return np.zeros((0, 2))
        return cv2.perspectiveTransform(pts, self.H).reshape(-1, 2)

    def to_pixel(self, pts) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
        if len(pts) == 0:
            return np.zeros((0, 2))
        return cv2.perspectiveTransform(pts, self.Hinv).reshape(-1, 2)

    def reprojection_rmse_cm(self) -> float:
        err = self.to_floor(self._px) - self._fl
        return float(np.sqrt((err ** 2).sum(axis=1).mean()) * 100.0)


def points_in_polygon(pts: np.ndarray, poly) -> np.ndarray:
    """Vectorised even-odd ray casting. pts: (N,2); poly: (M,2). Returns bool (N,)."""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    poly = np.asarray(poly, dtype=np.float64)
    x, y = pts[:, 0], pts[:, 1]
    inside = np.zeros(len(pts), dtype=bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        cond = (yi > y) != (yj > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xcross = (xj - xi) * (y - yi) / (yj - yi) + xi
        inside ^= cond & (x < xcross)
        j = i
    return inside


def angle_diff(a: np.ndarray | float, b: np.ndarray | float) -> np.ndarray:
    """Smallest absolute difference between angles in radians, in [0, pi]."""
    d = (np.asarray(a) - np.asarray(b) + np.pi) % (2 * np.pi) - np.pi
    return np.abs(d)


def project_zones_to_pixels(cfg: StoreConfig, cam: CameraConfig) -> dict[str, np.ndarray]:
    h = Homography.from_camera(cam)
    return {z.id: h.to_pixel(np.asarray(z.polygon)) for z in cfg.zones}


def draw_calibration_overlay(frame: np.ndarray, cfg: StoreConfig, cam: CameraConfig) -> np.ndarray:
    """Draw zone polygons, targets, entrance line and calibration points on a camera frame."""
    out = frame.copy()
    h = Homography.from_camera(cam)
    palette = {"fixture": (60, 180, 255), "area": (120, 220, 120), "staff": (200, 120, 255)}
    for z in cfg.zones:
        poly = h.to_pixel(np.asarray(z.polygon)).astype(np.int32)
        col = palette[z.kind]
        overlay = out.copy()
        cv2.fillPoly(overlay, [poly], col)
        out = cv2.addWeighted(overlay, 0.18, out, 0.82, 0)
        cv2.polylines(out, [poly], True, col, 1, cv2.LINE_AA)
        c = poly.mean(axis=0).astype(int)
        cv2.putText(out, z.name, (int(c[0]) - 30, int(c[1])), cv2.FONT_HERSHEY_SIMPLEX, 0.38, col, 1, cv2.LINE_AA)
        if z.target is not None:
            t = h.to_pixel([z.target])[0].astype(int)
            cv2.drawMarker(out, (int(t[0]), int(t[1])), col, cv2.MARKER_CROSS, 10, 1)
    e = cfg.entrance
    p = h.to_pixel([e.p1, e.p2]).astype(int)
    cv2.line(out, tuple(map(int, p[0])), tuple(map(int, p[1])), (0, 0, 255), 2, cv2.LINE_AA)
    for pt in cam.pixel_points:
        cv2.circle(out, (int(pt[0]), int(pt[1])), 4, (255, 255, 0), 1, cv2.LINE_AA)
    cv2.putText(out, f"calibration RMSE {h.reprojection_rmse_cm():.1f} cm", (8, 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return out
