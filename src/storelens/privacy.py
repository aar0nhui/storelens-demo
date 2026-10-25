"""Privacy by design: ephemeral IDs and irreversible blur on anything that leaves the analytics path."""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

from .config import PrivacyConfig


class Anonymizer:
    """Visit keys are HMACs under a salt that lives only in memory and rotates every N hours of *data
    time*. The salt is never written anywhere, so keys cannot be recomputed later, not even by us:
    there is nothing to join a visit to a person, a prior visit or another day."""

    def __init__(self, rotation_hours: int = 24):
        self.rotation = max(int(rotation_hours), 1) * 3600
        self._salts: dict[int, bytes] = {}

    def key(self, camera_id: str, uid: int, t_epoch: float) -> str:
        epoch = int(t_epoch // self.rotation)
        salt = self._salts.get(epoch)
        if salt is None:
            salt = os.urandom(16)
            self._salts = {k: v for k, v in self._salts.items() if k >= epoch - 1}  # purge old salts
            self._salts[epoch] = salt
        return hmac.new(salt, f"{camera_id}:{uid}:{int(t_epoch)}".encode(), hashlib.sha256).hexdigest()[:12]


def privacy_regions(xyxy: np.ndarray, anchor: str, cfg: PrivacyConfig, shape) -> np.ndarray:
    """Pixel regions to obscure. Fail-safe: regions are generous (``margin``) and head mode on an
    oblique camera blurs the top ``head_fraction`` of the box, not just what a face detector finds."""
    if len(xyxy) == 0 or cfg.blur == "none":
        return np.zeros((0, 4), int)
    h_img, w_img = shape[:2]
    regs = []
    for x1, y1, x2, y2 in xyxy:
        w, h = x2 - x1, y2 - y1
        if cfg.blur == "person":
            rx1, ry1, rx2, ry2 = x1, y1, x2, y2
        elif anchor == "center":  # overhead: the head is in the middle of the blob
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            rx1, rx2, ry1, ry2 = cx - 0.35 * w, cx + 0.35 * w, cy - 0.45 * h, cy + 0.45 * h
        else:  # oblique: head is the top slice of the person box
            rx1, rx2, ry1, ry2 = x1, x2, y1, y1 + cfg.head_fraction * h
        mx, my = cfg.margin * (rx2 - rx1), cfg.margin * (ry2 - ry1)
        regs.append([max(rx1 - mx, 0), max(ry1 - my, 0), min(rx2 + mx, w_img), min(ry2 + my, h_img)])
    return np.asarray(regs).round().astype(int)


def obscure(frame: np.ndarray, regions: np.ndarray) -> np.ndarray:
    """Mosaic (<= 4x4 blocks) then blur. A mosaic is not recoverable the way a light Gaussian can be."""
    for x1, y1, x2, y2 in regions:
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        roi = frame[y1:y2, x1:x2]
        small = cv2.resize(roi, (min(4, x2 - x1), min(4, y2 - y1)), interpolation=cv2.INTER_AREA)
        frame[y1:y2, x1:x2] = cv2.resize(small, (x2 - x1, y2 - y1), interpolation=cv2.INTER_NEAREST)
    return frame


class PreviewWriter:
    """Writes the anonymised preview video. It only ever receives frames AFTER obscure()."""

    def __init__(self, path: str | Path, size: tuple[int, int], fps: float, privacy: PrivacyConfig,
                 allow_raw: bool = False):
        if privacy.blur == "none" and not allow_raw:
            raise PermissionError("privacy.blur is 'none': refusing to write a preview without allow_raw=True")
        self.path = Path(path)
        self.tmp = self.path.with_suffix(".tmp.mp4")
        self.w = cv2.VideoWriter(str(self.tmp), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)

    def write(self, frame: np.ndarray) -> None:
        self.w.write(frame)

    def close(self) -> None:
        self.w.release()
        if shutil.which("ffmpeg"):
            try:
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(self.tmp), "-c:v", "libx264",
                                "-pix_fmt", "yuv420p", "-crf", "28", str(self.path)], check=True)
                self.tmp.unlink(missing_ok=True)
                return
            except Exception:
                pass
        self.tmp.replace(self.path)
