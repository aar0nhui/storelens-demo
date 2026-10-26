"""RF-DETR person detector (Roboflow; Apache-2.0 for the base sizes).

EXPERIMENTAL: this adapter has NOT been run in the build environment (it needs PyTorch + weight
download). Before relying on it, verify (1) the model-size license you pick, and (2) ``person_class_id``
(COCO ids are 1-based in RF-DETR's label map, 0-based in YOLOX). The rest of the pipeline only needs
``sv.Detections``, so wiring is trivial once those two things are confirmed on your machine.
"""
from __future__ import annotations

import cv2
import numpy as np
import supervision as sv

from .base import Detector, empty_detections


class RFDETRDetector(Detector):
    name = "rfdetr"

    def __init__(self, size: str = "base", threshold: float = 0.4, person_class_id: int = 1):
        try:
            import rfdetr
        except ImportError as e:  # pragma: no cover
            raise ImportError("pip install 'glance[rfdetr]' to use the RF-DETR backend") from e
        cls = {"nano": "RFDETRNano", "small": "RFDETRSmall", "base": "RFDETRBase"}[size]
        self.model = getattr(rfdetr, cls)()
        self.threshold, self.person = threshold, person_class_id

    def detect(self, frame: np.ndarray) -> sv.Detections:  # pragma: no cover - needs torch
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        det = self.model.predict(rgb, threshold=self.threshold)
        if det is None or len(det) == 0:
            return empty_detections()
        det = det[det.class_id == self.person]
        if len(det) == 0:
            return empty_detections()
        return sv.Detections(xyxy=det.xyxy.astype(np.float64), confidence=det.confidence.astype(np.float64),
                             class_id=np.zeros(len(det), dtype=int))
