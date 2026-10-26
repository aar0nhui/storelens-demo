# StoreLens

Privacy-first marketing analytics that runs on a store's **existing CCTV**. It counts anonymous people over time,
shows which displays they come near and stop at, for how long, tells the owner which layout changes worked, and suggests
what to change next with a clearly-labelled estimate of what it could be worth. It never remembers a person: no stored
faces, no appearance embeddings, no stored trajectories, and every preview is blurred before it is written. An optional,
off-by-default **audience mode** estimates age band and perceived gender in memory and keeps only aggregate counts.

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
pytest                                                          # ~6 min; `pytest -m "not slow"` skips the video tests (~1 min)
(cd cloud && npm ci && npm test)                                # private-app Worker tests (Node 20+)
```

Individual steps:

| command | what it does |
|---|---|
| `storelens sim --out DIR --minutes N` | render synthetic overhead CCTV video + `store.yaml` + ground truth |
| `storelens calibrate --config store.yaml --video v.mp4` | draw the zones over a real frame to eyeball the calibration before trusting any number |
| `storelens run --config store.yaml --video v.mp4 --start 2026-11-02T10:00 --db store.db --label week1 [--preview p.mp4]` | video → events (`--detector bgsub｜yolox_onnx｜rfdetr`, `--engagement-mode stop｜stop_facing`) |
| `storelens validate --config … --db … --run 1 --gt clip_gt.json.gz` | compare a run to simulator ground truth |
| `storelens cameras --config store.yaml [--png map.png]` | per-camera calibration (leave-one-out error), floor coverage, which camera owns which display |
| `storelens fieldcheck --init labels/` then `--spec labels/fieldcheck.yaml` | score the pipeline against hand tallies from the customer's own footage, with intervals and pre-registered gates |
| `storelens audit --config … --db …` | privacy audit: schema, files on disk, timestamp resolution, linkage attack on the stored counts (`--coarsen-days`) |
| `storelens snapshot` / `storelens push` | write the dashboard's answers as JSON / send them to the private cloud app |
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
* **Audience mode is off by default and aggregate-only.** When switched on (`demographics.enabled`), a face-region model
  runs on the store computer, in memory, once per tracked person; the estimate (age band, perceived gender, or
  `unknown`) is added to a counter and the person's state is dropped. The `audience` table has no visit key, buckets are
  6 hours, and the dashboard merges or hides any group under 10 people. Faces are analysed momentarily and never stored.
  No production model is bundled (the demo's estimator is simulated), and there is no emotion inference.
* **Staff are excluded** by uniform colour and by time spent in the back-of-house zone, and kept out of every metric.

See [`docs/privacy-and-legal.md`](docs/privacy-and-legal.md) for the consent and legal checklist (not legal advice) and
[`docs/validation.md`](docs/validation.md) for `storelens audit`, which tests these claims on a real database.

## Audience and recommendations

* **Audience** (opt-in): who visits, and who engages with each display, by age band and perceived gender, with shares,
  95% intervals, an over/under-indexing affinity score and a coverage figure. Small groups are merged or hidden.
* **Recommendations**: displays whose reach or stopping rate is clearly below the store's own median (the whole 95%
  interval below it) get a layout suggestion, a candidate floor spot when reach is the problem, and a scenario:
  extra engaged shoppers per day and a revenue range from two stated assumptions (purchase rate, basket size). Marketing
  effects on footfall appear only as sensitivity tables (+5/10/20%), never as predictions.

These are scenarios against the store's own median display, not forecasts. Details and limits:
[`docs/audience.md`](docs/audience.md).

## More than one camera

Every floor spot and every display has exactly one owning camera, so overlapping cameras never double count, and nothing
links a person across cameras. `storelens cameras` reports calibration quality and coverage; see
[`docs/cameras.md`](docs/cameras.md).

## Private cloud app (one customer)

`cloud/` is a Cloudflare Worker serving the dashboard behind Cloudflare Access, plus a service-token-protected `/ingest`
that receives the aggregate snapshot from the store computer. Setup: [`docs/cloud.md`](docs/cloud.md).

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

Two runs of `storelens demo --seed 18`: 20 minutes per week (about 100-120 visitors) and `--minutes 90` (about 500-580
visitors). Each cell is week 1 / week 2 (baseline / after the Endcap B change).

| | 20 min per week | 90 min per week |
|---|---|---|
| Footfall, pipeline vs truth | 105 vs 110 (-5 %) / 92 vs 95 (-3 %) | 451 vs 459 (-2 %) / 508 vs 517 (-2 %) |
| Fixture impressions | 358 vs 374 (-4 %) / 313 vs 314 (0 %) | 1589 vs 1596 (0 %) / 1714 vs 1724 (-1 %) |
| Engaged-visitor F1 vs scripted intent (ceiling for this definition) | 0.90 (0.93) / 0.94 (0.96) | 0.90 (0.92) / 0.90 (0.92) |
| ...if motion-heading "facing" is required | 0.55 / 0.68 | 0.65 / 0.65 |
| Staff leaking into customer impressions | 0 / 0 | 2 / 9 (of ~1,600-1,700) |
| Engaged-time error on matched visits | 0.39 s / 0.33 s | 0.23 s / 0.25 s |
| Heat-map correlation with truth | r = 1.00 / 1.00 | r = 1.00 / 1.00 |
| Simulated attribute model: age band exactly right / gender right | 0.87 / 0.91 | 0.87 / 0.95 |

Read this as "the plumbing and the definitions work", not as accuracy on real stores: the simulator has no
reflections, shelves hiding people, or odd lighting, and the attribute model is itself simulated.

**Before/after on Endcap B.** The simulator raises Endcap B's browse probability from 0.22 to 0.40 (about +80 % in
expectation; the realised change in any one run varies with luck).

* 20-minute weeks (~115 visitors): in this draw the truth barely moved (22 scripted engaged visitors in both weeks),
  and the pipeline reports +9 % adjusted with an interval of -51 % to +151 %: **inconclusive**, which is the right answer.
* 90-minute weeks (~540 visitors): truth 50 to 105 scripted engaged visitors (+87 % per visitor; week 2 also had more
  visitors); the pipeline reports +53 % raw and +61 % adjusted, interval +9 % to +152 %: **improved**. The four
  untouched displays stay inconclusive.

That is the intended behaviour: a few hundred visitors per period are needed before a layout change can be called a win.
Things that came out of looking at these runs: (1) An earlier version pooled all other displays as the control; once
Endcap B doubled, an untouched Front Table read as "worse". Controls are now the median change of the other displays
(regression test in `tests/test_queries.py`). (2) Looking at five displays gives five chances to see a "win" by luck
(about 23 % that at least one appears), so intervals are widened for the number of displays compared
(`test_no_change_anywhere_rarely_flags_a_winner_or_loser`). (3) The point estimate understates the scripted behaviour
(true +87 % per visitor against +53 % raw, +61 % adjusted, in the pipeline): stop-only counts people who stop but look away, a
roughly constant extra that shrinks relative lifts toward zero.

**Which seeds, and why.** The first 90-minute pair (seeds 11/12) happened to realise only +26 % at Endcap B, near the
10th percentile of 12 seed pairs (median +83 % per customer), so the demo would have said "inconclusive" for a change that was
real. The published demo uses seeds 18/19, whose realised effect (+89 % per customer against an expected +88 %) is typical
rather than the best of the 12. That is a choice of demo storyline, not a result; the 20-minute
run above shows what the same system says when the draw is small.

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
* **Audience**: age band and perceived gender of visitors and of each display's engaged shoppers (only when audience
  mode is on; the demo's numbers are simulated).
* **Recommendations**: what to change, why, and the what-if estimate with its assumptions beside it.
* **Insights**: the rule-based layout insight cards and the before/after "Did the change work?" comparison with
  confidence intervals.
* **Data & privacy**: privacy guarantees, data-quality warnings, per-run detector stats and metric definitions.

Sections are addressable (`#/overview`, `#/floor`, `#/activity`, `#/audience`, `#/recommend`, `#/insights`, `#/data`), render lazily, and the live
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
  real-footage run is the most important next step. `storelens fieldcheck` is the tool for it, but it has only been run
  against the simulator.
