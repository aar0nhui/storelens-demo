"""Detector interface. Every backend turns a BGR frame into ``sv.Detections`` (xyxy + confidence)."""
from __future__ import annotations

import numpy as np
import supervision as sv

from ..config import DetectorConfig


class Detector:
    """Swappable person detector.

    License note (important for a commercial product): the default backends here are Apache-2.0 /
    MIT. Ultralytics YOLO is AGPL-3.0 and needs an Enterprise license for closed-source or SaaS use,
    so it is deliberately *not* a dependency of this package.
    """

    name = "base"

    def prepare(self, video_path: str) -> None:  # optional one-off setup (e.g., background model)
        return None

    def detect(self, frame: np.ndarray) -> sv.Detections:  # pragma: no cover - interface
        raise NotImplementedError


def empty_detections() -> sv.Detections:
    return sv.Detections(xyxy=np.zeros((0, 4), dtype=np.float64), confidence=np.zeros(0),
                         class_id=np.zeros(0, dtype=int))


def make_detector(cfg: DetectorConfig, mask_polygons=None) -> Detector:
    params = dict(cfg.params)
    if cfg.kind == "bgsub":
        from .bgsub import BackgroundSubtractionDetector
        return BackgroundSubtractionDetector(mask_polygons=mask_polygons, **params)
    if cfg.kind == "yolox_onnx":
        from .yolox_onnx import YoloxOnnxDetector
        return YoloxOnnxDetector(**params)
    if cfg.kind == "rfdetr":
        from .rfdetr import RFDETRDetector
        return RFDETRDetector(**params)
    raise ValueError(f"unknown detector kind {cfg.kind}")
