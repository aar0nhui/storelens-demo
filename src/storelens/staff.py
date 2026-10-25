"""Staff vs customer: uniform colour vote per frame, plus a back-of-house dwell rule at commit time.

Staff pollute every retail metric (they stand at displays while restocking), so excluding them is
non-negotiable. Colour is the cheapest cue that needs no identity: it looks at *clothing hue inside
the box*, never at faces, and the result is a single boolean per frame that is not stored.
"""
from __future__ import annotations

import cv2
import numpy as np

from .config import StaffConfig


class StaffClassifier:
    def __init__(self, cfg: StaffConfig):
        self.cfg = cfg
        self.enabled = bool(cfg.hsv_ranges)
        self._ranges = [(np.array(lo, np.uint8), np.array(hi, np.uint8)) for lo, hi in cfg.hsv_ranges]

    def vote(self, frame: np.ndarray, xyxy) -> bool:
        if not self.enabled:
            return False
        x1, y1, x2, y2 = [int(round(v)) for v in xyxy]
        w, h = x2 - x1, y2 - y1
        mx, my = int(w * 0.2), int(h * 0.2)
        crop = frame[max(y1 + my, 0):max(y2 - my, 0), max(x1 + mx, 0):max(x2 - mx, 0)]
        if crop.size == 0:
            return False
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        m = np.zeros(hsv.shape[:2], np.uint8)
        for lo, hi in self._ranges:
            m |= cv2.inRange(hsv, lo, hi)
        return float((m > 0).mean()) >= self.cfg.min_fraction
