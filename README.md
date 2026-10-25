# StoreLens

Privacy-first marketing analytics that runs on a store's **existing CCTV**. It counts anonymous people over time,
shows which displays they come near and stop at, for how long, and tells the owner which layout changes worked.
It never remembers a person: no faces, no appearance embeddings, no stored trajectories, and every preview is blurred
before it is written.

This repository is the **MVP**: a complete, runnable pipeline plus a dashboard, validated on a synthetic store
simulator that provides ground truth. It has **not** been run on real store footage yet. See
[Known limitations](#known-limitations) and the [roadmap](#roadmap-to-a-minimum-reliable-product).

```
video ─▶ person detector ─▶ ByteTrack ─▶ floor-plan homography ─▶ zone / dwell / stop analysis ─▶ SQLite
(CCTV)   (swappable)        (motion only,  (pixels → metres,       (impression, engaged,          (events +
                             no re-ID)      calibration RMSE)       entry, heat)                   aggregates)
                                                                                                     │
                       blurred preview ◀──── privacy layer          FastAPI + offline dashboard ◀────┘
```

## Quickstart

```bash
pip install -e ".[dev]"          # Python >= 3.10
storelens demo --out demo --minutes 20 --preview
#   1. simulates two 20-minute weeks of a store ("baseline" and "after_change": new signage on Endcap B)
#   2. runs the full pipeline on both videos
#   3. scores it against the simulator's ground truth  -> demo/validation.json
#   4. renders a simulated live view (store camera + live floor plan, in sync)  -> demo/live/
#   5. writes a standalone dashboard                     -> demo/report.html   (open in any browser, works offline)
#      (add --minutes 90 for a comparison with enough visitors to be conclusive; --no-live skips step 4)

storelens serve --config demo/store.yaml --db demo/demo.db     # live dashboard at http://127.0.0.1:8000
pytest                                                          # ~1 min; `pytest -m "not slow"` skips the video test
```

Individual steps:

| command | what it does |
|---|---|
| `storelens sim --out DIR --minutes N` | render synthetic overhead CCTV video + `store.yaml` + ground truth |
| `storelens calibrate --config store.yaml --video v.mp4` | draw the zones over a real frame to eyeball the calibration before trusting any number |
| `storelens run --config store.yaml --video v.mp4 --start 2026-11-02T10:00 --db store.db --label week1 [--preview p.mp4]` | video → events (`--detector bgsub｜yolox_onnx｜rfdetr`, `--engagement-mode stop｜stop_facing`) |
| `storelens validate --config … --db … --run 1 --gt clip_gt.json.gz` | compare a run to simulator ground truth |
| `storelens live --out live --seconds 60` | simulate the live view: a store-camera feed and a live floor plan, in sync |
| `storelens serve` / `storelens report` | live dashboard / single-file HTML report (both pick up `live/` next to the database) |
| `storelens site --out site` | static website folder for Cloudflare / GitHub Pages / any CDN (`--demo-label`, `--cta-url`, `--noindex`) |

## What it measures

| metric | definition |
|---|---|
| **footfall** | door entries: crossing the entrance line outside→inside with a 0.25 m hysteresis band (jitter on the line is not an entry) |
| **impression** | a visitor's presence in a fixture's *exposure zone* for ≥ 0.5 s (an opportunity to see it) |
| **engaged** | impression where the visitor *stopped* (< 0.35 m/s) for ≥ 2 s in the zone (per-zone override: `min_engage_s`). In `stop_facing` mode they must also face the display (60° cone) |
| **reach** | impressions ÷ visitors |
| **engagement rate** | engaged ÷ impressions: how compelling a display is once it has been seen |
| **engaged per 100** | engaged ÷ visitors × 100: traffic-normalised, the number used for before/after comparisons |
| **lift** | change in engaged-per-100 between two periods. Bootstrap CIs over *visitors*, widened for the number of displays compared (family-wise 95 %), and an adjusted lift that divides by the **median** change of the other displays, so a store-wide shift cancels but the display you moved cannot make its neighbours look worse |

Engagement is decided **at query time** from stored facts (`dwell_s`, `stopped_s`, `facing_stopped_s`), so changing
the definition re-scores all history without reprocessing video.

Insight cards are plain rules, each naming the numbers behind it and a confidence from the sample size:
*seen-but-not-engaging*, *engaging-but-hard-to-reach*, *low reach*, *long dwell at checkout*, *long consideration*,
*strong path between fixtures*, and *measured lift* from Compare.

## Privacy by design

* **No identity.** Tracking is motion/IoU-only (ByteTrack + a short motion-based stitcher). There is no appearance
  model, so there is nothing to re-identify someone with, on a later visit or on another camera.
* **Short-lived keys.** A visit is keyed by `HMAC(salt, camera, track id, start time)`. The salt lives only in memory and
  rotates every 24 h of data time, so keys cannot be recomputed or linked across days, even by us.
* **No trajectories, frames, or biometrics persisted.** The database has no x/y, image, embedding, or face column
  (asserted in `tests/test_privacy.py` and `tests/test_pipeline_sim.py`). Heat is aggregated floor-grid seconds.
* **Blurred previews.** Faces are small and unreliable on CCTV, so the default is to mosaic the whole person box
  (`privacy.blur: person`) *before* anything is drawn or encoded. `blur: none` is refused unless explicitly allowed.
* **No demographic inference.** No age, gender, or emotion models, by design.
* **Staff are excluded** by uniform colour and by time spent in the back-of-house zone, and kept out of every metric.

See [`docs/privacy-and-legal.md`](docs/privacy-and-legal.md) for the consent and legal checklist (not legal advice).

## Detectors and licences

All detectors implement one interface returning `supervision.Detections`, so swapping is a config change:

| backend | use it for | licence |
|---|---|---|
| `bgsub` (default for the simulator) | fixed **overhead** cameras where people are blobs on a static floor; no weights; ~70 fps on CPU. Splits merged blobs with k-means | this repo |
| `yolox_onnx` | oblique / wall-mounted cameras. `scripts/fetch_yolox.sh` fetches YOLOX-nano (~3.6 MB) | YOLOX Apache-2.0, onnxruntime MIT |
| `rfdetr` | **experimental and untested here**; needs PyTorch. Verify model-size licence and `person_class_id` first | Apache-2.0 (base sizes) |

**Ultralytics YOLO is deliberately not a dependency**: its AGPL-3.0 licence would apply to a hosted product built on it
unless commercially licensed. COCO detectors are weakest on overhead views; fine-tune on a few hundred frames of the
store's own cameras before trusting them there.

## Setting up a real store

1. Copy `demo/store.yaml`. Set `floor` (metres), the `zones` (polygons in floor metres), and the `entrance` segment.
2. Click ≥ 4 matching points on a camera frame and on the floor plan (`pixel_points` ↔ `floor_points`); more is better.
   The run's `calibration_rmse_cm` is shown on the dashboard's Quality card and warns above 10 cm.
3. `storelens calibrate --config store.yaml --video clip.mp4` and *look* at the overlay. A silent calibration error
   corrupts every zone number without any visible failure, so this is the step not to skip.
4. Put the staff uniform colour in `staff.hsv_ranges`; add `mask_polygons` for windows, mirrors and screens.

## Validation harness

`storelens validate` replays the same analytics on the simulator's ground-truth positions and scores the pipeline:
footfall precision/recall, impression and engaged-visitor error per fixture, heat-map correlation, staff leakage, and
ghost tracks. It scores four engagement variants against the shoppers' scripted browse intent: stop-only,
stop + motion-facing, the pipeline as run, and a perfect-perception ceiling (what a flawless detector with this
*definition* could reach).

### Results on the bundled simulator

Two runs of `storelens demo`: 20 minutes per week (about 115 visitors) and `--minutes 90` (about 520 visitors).
Each cell is week 1 / week 2 (baseline / after the Endcap B change).

| | 20 min per week | 90 min per week |
|---|---|---|
| Footfall, pipeline vs truth | 105 vs 107 (-2 %) / 100 vs 105 (-5 %) | 453 vs 476 (-5 %) / 463 vs 474 (-2 %) |
| Fixture impressions | 340 vs 338 / 349 vs 348 | 1491 vs 1504 (-1 %) / 1608 vs 1618 (-1 %) |
| Engaged-visitor F1 vs scripted intent (ceiling for this definition) | 0.91 (0.92) / 0.95 (0.97) | 0.91 (0.93) / 0.92 (0.94) |
| ...if motion-heading "facing" is required | 0.64 / 0.73 | 0.70 / 0.68 |
| Staff leaking into customer impressions | 0 / 0 | 7 / 3 (of ~1,500) |
| Engaged-time error on matched visits | 0.35 s / 0.37 s | 0.20 s / 0.32 s |
| Heat-map correlation with truth | r = 0.99 / 0.99 | r = 0.99 / 0.99 |

Read this as "the plumbing and the definitions work", not as accuracy on real stores: the simulator has no
reflections, shelves hiding people, or odd lighting.

**Before/after on Endcap B.** The simulator raises Endcap B's browse probability from 0.22 to 0.40 (about +80 %).

* 20-minute weeks (~115 visitors): the pipeline reports +87 % adjusted, but the interval is -29 % to +448 %, so the
  dashboard says **inconclusive** and "needs roughly 490 visitors per period to confirm".
* 90-minute weeks (~520 visitors): +98 % adjusted, interval +32 % to +199 %: **improved**. The four untouched displays
  stay inconclusive.

That is the intended behaviour: a few hundred visitors per period are needed before a layout change can be called a win.
Two things in the statistics came from looking at these runs. (1) An earlier version pooled all other displays as the
control; once Endcap B doubled, an untouched Front Table read as "worse". Controls are now the median change of the other
displays (regression test in `tests/test_queries.py`). (2) Looking at five displays gives five chances to see a "win"
by luck (about 23 % that at least one appears), so intervals are widened for the number of displays compared
(`test_no_change_anywhere_rarely_flags_a_winner_or_loser`).
The point estimate also understates the scripted behaviour: in the 90-minute sample the true engaged visitors at Endcap B
went 51 to 118 (+131 %) while the pipeline saw 64 to 131 (+105 %). Stop-only counts people who stop but look away, a
roughly constant extra that shrinks relative lifts toward zero.

## Dashboard layout

`storelens serve`, the standalone `report.html` and the static site all show the same dashboard (offline, no external
requests). A **collapsible sidebar** jumps between sections; clicking the StoreLens logo or name returns to Overview. It
collapses to an icon rail (names appear as tooltips), remembers the choice in the browser, and on a phone becomes a
drawer behind a menu button.

* **Overview**: the four headline numbers (visitors, display impressions, visitors who engaged, median engaged time)
  and the simulated live view (store camera + live floor plan, in sync).
* **Floor plan**: the large floor plan (traffic heat or engaged per 100 visitors), a detail panel for the selected
  display (reach, engagement rate, median engaged time, where visitors come from and go next) and the full displays table.
* **Activity**: impressions, engaged visits and door entries over time (filter by display) and the most common
  display-to-display moves.
* **Insights**: the rule-based layout insight cards and the before/after "Did the change work?" comparison with
  confidence intervals.
* **Data & privacy**: privacy guarantees, data-quality warnings, per-run detector stats and metric definitions.

Sections are addressable (`#/overview`, `#/floor`, `#/activity`, `#/insights`, `#/data`), render lazily, and the live
videos pause when you leave Overview. `tests/test_server.py` includes a headless-browser smoke test (skipped when Playwright
or Chromium is not installed).

## Static demo (free hosting)

`make site` (= `storelens site --config demo_data/store.yaml --db demo_data/demo.db --out site`) writes the dashboard as a
plain folder: 10 files, 3 MB, relative URLs, a strict Content-Security-Policy, no server and no third-party requests.
Commit `site/` and let Cloudflare (Workers static assets or Pages) or any static host publish it on every push.
Step-by-step, limits and costs: [`docs/deploy-static-demo.md`](docs/deploy-static-demo.md). What to add once real stores
and users arrive (auth, storage, ingest) and what it costs: [`docs/backend-options.md`](docs/backend-options.md).

## Live view (simulated)

`storelens live` (also part of `storelens demo`) renders a one-minute **simulated live feed** for the dashboard: a
general store-camera view next to a live floor plan, playing in sync, with the same anonymous track ids on both.

* **Store camera**: a perspective view from a high front corner of the same simulated store (shelves, shoppers, red-vest
  staff). Heads are mosaicked with the product's own head-region rule before the frame is finished; tags are anonymous
  track ids; each display's floor zone turns green while someone is engaged with it.
* **Live floor plan**: tracks with short trails, a fading recent-traffic glow, and counters (in store, visitors,
  impressions, engaged) that update as people move.
* **How it is made**: the simulator renders two cameras from one ground truth. The real pipeline runs on the overhead
  camera; its per-frame tracks (kept in memory only) drive both panels. The videos are the only thing written, and the
  database still stores only events.

Honest limits: the perspective view is a stylised render (low-poly people), not photoreal, and the pipeline does not
analyse it. Analysing an oblique feed needs the YOLOX backend fine-tuned on real footage. The live counters are a
streaming approximation of the batch analysis (the database numbers always come from the batch analysis). The floor plan
draws one staff marker per 1 m cluster, hiding the ghost tracks that background subtraction leaves around people who
stand still; the batch metrics already exclude staff.

## Known limitations

* **Synthetic only.** Real-world detector accuracy, lighting, occlusion, and camera angles are untested. The first
  real-footage run is the most important next step.
* **"Facing" is not measured.** Without a head/pose model, orientation can only come from walking direction. In the
  simulator that **lowered** recall (people standing still after a walk lose a reliable heading), so the default is
  `engagement_mode: stop`. Remaining stop-only error is definitional: people who stop but look elsewhere. The
  `FacingEstimator` interface is the plug-in point for a real head-orientation model.
* Walking-together groups are under-counted when the detector merges them (bgsub splits most, not all).
* Lift CIs are wide on small samples; the dashboard says "inconclusive" instead of guessing.
* `bgsub` assumes a fixed camera and ghosts around people who stand still for minutes (handled for staff by the
  staff-zone rule, not in general).
* Impressions are *zone presence*, not gaze. They are a layout metric, not an ad-viewability claim.

## Roadmap to a minimum reliable product

1. **Real footage**: 2–3 pilot stores, label ~200 frames each, fine-tune the detector, re-run `validate`-style checks
   against hand counts on a few hours of video.
2. **Edge deployment**: run on a Jetson-class box on the store LAN reading RTSP; only events leave the building.
3. **Orientation**: plug a head/pose estimator into `FacingEstimator` and re-test `stop_facing`.
4. **Multi-camera**: de-duplicate in floor coordinates; stitch zones across cameras.
5. **POS link**: import transactions as CSV to turn "engaged" into conversion and basket lift.
6. **Layout what-ifs**: simulate a proposed change on the digital twin before the owner moves a shelf.
7. **Compliance pack**: signage templates, DPIA template, retention settings, audit log.

## Repository layout

```
src/storelens/
  config.py geometry.py        store.yaml models, homography + calibration QA
  detect/                      bgsub, yolox_onnx, rfdetr (experimental), Detector interface
  track.py                     ByteTrack wrapper + motion-only stitching with floor-edge guard
  analysis.py                  resampling, kinematics, zone visits, entries, FacingEstimator
  staff.py privacy.py          uniform vote; salted keys, mosaic, preview writer
  store.py queries.py          SQLite events; metrics, lift (bootstrap), quality
  insights.py                  rule-based insight cards
  pipeline.py cli.py           video → events; command line
  server.py report.py web/     FastAPI + offline dashboard (sidebar navigation); standalone report
  site.py                      static-site export (relative URLs, CSP, snapshot.js, media clips)
  sim/ validate.py             synthetic store, overhead renderer, perspective "webcam" renderer, ground truth; scoring
  live.py                      simulated live view: streaming counters, floor-plan panel, clip generator
tests/                         unit, query, API, site, privacy and end-to-end tests
demo_data/ site/               simulated dataset the public demo is built from; the built static site (committed)
wrangler.jsonc Makefile        Cloudflare static-assets config; shortcuts (make site / test / serve)
.github/workflows/ci.yml       tests + a trial site build on every push
```
