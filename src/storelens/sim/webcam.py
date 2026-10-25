"""A simulated wall-mounted "webcam" view of the synthetic store.

The analytics pipeline runs on the overhead camera (``render.py``). This module draws what an ordinary
perspective camera in the front corner of the same store would see, from the same ground-truth people, so the
dashboard can show a generic live feed next to the floor plan. It is a stylised render (low-poly people,
shelves in perspective), meant to look like a camera feed for demos, not photoreal.

Heads are mosaicked before the frame leaves this module, the same way the product blurs real footage.
"""
from __future__ import annotations

import math
import zlib

import cv2
import numpy as np

from ..config import PrivacyConfig
from ..privacy import obscure, privacy_regions
from .layout import Layout

SIZE = (960, 540)
CAM_POS = (17.3, 11.35, 3.0)      # high in the front-right corner, looking back across the store
CAM_TARGET = (7.4, 5.0, 0.0)
HFOV_DEG = 92.0
ZNEAR = 0.12
WALL_H = 3.5

FIXTURE_SPECS = {  # id -> (height m, kind)
    "front_table": (0.85, "table"), "wall_a": (2.2, "shelf"), "gondola_c": (1.45, "shelf"),
    "endcap_b": (1.65, "shelf"), "back_promo": (2.05, "shelf"),
}
COUNTER_H = 1.02
ACCENTS = [(40, 150, 210), (120, 90, 40), (60, 130, 60), (150, 60, 100), (200, 170, 60), (90, 90, 200), (170, 170, 175)]
PANTS = [(55, 45, 38), (70, 62, 55), (85, 70, 50), (45, 45, 52), (100, 92, 80)]
SKIN = [(130, 165, 205), (110, 140, 185), (85, 115, 160), (70, 95, 135)]

Rect = tuple[float, float, float, float]
HEAD_BLUR = PrivacyConfig(blur="head", head_fraction=0.19, margin=0.35)


class Camera:
    """Pinhole camera. ``right`` is derived from the plan (x right, y down) so the image is not mirrored."""

    def __init__(self, pos=CAM_POS, target=CAM_TARGET, hfov_deg=HFOV_DEG, size=SIZE):
        self.C = np.array(pos, float)
        f = np.array(target, float) - self.C
        f /= np.linalg.norm(f)
        right = np.array([-f[1], f[0], 0.0])
        right /= np.linalg.norm(right)
        up = np.cross(right, f)
        if up[2] < 0:
            up = -up
        self.R = np.stack([right, up, f])
        self.W, self.H = size
        self.fpx = (self.W / 2) / math.tan(math.radians(hfov_deg) / 2)
        self.cx, self.cy = self.W / 2, self.H / 2
        self.fwd = f

    def cam(self, P) -> np.ndarray:
        return (np.asarray(P, float) - self.C) @ self.R.T

    def to_px(self, pc: np.ndarray) -> np.ndarray:
        z = np.maximum(pc[..., 2], 1e-6)
        return np.stack([self.cx + self.fpx * pc[..., 0] / z, self.cy - self.fpx * pc[..., 1] / z], -1)

    def project(self, P) -> tuple[np.ndarray, np.ndarray]:
        pc = self.cam(P)
        return self.to_px(pc), pc[..., 2]

    def poly(self, pts_world) -> np.ndarray | None:
        """World polygon -> clipped pixel polygon (None if it is entirely behind the camera)."""
        pc = self.cam(pts_world)
        out = []
        n = len(pc)
        for i in range(n):
            a, b = pc[i], pc[(i + 1) % n]
            ia, ib = a[2] >= ZNEAR, b[2] >= ZNEAR
            if ia:
                out.append(a)
            if ia != ib:
                out.append(a + (b - a) * ((ZNEAR - a[2]) / (b[2] - a[2])))
        if len(out) < 3:
            return None
        return np.clip(self.to_px(np.array(out)), -20000, 20000)

    def fill(self, img, pts_world, color, aa=True) -> None:
        p = self.poly(pts_world)
        if p is not None:
            cv2.fillPoly(img, [np.round(p).astype(np.int32)], color, cv2.LINE_AA if aa else cv2.LINE_8)

    def seg(self, img, a, b, color, thickness=1) -> None:
        pa, pb = self.cam(a), self.cam(b)
        if pa[2] < ZNEAR and pb[2] < ZNEAR:
            return
        if pa[2] < ZNEAR:
            pa = pa + (pb - pa) * ((ZNEAR - pa[2]) / (pb[2] - pa[2]))
        if pb[2] < ZNEAR:
            pb = pb + (pa - pb) * ((ZNEAR - pb[2]) / (pa[2] - pb[2]))
        q = np.clip(np.round(self.to_px(np.stack([pa, pb]))), -20000, 20000).astype(int)
        cv2.line(img, tuple(q[0]), tuple(q[1]), color, thickness, cv2.LINE_AA)


