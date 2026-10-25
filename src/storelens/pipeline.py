"""End-to-end pipeline: video -> detections -> tracks -> floor coordinates -> events -> SQLite."""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np
import supervision as sv

from .analysis import FacingEstimator, MotionFacing, analyze_samples, is_engaged
from .config import StoreConfig
from .detect import Detector, make_detector
from .geometry import Homography, points_in_polygon, project_zones_to_pixels
from .privacy import Anonymizer, PreviewWriter, obscure, privacy_regions
from .staff import StaffClassifier
from .store import EventStore
from .track import TrackManager, Tracklet


class Pipeline:
    def __init__(self, cfg: StoreConfig, store: EventStore, camera_id: str | None = None,
                 detector: Detector | None = None, estimator: FacingEstimator | None = None,
                 label: str = "run", anonymizer: Anonymizer | None = None):
        self.cfg = cfg
        self.cam = cfg.camera(camera_id)
        self.store = store
        self.label = label
        self.H = Homography.from_camera(self.cam)
        self.detector = detector or make_detector(cfg.detector, mask_polygons=self.cam.mask_polygons)
        self.estimator = estimator or MotionFacing()
        self.staff = StaffClassifier(cfg.staff)
        self.anon = anonymizer or Anonymizer(cfg.privacy.id_rotation_hours)
        self._reset_counters()

    def _reset_counters(self) -> None:
        self.n = {"customers": 0, "staff": 0, "dropped_short": 0, "dropped_static": 0, "entries": 0,
                  "exits": 0, "zone_visits": 0, "engaged": 0}
        self.run_id = -1

    # ------------------------------------------------------------------------------------ commit
    def _on_commit(self, tl: Tracklet) -> None:
        a = analyze_samples(tl.t, tl.xy, self.cfg, estimator=self.estimator)
        if a.dropped == "short":
            self.n["dropped_short"] += 1
            return
        if a.dropped == "static":
            self.n["dropped_static"] += 1
            return
        key = self.anon.key(self.cam.id, tl.uid, a.t_first)
        is_staff = (self.staff.enabled and tl.staff_vote_ratio >= self.cfg.staff.vote_ratio) or \
            a.staff_zone_fraction >= self.cfg.staff.zone_time_fraction
        if is_staff:
            self.n["staff"] += 1
            self.store.add_visit(self.run_id, key, "staff", a.t_first, a.t_last)
            return
        self.n["customers"] += 1
        self.store.add_visit(self.run_id, key, "customer", a.t_first, a.t_last)
        self.store.add_zone_visits(self.run_id, key, a.zone_visits)
        self.store.add_entries(self.run_id, key, a.entries)
        self.store.add_heat(self.run_id, a.heat)
        self.n["zone_visits"] += len(a.zone_visits)
        mode = self.cfg.thresholds.engagement_mode
        self.n["engaged"] += sum(
            is_engaged(z.stopped_s, z.facing_stopped_s, self.cfg.engage_seconds(self.cfg.zone(z.zone_id)), mode)
            for z in a.zone_visits if self.cfg.zone(z.zone_id).kind == "fixture")
        self.n["entries"] += sum(1 for _, d in a.entries if d > 0)
        self.n["exits"] += sum(1 for _, d in a.entries if d < 0)

    # ------------------------------------------------------------------------------------ run
    def run(self, video: str | Path, start_ts: float, preview: str | Path | None = None,
            max_frames: int | None = None, allow_raw_preview: bool = False, progress: bool = True,
            frame_hook=None) -> dict:
        thr = self.cfg.thresholds
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            raise FileNotFoundError(f"cannot open video {video}")
        vfps = cap.get(cv2.CAP_PROP_FPS) or thr.process_fps
        n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        stride = max(int(round(vfps / thr.process_fps)), 1)
        eff_fps = vfps / stride
        # NOTE: analysis assumes thresholds.process_fps == effective fps; keep them aligned.
        if abs(eff_fps - thr.process_fps) > 0.51:
            thr.process_fps = eff_fps

        self._reset_counters()
        self.run_id = self.store.begin_run(self.label, self.cam.id, str(video), start_ts)
        self.detector.prepare(str(video))
        tm = TrackManager(thr, eff_fps, self._on_commit, (self.cfg.floor.width_m, self.cfg.floor.height_m))
        pv: PreviewWriter | None = None
        zone_px = project_zones_to_pixels(self.cfg, self.cam) if preview else {}

        frames = det_total = 0
        t0 = time.time()
        last_t = start_ts
        mask_polys = [np.asarray(p, np.int32) for p in self.cam.mask_polygons]
        idx = -1
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            idx += 1
            if idx % stride:
                continue
            if max_frames and frames >= max_frames:
                break
            t = start_ts + idx / vfps
            last_t = t
            dets = self.detector.detect(frame)
            dets = self._apply_masks(dets, mask_polys)
            anchors = self._anchors(dets.xyxy)
            floor = self.H.to_floor(anchors) if len(anchors) else np.zeros((0, 2))
            votes = np.array([self.staff.vote(frame, b) for b in dets.xyxy], dtype=bool) \
                if len(dets) else np.zeros(0, bool)
            confirmed = tm.update(t, dets, floor, votes)
            frames += 1
            det_total += len(dets)
            if frame_hook is not None:  # live views: (t, [(track uid, floor xy, staff vote), ...]); in memory only
                frame_hook(t, tm.last)

            if preview:
                if pv is None:
                    h, w = frame.shape[:2]
                    pv = PreviewWriter(preview, (w, h), eff_fps, self.cfg.privacy, allow_raw_preview)
                pv.write(self._render_preview(frame, confirmed, zone_px, tm.live_count(), frames))
            if progress and frames % 1000 == 0:
                print(f"  frame {idx}/{n_total}  {frames / (time.time() - t0):.0f} fps  tracks live={tm.live_count()}")
        cap.release()
        tm.flush()
        if pv:
            pv.close()

        elapsed = time.time() - t0
        meta = {
            "frames_processed": frames, "video_fps": vfps, "process_fps": eff_fps, "elapsed_s": round(elapsed, 1),
            "processing_fps": round(frames / max(elapsed, 1e-6), 1), "detector": self.detector.name,
            "mean_detections_per_frame": round(det_total / max(frames, 1), 2),
            "tracklets_committed": tm.stats["committed"], "stitched": tm.stats["stitched"],
            "customers": self.n["customers"], "staff": self.n["staff"],
            "dropped_short": self.n["dropped_short"], "dropped_static": self.n["dropped_static"],
            "entries": self.n["entries"], "exits": self.n["exits"],
            "calibration_rmse_cm": round(self.H.reprojection_rmse_cm(), 1),
            "camera_anchor": self.cam.anchor, "facing_estimator": type(self.estimator).__name__,
            "privacy": {"blur": self.cfg.privacy.blur, "trajectories_persisted": False,
                        "raw_frames_persisted": 0, "id_rotation_hours": self.cfg.privacy.id_rotation_hours,
                        "appearance_reid": False},
        }
        self.store.finish_run(self.run_id, last_t, meta)
        meta["run_id"] = self.run_id
        return meta

    # ------------------------------------------------------------------------------------ helpers
    def _anchors(self, xyxy: np.ndarray) -> np.ndarray:
        if len(xyxy) == 0:
            return np.zeros((0, 2))
        cx = (xyxy[:, 0] + xyxy[:, 2]) / 2
        cy = (xyxy[:, 1] + xyxy[:, 3]) / 2 if self.cam.anchor == "center" else xyxy[:, 3]
        return np.stack([cx, cy], axis=1)

    def _apply_masks(self, dets: sv.Detections, polys: list[np.ndarray]) -> sv.Detections:
        if not polys or len(dets) == 0:
            return dets
        keep = np.ones(len(dets), bool)
        for i, (cx, cy) in enumerate(self._anchors(dets.xyxy)):
            if any(cv2.pointPolygonTest(p, (float(cx), float(cy)), False) >= 0 for p in polys):
                keep[i] = False
        return dets[keep]

    def _render_preview(self, frame: np.ndarray, confirmed: sv.Detections, zone_px: dict, live: int,
                        n: int) -> np.ndarray:
        out = frame.copy()
        xyxy = confirmed.xyxy if len(confirmed) else np.zeros((0, 4))
        out = obscure(out, privacy_regions(xyxy, self.cam.anchor, self.cfg.privacy, out.shape))
        for zid, poly in zone_px.items():
            kind = self.cfg.zone(zid).kind
            col = {"fixture": (60, 180, 255), "area": (120, 220, 120), "staff": (200, 120, 255)}[kind]
            cv2.polylines(out, [poly.astype(np.int32)], True, col, 1, cv2.LINE_AA)
        for b, tid in zip(xyxy.astype(int), confirmed.tracker_id if len(confirmed) else []):
            cv2.rectangle(out, (b[0], b[1]), (b[2], b[3]), (0, 255, 0), 1)
            cv2.putText(out, f"#{int(tid) % 1000}", (b[0], max(b[1] - 3, 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                        (0, 255, 0), 1, cv2.LINE_AA)
        cv2.putText(out, f"anonymous preview | tracked now: {live}", (8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1, cv2.LINE_AA)
        return out
