#!/usr/bin/env bash
# Download the Apache-2.0 YOLOX-nano ONNX weights (~3.6 MB) used by the `yolox_onnx` detector.
set -euo pipefail
mkdir -p weights
curl -fL -o weights/yolox_nano.onnx \
  https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx
echo "saved weights/yolox_nano.onnx"
echo "run with:  glance run --detector yolox_onnx --model weights/yolox_nano.onnx ..."
