# Examples

Everything here is synthetic. Nothing in these folders says anything about accuracy on a real store.

* `multicam/store.yaml`: a two-camera store configuration (an entrance camera and a floor camera) as produced by the
  simulator, with `detector_params` set per camera. `multicam/coverage.png` is the picture `glance cameras --config
  examples/multicam/store.yaml --png coverage.png` writes: tint is the camera that owns that part of the floor. See
  [docs/cameras.md](../docs/cameras.md).
* `fieldcheck/`: the label files a person would produce by watching footage (here written by a simulated, imperfect
  annotator), the spec, and the harness's verdict (`result.txt`, `result.json`). Rebuild it with
  `make fieldcheck-example FROM=<a glance demo folder>`. See [docs/validation.md](../docs/validation.md).
