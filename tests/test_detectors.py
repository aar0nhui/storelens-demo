import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from glance.detect.bgsub import BackgroundSubtractionDetector
from glance.detect.yolox_onnx import _decode


def scene(people):
    img = np.full((240, 320, 3), 200, np.uint8)
    for x, y in people:
        cv2.ellipse(img, (x, y), (14, 8), 0, 0, 360, (60, 60, 160), -1)
    return img


def test_bgsub_detects_moving_people_and_ignores_static_background():
    det = BackgroundSubtractionDetector(min_area=60, typical_area=330)
    det.bg = scene([]).astype(np.float32)
    d = det.detect(scene([(100, 100), (220, 150)]))
    assert len(d) == 2
    centres = sorted(((d.xyxy[:, 0] + d.xyxy[:, 2]) / 2).round().tolist())
    assert abs(centres[0] - 100) < 4 and abs(centres[1] - 220) < 4
    assert len(det.detect(scene([]))) == 0


def test_bgsub_splits_two_people_walking_together():
    det = BackgroundSubtractionDetector(min_area=60, typical_area=330)
    det.bg = scene([]).astype(np.float32)
    d = det.detect(scene([(100, 100), (122, 100)]))  # touching shoulders -> one merged blob
    assert len(d) == 2


def test_bgsub_respects_mask_polygons():
    det = BackgroundSubtractionDetector(min_area=60, typical_area=330, mask_polygons=[[(0, 0), (160, 0), (160, 240), (0, 240)]])
    det.bg = scene([]).astype(np.float32)
    assert len(det.detect(scene([(100, 100)]))) == 0 and len(det.detect(scene([(240, 100)]))) == 1


def test_yolox_decode_shapes_and_scaling():
    n = (416 // 8) ** 2 + (416 // 16) ** 2 + (416 // 32) ** 2
    raw = np.zeros((1, n, 85), np.float32)
    out = _decode(raw, (416, 416))
    assert out.shape == (1, n, 85)
    assert np.allclose(out[0, 0, 2:4], 8.0)      # exp(0) * stride 8 at the first (finest) cell
    assert np.allclose(out[0, 1, :2], [8.0, 0])  # next cell along x: (0 + 1) * 8


WEIGHTS = os.environ.get("GLANCE_YOLOX_ONNX", "")


@pytest.mark.skipif(not (WEIGHTS and Path(WEIGHTS).exists()), reason="set GLANCE_YOLOX_ONNX to a yolox_nano.onnx path")
def test_yolox_onnx_finds_a_person():
    from skimage import data
    from glance.detect.yolox_onnx import YoloxOnnxDetector
    img = cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)
    d = YoloxOnnxDetector(WEIGHTS, conf=0.3).detect(img)
    assert len(d) >= 1 and d.confidence.max() > 0.5
