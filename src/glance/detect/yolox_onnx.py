"""YOLOX (Megvii, Apache-2.0) person detector running on ONNX Runtime.

Get weights (nano ~3.6 MB, tiny/s/m larger) from the official YOLOX releases, e.g.
https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx
and set ``detector: {kind: yolox_onnx, params: {model_path: weights/yolox_nano.onnx}}``.

This backend is for oblique / side-view cameras. COCO-trained detectors are weakest on *overhead*
views (people look like blobs from above), so fine-tune on a few hundred frames from the store's own
cameras before trusting it there.
"""
from __future__ import annotations

import cv2
import numpy as np
import supervision as sv

from .base import Detector, empty_detections


def _decode(outputs: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Turn raw YOLOX head outputs into (cx, cy, w, h, obj, cls...) in input-pixel units."""
    strides = (8, 16, 32)
    grids, exp_strides = [], []
    for s in strides:
        hs, ws = size[0] // s, size[1] // s
        xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))
        g = np.stack((xv, yv), 2).reshape(1, -1, 2)
        grids.append(g)
        exp_strides.append(np.full((1, g.shape[1], 1), s))
    grids = np.concatenate(grids, 1)
    exp_strides = np.concatenate(exp_strides, 1)
    out = outputs.copy()
    out[..., :2] = (out[..., :2] + grids) * exp_strides
    out[..., 2:4] = np.exp(out[..., 2:4]) * exp_strides
    return out


class YoloxOnnxDetector(Detector):
    name = "yolox_onnx"

    def __init__(self, model_path: str, input_size: int = 416, conf: float = 0.35, nms_iou: float = 0.45,
                 person_class: int = 0, providers: list[str] | None = None):
        try:
            import onnxruntime as ort
        except ImportError as e:  # pragma: no cover
            raise ImportError("pip install 'glance[onnx]' to use the YOLOX backend") from e
        self.sess = ort.InferenceSession(model_path, providers=providers or ["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        shape = self.sess.get_inputs()[0].shape
        self.size = (int(shape[2]), int(shape[3])) if isinstance(shape[2], int) else (input_size, input_size)
        self.conf, self.nms_iou, self.person = conf, nms_iou, person_class

    def _letterbox(self, img: np.ndarray):
        h, w = img.shape[:2]
        r = min(self.size[0] / h, self.size[1] / w)
        nh, nw = int(h * r), int(w * r)
        resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.size[0], self.size[1], 3), 114, np.uint8)
        canvas[:nh, :nw] = resized
        return canvas, r

    def detect(self, frame: np.ndarray) -> sv.Detections:
        img, r = self._letterbox(frame)
        blob = img.astype(np.float32).transpose(2, 0, 1)[None]  # BGR, 0-255, CHW (YOLOX convention)
        raw = self.sess.run(None, {self.inp: blob})[0]
        pred = _decode(raw, self.size)[0]
        scores = pred[:, 4] * pred[:, 5 + self.person]
        keep = scores > self.conf
        if not keep.any():
            return empty_detections()
        p, s = pred[keep], scores[keep]
        xyxy = np.stack([p[:, 0] - p[:, 2] / 2, p[:, 1] - p[:, 3] / 2,
                         p[:, 0] + p[:, 2] / 2, p[:, 1] + p[:, 3] / 2], axis=1) / r
        xywh = np.stack([xyxy[:, 0], xyxy[:, 1], xyxy[:, 2] - xyxy[:, 0], xyxy[:, 3] - xyxy[:, 1]], axis=1)
        idx = cv2.dnn.NMSBoxes(xywh.tolist(), s.tolist(), self.conf, self.nms_iou)
        if len(idx) == 0:
            return empty_detections()
        idx = np.asarray(idx).reshape(-1)
        h, w = frame.shape[:2]
        xyxy = np.clip(xyxy[idx], [0, 0, 0, 0], [w, h, w, h])
        return sv.Detections(xyxy=xyxy.astype(np.float64), confidence=s[idx].astype(np.float64),
                             class_id=np.zeros(len(idx), dtype=int))
