"""Render an overhead CCTV view of the simulated store (floor canvas -> perspective warp -> sensor noise)."""
from __future__ import annotations

import math

import cv2
import numpy as np

from .layout import CAM_DST, CAM_SIZE, PPM_FLOOR, CamSpec, Layout

FLOOR_BGR = (206, 209, 213)


class Renderer:
    def __init__(self, layout: Layout, seed: int = 0, spec: CamSpec | None = None):
        self.layout = layout
        self.rng = np.random.default_rng(seed)
        self.W = int(layout.width * PPM_FLOOR)
        self.H = int(layout.height * PPM_FLOOR)
        if spec is None:  # one camera sees the whole floor
            src = np.float32([[0, 0], [self.W, 0], [self.W, self.H], [0, self.H]])
            self.Hc = cv2.getPerspectiveTransform(src, CAM_DST)
        else:  # this camera sees one rectangle of it
            x0, y0, x1, y1 = (v * PPM_FLOOR for v in spec.rect)
            src = np.float32([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
            self.Hc = cv2.getPerspectiveTransform(src, np.float32(spec.dst))
        self.canvas = self._static_canvas()
        w, h = CAM_SIZE
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
        vig = (1.0 - 0.10 * np.clip(r, 0, 1.4) ** 2)
        self.vignette = np.repeat((vig * 255).astype(np.uint8)[..., None], 3, axis=2)  # uint8 for fast multiply
        self.noise_pool = [np.round(self.rng.normal(0, 2.3, (h, w, 3))).astype(np.int16) for _ in range(12)]

    # -- static scene -------------------------------------------------------------------------
    def _m(self, x: float, y: float) -> tuple[int, int]:
        return int(round(x * PPM_FLOOR)), int(round(y * PPM_FLOOR))

    def _static_canvas(self) -> np.ndarray:
        L = self.layout
        img = np.full((self.H, self.W, 3), FLOOR_BGR, np.uint8)
        # tile grid every metre, very faint
        for gx in range(int(L.width) + 1):
            cv2.line(img, self._m(gx, 0), self._m(gx, L.height), (198, 201, 205), 1)
        for gy in range(int(L.height) + 1):
            cv2.line(img, self._m(0, gy), self._m(L.width, gy), (198, 201, 205), 1)
        # entrance mat
        cv2.rectangle(img, self._m(L.door_x[0], 11.45), self._m(L.door_x[1], 12.0), (150, 160, 168), -1)
        # fixtures
        wood, accent = (62, 92, 128), [(40, 150, 210), (120, 90, 40), (60, 130, 60), (150, 60, 100)]
        rng = np.random.default_rng(7)
        for f in L.fixtures:
            x0, y0, x1, y1 = f.obstacle
            cv2.rectangle(img, self._m(x0, y0), self._m(x1, y1), wood, -1)
            cv2.rectangle(img, self._m(x0, y0), self._m(x1, y1), (35, 55, 85), 2)
            n = int(max(x1 - x0, y1 - y0) / 0.35)
            for _ in range(n):
                px, py = rng.uniform(x0 + 0.05, x1 - 0.05), rng.uniform(y0 + 0.05, y1 - 0.05)
                cv2.circle(img, self._m(px, py), 4, accent[int(rng.integers(len(accent)))], -1)
        x0, y0, x1, y1 = L.checkout_obstacle
        cv2.rectangle(img, self._m(x0, y0), self._m(x1, y1), (90, 90, 95), -1)
        cv2.rectangle(img, self._m(x0, y0), self._m(x1, y1), (50, 50, 55), 2)
        # mannequins are part of the empty-store background (a real detector would see them!)
        for mx, my in L.mannequins:
            cv2.circle(img, self._m(mx, my), int(0.26 * PPM_FLOOR), (140, 146, 152), -1, cv2.LINE_AA)
            cv2.circle(img, self._m(mx, my), int(0.10 * PPM_FLOOR), (110, 114, 120), -1, cv2.LINE_AA)
        return img

    # -- dynamic frame ------------------------------------------------------------------------
    def frame(self, people: list[tuple[float, float, float, tuple, tuple]], t: float) -> np.ndarray:
        """people: list of (x, y, heading, clothing_bgr, hair_bgr) in floor metres."""
        img = self.canvas.copy()
        a_sh, b_sh = 0.23 * PPM_FLOOR, 0.14 * PPM_FLOOR
        for x, y, h, cloth, hair in people:
            cx, cy = x * PPM_FLOOR, y * PPM_FLOOR
            ang = math.degrees(h + math.pi / 2)
            # soft shadow
            cv2.ellipse(img, (int(cx + 3), int(cy + 3)), (int(a_sh + 1), int(b_sh + 1)), ang, 0, 360,
                        (186, 189, 193), -1, cv2.LINE_AA)
            cv2.ellipse(img, (int(round(cx)), int(round(cy))), (int(a_sh), int(b_sh)), ang, 0, 360, cloth, -1,
                        cv2.LINE_AA)
            hx, hy = cx + math.cos(h) * 0.035 * PPM_FLOOR, cy + math.sin(h) * 0.035 * PPM_FLOOR
            cv2.circle(img, (int(round(hx)), int(round(hy))), int(0.10 * PPM_FLOOR), hair, -1, cv2.LINE_AA)
        cam = cv2.warpPerspective(img, self.Hc, CAM_SIZE, flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_CONSTANT, borderValue=(28, 28, 30))
        gain = 1.0 + 0.025 * math.sin(2 * math.pi * t / 420.0) + float(self.rng.normal(0, 0.004))
        out = cv2.multiply(cam, self.vignette, scale=gain / 255.0)  # vignette + slow lighting drift
        out = cv2.GaussianBlur(out, (3, 3), 0.7)
        return cv2.add(out, self.noise_pool[int(self.rng.integers(len(self.noise_pool)))], dtype=cv2.CV_8U)
