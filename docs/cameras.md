# Several cameras, one floor plan

Zones, displays and the entrance belong to the **floor plan** (metres, one origin). Every camera has its own homography
from its image to that plan. `store.yaml` lists the cameras; a single-camera store keeps working with the same file.

```yaml
cameras:
  - id: door
    role: entrance            # counts door crossings and defines "visitors"; floor | entrance | both
    anchor: center            # center for ceiling cameras, bottom_center for oblique ones (feet = floor contact)
    pixel_points: [[...], ...]   # at least 4, 5-8+ recommended, spread across the area the camera covers
    floor_points: [[...], ...]   # the same spots on the floor plan, in metres
    privacy_mask_polygons: []    # blanked BEFORE detection: restrooms, fitting-room doors, staff-only rooms
    mask_polygons: []            # ignored AFTER detection: mirrors, TVs, posters of people
    pose: {x: 4.0, y: 0.5, height_m: 3.0, heading_deg: 90, fov_deg: 100}   # for the coverage map
    owns: [lobby]                # optional: otherwise the best view of each display wins
    detector_params: {typical_area: 360}   # bgsub only: pixel size of one person differs per camera
```

## The one rule

**Every spot on the floor, and every display, has exactly one owning camera.** A camera only tracks people standing in
what it owns, so overlapping cameras never count the same person twice, and nothing links a person across cameras (no
appearance matching, which would be the "remember someone" capability the privacy design avoids).

* Visitors are door-entry tracks from the entrance camera. The entrance camera owns the lobby (1.5 m around the door).
* Impressions, dwell and engagement for a display come from its owning camera.
* The price: a person walking from one camera's area into another's is two short tracks, so flows between displays stop
  at a boundary. Footfall and per-display numbers are unaffected.
* Lift comparisons use a time-block bootstrap in multi-camera mode, since visitors are not independent samples across
  cameras.

## `storelens cameras --config store.yaml [--png coverage.png] [--json out.json]`

For each camera: how well the calibration fits, how much of the floor the points cover, and pixels per metre. Then for the
store: which camera owns each display, what share of the floor is seen by none, one or several cameras, and warnings.

* **Four points prove nothing.** Four points define a homography exactly, so the reported error is zero whatever you
  clicked. The tool warns. With five or more it reports a **leave-one-out error** (predict each point from the others),
  which is what exposes a mis-clicked point. Status: good up to 8 cm, fair up to 15 cm, poor above.
* **Points on a line** are flagged: they fit but extrapolate badly.
* A display seen by its owner less than 60% is flagged.
* **Pixel size per camera.** For the background-subtraction detector, `typical_area` must match each camera's scale. The
  command prints a suggested value per camera (`suggest_detector_params`); copy it into `detector_params`. It starts from
  a person footprint of about 0.12 m^2 as seen from above, calibrated on the simulator: check it against a real frame.

## Process each camera, then analyse together

```bash
storelens run --config store.yaml --camera door  --video door.mp4  --start 2026-11-14T10:00 --db store.db --label door
storelens run --config store.yaml --camera floor --video floor.mp4 --start 2026-11-14T10:00 --db store.db --label floor
```

Start times must agree (use the box's clock, synced by NTP, not the cameras' own). The Cameras card on the dashboard's Floor plan tab shows
the same calibration and coverage numbers, and the Compare tab warns when a before/after pair was recorded under a
different layout version.

## What is not done yet

The on-site setup wizard (find cameras on the network, click calibration points on a live frame, draw masks, run a
five-minute validation) is not built; it needs the real hardware. Until then, calibration points are entered in
`store.yaml` and checked with `storelens cameras` and `storelens calibrate`. Credentials for RTSP streams stay on the store
computer and never go to the cloud.
