"""Classical background-subtraction people detector for FIXED, mostly overhead cameras.

Why it exists: it needs no model weights, runs at hundreds of fps on a laptop and is a legitimate
baseline for ceiling cameras where learned detectors (trained on side-view people) are weakest.
Why it is not the end state: it cannot tell people from carts or shadows, merges groups that touch
(we try to split them) and loses people who stand still for many minutes. Use a learned detector
(YOLOX-ONNX / RF-DETR) for oblique cameras and busy stores.
"""
from __future__ import annotations

import cv2
import numpy as np
import supervision as sv

from .base import Detector, empty_detections


class BackgroundSubtractionDetector(Detector):
    name = "bgsub"

    def __init__(self, thresh: int = 28, min_area: int = 110, max_area: int = 6000,
                 typical_area: float = 330.0, rate_bg: float = 0.01, rate_fg: float = 0.0005,
                 n_bg_samples: int = 90, split_blobs: bool = True, split_ratio: float = 1.45, mask_polygons=None):
        self.thresh = thresh
        self.min_area, self.max_area, self.typical = min_area, max_area, typical_area
        self.rate_bg, self.rate_fg = rate_bg, rate_fg
        self.n_bg_samples = n_bg_samples
        self.split_blobs = split_blobs
        self.split_ratio = split_ratio
        self.bg: np.ndarray | None = None
        self.mask_polygons = mask_polygons or []
        self._k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self._ignore: np.ndarray | None = None

    def prepare(self, video_path: str) -> None:
        """Median of frames sampled across the whole video = an (almost) empty-store background."""
        cap = cv2.VideoCapture(video_path)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if n <= 0:
            return
        idx = np.linspace(0, n - 1, min(self.n_bg_samples, n)).astype(int)
        frames = []
        for i in idx:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
            ok, f = cap.read()
            if ok:
                frames.append(f)
        cap.release()
        if frames:
            self.bg = np.median(np.stack(frames), axis=0).astype(np.float32)

    def _ignore_mask(self, shape) -> np.ndarray:
        if self._ignore is None or self._ignore.shape != shape[:2]:
            m = np.zeros(shape[:2], np.uint8)
            for poly in self.mask_polygons:
                cv2.fillPoly(m, [np.asarray(poly, np.int32)], 255)
            self._ignore = m
        return self._ignore

    def detect(self, frame: np.ndarray) -> sv.Detections:
        if self.bg is None:
            self.bg = frame.astype(np.float32)
            return empty_detections()
        bg_u8 = cv2.convertScaleAbs(self.bg)
        diff = cv2.absdiff(cv2.GaussianBlur(frame, (5, 5), 1.2), cv2.GaussianBlur(bg_u8, (5, 5), 1.2))
        b, g, r = cv2.split(diff)
        diff = cv2.max(cv2.max(b, g), r)
        _, mask = cv2.threshold(diff, self.thresh, 255, cv2.THRESH_BINARY)
        if self.mask_polygons:
            mask[self._ignore_mask(frame.shape) > 0] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._k_open)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._k_close)

        # slow adaptation: fast where nothing moves, very slow under foreground (so ghosts fade and
        # people standing for a minute are not absorbed)
        cv2.accumulateWeighted(frame, self.bg, self.rate_bg, mask=cv2.bitwise_not(mask))
        cv2.accumulateWeighted(frame, self.bg, self.rate_fg, mask=mask)

        n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        boxes, confs = [], []
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if area < self.min_area or area > self.max_area:
                continue
            if self.split_blobs and area > self.split_ratio * self.typical:
                parts = self._split(labels == i, area)
                if parts:
                    for bx, ba in parts:
                        boxes.append(bx)
                        confs.append(float(np.clip(ba / (0.8 * self.typical), 0.3, 0.98)))
                    continue
            boxes.append((x, y, x + w, y + h))
            confs.append(float(np.clip(area / (0.8 * self.typical), 0.3, 0.98)))
        if not boxes:
            return empty_detections()
        return sv.Detections(xyxy=np.asarray(boxes, dtype=np.float64), confidence=np.asarray(confs),
                             class_id=np.zeros(len(boxes), dtype=int))

    def _split(self, blob: np.ndarray, area: float):
        """Split a merged blob (people walking/standing together) with k-means on pixel coordinates,
        k = round(area / typical person area). Stabler frame-to-frame than distance-transform seeds."""
        k = int(round(area / self.typical))
        if k < 2:
            return None
        ys, xs = np.nonzero(blob)
        pts = np.stack([xs, ys], axis=1).astype(np.float32)
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 15, 0.5)
        cv2.setRNGSeed(3)
        _, labels, _ = cv2.kmeans(pts, k, None, crit, 2, cv2.KMEANS_PP_CENTERS)
        labels = labels.ravel()
        out = []
        for j in range(k):
            sel = labels == j
            if sel.sum() < self.min_area * 0.6:
                continue
            out.append(((float(xs[sel].min()), float(ys[sel].min()), float(xs[sel].max() + 1),
                         float(ys[sel].max() + 1)), float(sel.sum())))
        return out if len(out) >= 2 else None