* **Audience numbers in the demo are simulated** (a noisy copy of the simulator's truth). Overhead ceiling cameras
  usually cannot see faces; real audience mode needs an eye-level or entrance camera and a measured model.
* **Revenue and traffic estimates rest on assumptions** (purchase rate, basket size, visitors per day) and on the store's
  own median as the target. They are scenarios to test with Compare, not forecasts.
* **No on-site setup wizard yet.** Cameras are configured in `store.yaml` and checked with `storelens cameras`.
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
4. **Multi-camera setup wizard**: ownership-based multi-camera analysis is built (and tested on a two-camera simulator);
   the on-site wizard (discover cameras, click calibration points on a live frame, draw masks) is not.
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
  cameras.py                   multi-camera calibration checks, coverage, display ownership
  attributes.py audience.py    audience estimator interface + simulated estimator; small-cell rules and intervals
  recommend.py                 layout/marketing recommendations with what-if estimates
  fieldcheck.py audit.py       hand-tally validation with gates; privacy audit and coarsening
  cloudsync.py                 snapshot envelope + push to the private cloud app
  sim/ validate.py             synthetic store, overhead renderer, perspective "webcam" renderer, ground truth; scoring
  live.py                      simulated live view: streaming counters, floor-plan panel, clip generator
cloud/                         private Cloudflare Worker (Access-checked dashboard + /ingest); `npm test` inside it
docs/                          privacy and legal checklist, cameras, validation, audience, cloud, deploy, backend options
tests/                         unit, query, API, site, privacy, cloud and end-to-end tests
demo_data/ site/               simulated dataset the public demo is built from; the built static site (committed)
wrangler.jsonc Makefile        Cloudflare static-assets config; shortcuts (make site / test / serve)
.github/workflows/ci.yml       tests + a trial site build on every push
```