def box_faces(x0, y0, x1, y1, z0, z1) -> dict:
    return {
        "top": ([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], (0, 0, 1)),
        "n": ([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], (0, -1, 0)),
        "s": ([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], (0, 1, 0)),
        "w": ([(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)], (-1, 0, 0)),
        "e": ([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)], (1, 0, 0)),
    }


def _shade(col, k):
    return tuple(int(max(0, min(255, c * k))) for c in col)


def _seg_hits_rect(p, q, r: Rect) -> bool:
    """Liang-Barsky: does the plan segment p->q cross rectangle r?"""
    x0, y0, x1, y1 = r
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if abs(pp) < 1e-12:
            if qq < 0:
                return False
        else:
            t = qq / pp
            if pp < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


class WebcamScene:
    def __init__(self, layout: Layout, size=SIZE, seed: int = 3):
        self.layout = layout
        self.cam = Camera(size=size)
        self.W, self.H = size
        self.rng = np.random.default_rng(seed)
        self.C_plan = self.cam.C[:2]
        self.base = self._base()
        self.items = self._fixture_items()
        self.noise = [np.round(self.rng.normal(0, 1.6, (self.H, self.W, 1))).astype(np.int16) for _ in range(8)]
        yy, xx = np.mgrid[0:self.H, 0:self.W].astype(np.float32)
        r = np.sqrt(((xx - self.W / 2) / (self.W / 2)) ** 2 + ((yy - self.H / 2) / (self.H / 2)) ** 2)
        self.vig = np.repeat(((1.0 - 0.14 * np.clip(r, 0, 1.5) ** 2) * 255).astype(np.uint8)[..., None], 3, axis=2)
        self._t = 0

    # ------------------------------------------------------------------------------- static scene
    def _base(self) -> np.ndarray:
        L, cam = self.layout, self.cam
        img = np.zeros((self.H, self.W, 3), np.uint8)
        img[:] = (150, 150, 150)
        # walls
        cam.fill(img, [(0, 0, 0), (L.width, 0, 0), (L.width, 0, WALL_H), (0, 0, WALL_H)], (236, 238, 240))        # back
        cam.fill(img, [(0, 0, 0), (L.width, 0, 0), (L.width, 0, 0.95), (0, 0, 0.95)], (214, 218, 222))           # wainscot
        cam.fill(img, [(0, 0, 0), (0, L.height, 0), (0, L.height, WALL_H), (0, 0, WALL_H)], (228, 231, 234))      # left
        cam.fill(img, [(0, 0, 0), (0, L.height, 0), (0, L.height, 0.95), (0, 0, 0.95)], (210, 214, 218))
        cam.fill(img, [(L.width, 0, 0), (L.width, L.height, 0), (L.width, L.height, WALL_H), (L.width, 0, WALL_H)],
                 (219, 222, 226))                                                                                  # right
        cam.fill(img, [(L.width, 0, 0), (L.width, L.height, 0), (L.width, L.height, 0.95), (L.width, 0, 0.95)],
                 (200, 204, 208))
        # floor
        cam.fill(img, [(0, 0, 0), (L.width, 0, 0), (L.width, L.height, 0), (0, L.height, 0)], (203, 207, 211))
        for gx in range(int(L.width) + 1):
            cam.seg(img, (gx, 0, 0), (gx, L.height, 0), (190, 194, 198))
        for gy in range(int(L.height) + 1):
            cam.seg(img, (0, gy, 0), (L.width, gy, 0), (190, 194, 198))
        # soft floor lighting: brighter pools under the ceiling lights
        glow = np.zeros((self.H, self.W), np.float32)
        for lx in (4.5, 9.0, 13.5):
            for ly in (2.5, 6.5, 10.0):
                p, z = cam.project((lx, ly, 0.0))
                if z > 0.5:
                    cv2.circle(glow, (int(p[0]), int(p[1])), int(max(30, 700 / z)), 1.0, -1)
        glow = cv2.GaussianBlur(glow, (0, 0), 40)
        img = np.clip(img.astype(np.float32) * (0.9 + 0.16 * glow[..., None]), 0, 255).astype(np.uint8)
        # front wall with shop-front glass and the door
        d0, d1 = L.door_x
        cam.fill(img, [(0, L.height, 0), (d0, L.height, 0), (d0, L.height, WALL_H), (0, L.height, WALL_H)], (205, 212, 214))
        cam.fill(img, [(d1, L.height, 0), (L.width, L.height, 0), (L.width, L.height, WALL_H), (d1, L.height, WALL_H)], (205, 212, 214))
        for xa, xb in ((0.4, d0 - 0.4), (d1 + 0.4, L.width - 0.4)):
            cam.fill(img, [(xa, L.height, 0.45), (xb, L.height, 0.45), (xb, L.height, 2.55), (xa, L.height, 2.55)], (236, 232, 218))
        cam.fill(img, [(d0, L.height, 0), (d1, L.height, 0), (d1, L.height, 2.4), (d0, L.height, 2.4)], (250, 250, 247))
        for xx in (d0, d1):
            cam.seg(img, (xx, L.height, 0), (xx, L.height, 2.4), (120, 125, 130), 2)
        cam.seg(img, (d0, L.height, 2.4), (d1, L.height, 2.4), (120, 125, 130), 2)
        cam.fill(img, [(d0, 11.45, 0.003), (d1, 11.45, 0.003), (d1, L.height, 0.003), (d0, L.height, 0.003)], (150, 158, 164))
        # staff door on the right wall, promo banner on the back wall
        cam.fill(img, [(L.width, 6.8, 0), (L.width, 7.8, 0), (L.width, 7.8, 2.1), (L.width, 6.8, 2.1)], (86, 86, 94))
        cam.fill(img, [(6.4, 0, 2.25), (11.6, 0, 2.25), (11.6, 0, 2.85), (6.4, 0, 2.85)], (60, 120, 225))
        cam.fill(img, [(6.4, 0, 2.25), (7.2, 0, 2.25), (7.2, 0, 2.85), (6.4, 0, 2.85)], (40, 90, 200))
        for xa in (3.0, 14.0):  # framed posters
            cam.fill(img, [(xa, 0, 1.5), (xa + 1.1, 0, 1.5), (xa + 1.1, 0, 2.4), (xa, 0, 2.4)], (170, 120, 60))
            cam.fill(img, [(xa + 0.1, 0, 1.6), (xa + 1.0, 0, 1.6), (xa + 1.0, 0, 2.3), (xa + 0.1, 0, 2.3)], (225, 232, 238))
        # ceiling strip
        cam.fill(img, [(0, 0, WALL_H), (L.width, 0, WALL_H), (L.width, L.height, WALL_H), (0, L.height, WALL_H)], (226, 228, 230))
        for lx in (3.0, 7.5, 12.0, 16.0):
            for ly in (1.5, 4.5, 7.5, 10.5):
                cam.fill(img, [(lx, ly, WALL_H - 0.01), (lx + 1.4, ly, WALL_H - 0.01), (lx + 1.4, ly + 0.45, WALL_H - 0.01),
                               (lx, ly + 0.45, WALL_H - 0.01)], (252, 252, 250))
        return img

    # ------------------------------------------------------------------------------- fixtures
    def _draw_box(self, layer, mask, rect, z0, z1, top, side, shelf=False, seed=0):
        cam = self.cam
        faces = box_faces(rect[0], rect[1], rect[2], rect[3], z0, z1)
        vis = []
        for name, (pts, n) in faces.items():
            c = np.mean(np.array(pts), axis=0)
            if np.dot(n, cam.C - c) > 0:
                vis.append((np.linalg.norm(cam.C - c), name, pts, n))
        for _, name, pts, n in sorted(vis, reverse=True):
            col = top if name == "top" else _shade(side, 1.0 if abs(n[1]) > 0.5 else 0.82)
            cam.fill(layer, pts, col)
            cam.fill(mask, pts, 255)
            if shelf and name != "top":
                self._shelves(layer, pts, seed + zlib.crc32(name.encode()) % 97)

    def _shelves(self, layer, pts, seed):
        cam, rng = self.cam, np.random.default_rng(seed)
        A, B, _, D = [np.array(p, float) for p in pts]
        width, height = np.linalg.norm(B - A), np.linalg.norm(D - A)
        if width < 0.2:
            return
        up = (D - A) / height
        along = (B - A) / width
        levels = np.arange(0.18, height - 0.15, 0.36)
        for z in levels:
            base = A + up * z
            cam.fill(layer, [base, base + along * width, base + along * width + up * 0.035, base + up * 0.035], (40, 55, 78))
            u = 0.04
            while u < width - 0.1:
                w = float(rng.uniform(0.12, 0.24))
                h = float(rng.uniform(0.12, 0.26))
                w = min(w, width - 0.04 - u)
                p0 = base + along * u + up * 0.035
                col = ACCENTS[int(rng.integers(len(ACCENTS)))]
                cam.fill(layer, [p0, p0 + along * w, p0 + along * w + up * h, p0 + up * h], col)
                u += w + float(rng.uniform(0.01, 0.05))

    def _fixture_items(self) -> list[dict]:
        L, items = self.layout, []
        wood_top, wood_side = (110, 146, 184), (88, 120, 158)
        for f in L.fixtures:
            h, kind = FIXTURE_SPECS[f.id]
            layer = np.zeros((self.H, self.W, 3), np.uint8)
            mask = np.zeros((self.H, self.W), np.uint8)
            self._draw_box(layer, mask, f.obstacle, 0.0, h, wood_top, wood_side, shelf=(kind == "shelf"), seed=zlib.crc32(f.id.encode()) % 1000)
            if kind == "table":  # product stacks on the table top
                x0, y0, x1, y1 = f.obstacle
                rng = np.random.default_rng(5)
                for ix in range(6):
                    for iy in range(2):
                        cx = x0 + 0.3 + ix * (x1 - x0 - 0.6) / 5
                        cy = y0 + 0.3 + iy * (y1 - y0 - 0.6)
                        col = ACCENTS[int(rng.integers(len(ACCENTS)))]
                        self._draw_box(layer, mask, (cx - 0.16, cy - 0.12, cx + 0.16, cy + 0.12), h, h + 0.1 + 0.05 * (ix % 3),
                                       _shade(col, 1.15), col)
            items.append(self._sprite(f.id, f.obstacle, layer, mask))
        x0, y0, x1, y1 = L.checkout_obstacle
        layer = np.zeros((self.H, self.W, 3), np.uint8)
        mask = np.zeros((self.H, self.W), np.uint8)
        self._draw_box(layer, mask, L.checkout_obstacle, 0.0, COUNTER_H, (182, 188, 194), (92, 104, 124))
        self._draw_box(layer, mask, (16.3, 8.3, 16.9, 8.8), COUNTER_H, COUNTER_H + 0.28, (80, 80, 86), (60, 60, 66))
        items.append(self._sprite("checkout", L.checkout_obstacle, layer, mask))
        return items

    def _sprite(self, fid, rect, layer, mask) -> dict:
        ys, xs = np.nonzero(mask)
        if len(xs) == 0:
            return {"id": fid, "rect": rect, "bbox": None}
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        c = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
        return {"id": fid, "rect": rect, "bbox": (y0, y1, x0, x1), "bgr": layer[y0:y1, x0:x1].copy(),
                "mask": mask[y0:y1, x0:x1] > 0, "dist": float(np.hypot(c[0] - self.C_plan[0], c[1] - self.C_plan[1]))}

    # ------------------------------------------------------------------------------- people
    def _figure(self, img, p: dict) -> tuple[float, float, float] | None:
        """Draw one person; returns the head centre (px) and radius for the privacy mosaic."""
        cam = self.cam
        x, y, hd = p["x"], p["y"], p["h"]
        mann = p.get("mannequin", False)
        pid = p.get("pid", 0)
        H = 1.70 if mann else 1.56 + 0.04 * (pid * 7 % 7)
        spd = p.get("speed", 0.0)
        fwd = np.array([math.cos(hd), math.sin(hd), 0.0])
        lat = np.array([-math.sin(hd), math.cos(hd), 0.0])
        P = np.array([x, y, 0.0])
        phase = p.get("dist", 0.0) / 0.78 * 2 * math.pi
        amp = 0.30 * min(1.0, spd / 1.1)
        shirt = (212, 212, 216) if mann else p["shirt"]
        pants = (200, 200, 204) if mann else PANTS[pid % len(PANTS)]
        skin = (205, 205, 210) if mann else SKIN[pid % len(SKIN)]
        hair = (190, 190, 195) if mann else p["hair"]
        vest = p.get("role") == "staff"
        edge = _shade(shirt, 0.55)

        def pt(base, dz):  # world point at height dz
            return base + np.array([0, 0, dz])

        sh_l, sh_r = pt(P + lat * 0.20, 0.82 * H), pt(P - lat * 0.20, 0.82 * H)
        hp_l, hp_r = pt(P + lat * 0.13, 0.52 * H), pt(P - lat * 0.13, 0.52 * H)
        legs = []
        for s in (1, -1):
            off = amp * math.sin(phase + (0 if s > 0 else math.pi))
            lift = max(0.0, math.cos(phase + (0 if s > 0 else math.pi))) * 0.05 * min(1.0, spd / 1.0)
            hip = pt(P + lat * 0.09 * s, 0.52 * H)
            foot = pt(P + lat * 0.09 * s + fwd * off, lift)
            legs.append((hip, foot))
        arms = []
        for s in (1, -1):
            off = -amp * 0.8 * math.sin(phase + (0 if s > 0 else math.pi))
            hand = pt(P + lat * 0.25 * s + fwd * off, 0.50 * H)
            arms.append((pt(P + lat * 0.20 * s, 0.82 * H), hand))
        head_c = pt(P + fwd * 0.015, 0.935 * H)

        def thick(world_pt, width):
            q, z = cam.project(world_pt)
            return max(2, int(round(cam.fpx * width / max(z, 0.2))))

        def limb(a, b, width, col):
            pa, za = cam.project(a)
            pb, zb = cam.project(b)
            if za < ZNEAR or zb < ZNEAR:
                return
            t = thick((a + b) / 2, width)
            a_i, b_i = tuple(np.round(pa).astype(int)), tuple(np.round(pb).astype(int))
            cv2.line(img, a_i, b_i, col, t, cv2.LINE_AA)
            cv2.circle(img, b_i, max(1, t // 2), col, -1, cv2.LINE_AA)
            cv2.circle(img, a_i, max(1, t // 2), col, -1, cv2.LINE_AA)

        order = lambda pairs: sorted(pairs, key=lambda ab: -cam.cam(ab[1])[2])  # far limb first
        for hip, foot in order(legs):
            limb(hip, foot, 0.125, pants)
            q, z = cam.project(foot)
            if z > ZNEAR:  # shoe
                cv2.circle(img, tuple(np.round(q).astype(int)), max(2, thick(foot, 0.10) // 2 + 1), (35, 35, 38), -1, cv2.LINE_AA)
        arms_sorted = order(arms)
        far_arm, near_arm = arms_sorted[0], arms_sorted[1]
        limb(*far_arm, 0.075, shirt)
        torso = [sh_l, sh_r, hp_r, hp_l]
        cam.fill(img, torso, shirt)
        poly = cam.poly(torso)
        if poly is not None:
            cv2.polylines(img, [np.round(poly).astype(np.int32)], True, edge, 1, cv2.LINE_AA)
        if vest:  # hi-vis staff vest over a dark shirt
            cam.fill(img, [pt(P + lat * 0.19, 0.80 * H), pt(P - lat * 0.19, 0.80 * H), pt(P - lat * 0.125, 0.55 * H),
                           pt(P + lat * 0.125, 0.55 * H)], (35, 35, 205))
        limb(*near_arm, 0.075, (60, 60, 66) if vest else shirt)
        # head: skin toward the camera, hair when facing away
        hp, hz = cam.project(head_c)
        if hz < ZNEAR:
            return None
        r = cam.fpx * 0.105 / hz
        c = (int(round(hp[0])), int(round(hp[1])))
        to_cam = cam.C[:2] - np.array([x, y])
        to_cam /= max(np.linalg.norm(to_cam), 1e-6)
        faces_cam = float(np.dot(fwd[:2], to_cam)) > -0.1
        cv2.ellipse(img, c, (int(round(r)), int(round(r * 1.12))), 0, 0, 360, skin if faces_cam else hair, -1, cv2.LINE_AA)
        if faces_cam and not mann:
            cv2.ellipse(img, (c[0], c[1] - int(r * 0.2)), (int(round(r * 1.05)), int(round(r * 0.95))), 0, 180, 360, hair, -1, cv2.LINE_AA)
        foot_px, fz = cam.project(P)
        half_w = cam.fpx * 0.30 / max(fz, 0.2)
        box = [float(hp[0]) - half_w, float(hp[1]) - 1.15 * r, float(hp[0]) + half_w, float(max(foot_px[1], hp[1] + 4 * r))]
        return float(hp[0]), float(hp[1]), float(r), box

    # ------------------------------------------------------------------------------- frame
    def render(self, people: list[dict], zone_state: dict[str, bool] | None = None, clock: str = "",
               show_overlay: bool = True) -> np.ndarray:
        cam, L = self.cam, self.layout
        img = self.base.copy()
        # zone overlays on the floor
        if show_overlay:
            ov = img.copy()
            for f in L.fixtures:
                x0, y0, x1, y1 = f.exposure
                poly = [(x0, y0, 0.004), (x1, y0, 0.004), (x1, y1, 0.004), (x0, y1, 0.004)]
                engaged = bool(zone_state and zone_state.get(f.id))
                if engaged:
                    cam.fill(ov, poly, (110, 210, 90))
                p = cam.poly(poly)
                if p is not None:
                    cv2.polylines(ov, [np.round(p).astype(np.int32)], True, (110, 210, 90) if engaged else (235, 160, 70),
                                  2 if engaged else 1, cv2.LINE_AA)
            img = cv2.addWeighted(ov, 0.42, img, 0.58, 0)
        # soft contact shadows
        sh = np.zeros((self.H, self.W), np.uint8)
        for p in people:
            ang = np.linspace(0, 2 * math.pi, 14, endpoint=False)
            ring = [(p["x"] + 0.30 * math.cos(a), p["y"] + 0.22 * math.sin(a), 0.0) for a in ang]
            cam.fill(sh, ring, 255)
        if sh.any():
            m = cv2.GaussianBlur(sh, (0, 0), 3).astype(np.float32) / 255.0
            img = (img.astype(np.float32) * (1.0 - 0.38 * m[..., None])).astype(np.uint8)
        # painter's order: fixtures far->near, each person slotted around the fixtures that can hide it
        fx = sorted([it for it in self.items if it["bbox"] is not None], key=lambda it: -it["dist"])
        slots: list[list[tuple[float, dict]]] = [[] for _ in range(len(fx) + 1)]
        for p in people:
            pos = (p["x"], p["y"])
            d = math.hypot(pos[0] - self.C_plan[0], pos[1] - self.C_plan[1])
            shrunk = lambda r: (r[0] + 0.03, r[1] + 0.03, r[2] - 0.03, r[3] - 0.03)
            behind = [i for i, it in enumerate(fx) if _seg_hits_rect(self.C_plan, pos, shrunk(it["rect"]))]
            slot = min(behind) if behind else sum(1 for it in fx if it["dist"] > d)
            slots[slot].append((d, p))
        heads = []
        for i in range(len(fx) + 1):
            for _, p in sorted(slots[i], key=lambda dp: -dp[0]):
                hd = self._figure(img, p)
                if hd is not None:
                    heads.append((p, hd))
            if i < len(fx):
                it = fx[i]
                y0, y1, x0, x1 = it["bbox"]
                region = img[y0:y1, x0:x1]
                region[it["mask"]] = it["bgr"][it["mask"]]
        # privacy first: mosaic every head, then draw labels/tags on top of the blurred frame
        boxes = [hd[3] for p, hd in heads if not p.get("mannequin")]
        if boxes:  # the product's own head-region rule (oblique camera: top slice of each person box)
            img = obscure(img, privacy_regions(np.array(boxes, float), "bottom_center", HEAD_BLUR, img.shape))
        if show_overlay:
            for it in self.items:
                if it["id"] in ("checkout",):
                    continue
                f = L.fixture(it["id"])
                h = FIXTURE_SPECS[it["id"]][0]
                c = ((f.obstacle[0] + f.obstacle[2]) / 2, (f.obstacle[1] + f.obstacle[3]) / 2, h + 0.12)
                q, z = cam.project(c)
                if z > 0.5 and 0 <= q[0] < self.W and 0 <= q[1] < self.H:
                    engaged = bool(zone_state and zone_state.get(it["id"]))
                    self._label(img, f.name, int(q[0]), int(q[1]), engaged)
        for p, (hx, hy, r, _) in heads:
            tag = p.get("tag")
            if tag:
                self._tag(img, tag, int(hx), int(hy - 2.1 * r), p.get("role") == "staff")
        return self._finish(img, clock)

    def _tag(self, img, text, cx, cy, staff):
        (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
        x0, y0 = cx - w // 2 - 4, cy - h - 4
        cv2.rectangle(img, (x0, y0), (x0 + w + 8, y0 + h + 6), (200, 110, 170) if staff else (60, 60, 60), -1)
        cv2.putText(img, text, (x0 + 4, y0 + h + 1), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)

    def _label(self, img, text, cx, cy, engaged):
        (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.46, 1)
        x0, y0 = cx - w // 2 - 5, cy - h - 5
        sub = img[max(y0, 0):y0 + h + 10, max(x0, 0):x0 + w + 10]
        if sub.size:
            bg = np.full_like(sub, (96, 190, 80) if engaged else (60, 50, 40))
            sub[:] = cv2.addWeighted(bg, 0.78, sub, 0.22, 0)
        cv2.putText(img, text, (x0 + 5, y0 + h + 2), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (255, 255, 255), 1, cv2.LINE_AA)

    def _finish(self, img, clock: str) -> np.ndarray:
        # sensor look: slight softness, noise, vignette, mild colour cast
        img = cv2.GaussianBlur(img, (0, 0), 0.6)
        n = self.noise[self._t % len(self.noise)]
        self._t += 1
        img = np.clip(img.astype(np.int16) + n, 0, 255).astype(np.uint8)
        img = cv2.multiply(img, self.vig, scale=1 / 255.0)
        font = cv2.FONT_HERSHEY_SIMPLEX
        bar = img[self.H - 30:, :]
        bar[:] = (bar.astype(np.float32) * 0.45).astype(np.uint8)
        cv2.putText(img, f"CAM 02  FRONT-RIGHT   {clock}", (14, self.H - 10), font, 0.5, (235, 235, 235), 1, cv2.LINE_AA)
        cv2.putText(img, "SIMULATED", (self.W - 106, self.H - 10), font, 0.5, (235, 235, 235), 1, cv2.LINE_AA)
        cv2.circle(img, (24, 24), 6, (40, 40, 235), -1, cv2.LINE_AA)
        cv2.putText(img, "LIVE", (38, 29), font, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, "LIVE", (38, 29), font, 0.55, (245, 245, 245), 1, cv2.LINE_AA)
        return img
