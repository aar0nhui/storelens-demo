"""Multi-object tracking (ByteTrack) + live track book-keeping + motion-only tracklet stitching.

Privacy/accuracy design choice: there is NO appearance re-identification. ByteTrack associates by
motion/IoU only, and the stitcher re-links a track that vanished <= ``stitch_window_s`` ago within
``stitch_max_dist_m`` of where a new one appears. IDs are therefore short-lived by construction and
cannot be used to recognise someone across visits or days.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import supervision as sv
from trackers import ByteTrackTracker

from .config import Thresholds


@dataclass
class Live:
    uid: int
    t: list[float] = field(default_factory=list)
    xy: list[tuple[float, float]] = field(default_factory=list)
    votes: list[bool] = field(default_factory=list)
    last_seen: float = -1e18
    stitched_from: int = 0

    def add(self, t: float, xy, vote: bool):
        if self.t and t <= self.t[-1] + 1e-9:
            return  # an alias already wrote this timestamp
        self.t.append(t)
        self.xy.append((float(xy[0]), float(xy[1])))
        self.votes.append(bool(vote))
        self.last_seen = t


@dataclass
class Tracklet:
    uid: int
    t: np.ndarray
    xy: np.ndarray
    staff_vote_ratio: float
    stitched_from: int = 0


def _to_tracklet(live: Live) -> Tracklet:
    return Tracklet(uid=live.uid, t=np.asarray(live.t, float), xy=np.asarray(live.xy, float),
                    staff_vote_ratio=float(np.mean(live.votes)) if live.votes else 0.0,
                    stitched_from=live.stitched_from)


class TrackManager:
    def __init__(self, thr: Thresholds, fps: float, on_commit: Callable[[Tracklet], None],
                 floor_size: tuple[float, float] | None = None):
        self.thr, self.fps, self.on_commit = thr, fps, on_commit
        self.floor_size = floor_size  # (width_m, height_m); enables the edge guard for stitching
        self.tracker = ByteTrackTracker(
            lost_track_buffer=max(int(thr.lost_buffer_s * 30), 1), frame_rate=fps,
            track_activation_threshold=thr.track_activation, minimum_consecutive_frames=thr.min_consecutive,
            minimum_iou_threshold=0.05, high_conf_det_threshold=thr.high_conf)
        self.by_id: dict[int, Live] = {}  # tracker id -> Live (aliases after stitching point to the same Live)
        self.pending: list[Live] = []  # ended, waiting out the stitch window
        self.stats = {"stitched": 0, "committed": 0}
        # (track uid, floor xy, staff vote) for every confirmed track matched in the most recent frame.
        # In-memory only, for live views; nothing here is persisted.
        self.last: list[tuple[int, np.ndarray, bool]] = []
        self.last_boxes: list[np.ndarray] = []  # box (xyxy) of each entry in ``last``, same order
        self._end_gap = thr.lost_buffer_s + 0.5

    # ---------------------------------------------------------------------------------- stepping
    def update(self, t: float, detections: sv.Detections, floor_xy: np.ndarray, votes: np.ndarray) -> sv.Detections:
        """Feed one frame. ``floor_xy``/``votes`` are aligned with ``detections``.

        Returns the confirmed tracked detections (tracker_id >= 0) for drawing/preview.
        """
        out = self.tracker.update(detections)
        confirmed = out[out.tracker_id >= 0] if len(out) else out
        self.last = []
        self.last_boxes = []
        # Map tracker outputs back to our per-detection floor points / votes by box identity.
        if len(confirmed):
            idx = self._match_back(detections.xyxy, confirmed.xyxy)
            active = {int(i) for i in confirmed.tracker_id}  # matched THIS frame: never stitch to these
            for k, tid in enumerate(confirmed.tracker_id):
                j = idx[k]
                if j < 0:
                    continue
                tid = int(tid)
                live = self.by_id.get(tid)
                if live is None:
                    live = Live(uid=tid)
                    self._try_stitch(live, t, floor_xy[j], active)
                    self.by_id[tid] = live
                live.add(t, floor_xy[j], bool(votes[j]))
                self.last.append((live.uid, np.asarray(floor_xy[j], float), bool(votes[j])))
                self.last_boxes.append(np.asarray(detections.xyxy[j], float))
        self._expire(t)
        return confirmed

    @staticmethod
    def _match_back(det_xyxy: np.ndarray, out_xyxy: np.ndarray) -> np.ndarray:
        """Tracker returns boxes; recover the index of the originating detection (nearest box)."""
        if len(det_xyxy) == 0:
            return np.full(len(out_xyxy), -1)
        d = np.abs(out_xyxy[:, None, :] - det_xyxy[None, :, :]).sum(axis=2)
        j = d.argmin(axis=1)
        return np.where(d[np.arange(len(j)), j] < 8.0, j, -1)

    def _near_edge(self, xy) -> bool:
        if self.floor_size is None:
            return False
        m = self.thr.stitch_edge_margin_m
        return not (m <= xy[0] <= self.floor_size[0] - m and m <= xy[1] <= self.floor_size[1] - m)

    def _try_stitch(self, new: Live, t: float, xy, active: set[int]) -> None:
        thr = self.thr
        if self._near_edge(xy):  # a track born at the edge is someone entering view, not a lost track
            return
        best, best_d = None, thr.stitch_max_dist_m
        busy = {id(self.by_id[i]) for i in active if i in self.by_id}
        cands = list(self.pending) + [l for l in {id(v): v for v in self.by_id.values()}.values()
                                      if id(l) not in busy]
        min_gap = 3.0 / self.fps  # a track missed for a couple of frames is just coasting, not gone
        for cand in cands:
            if cand is new or not cand.t:
                continue
            gap = t - cand.last_seen
            if gap < min_gap or gap > thr.stitch_window_s or self._near_edge(cand.xy[-1]):
                continue
            d = float(np.hypot(cand.xy[-1][0] - xy[0], cand.xy[-1][1] - xy[1]))
            if d < best_d:
                best, best_d = cand, d
        if best is None:
            return
        new.uid = best.uid
        new.t, new.xy, new.votes = list(best.t), list(best.xy), list(best.votes)
        new.last_seen = best.last_seen
        new.stitched_from = best.stitched_from + 1
        self.stats["stitched"] += 1
        # `best` is absorbed: drop it from pending / by_id (its aliases keep pointing at `new`)
        if best in self.pending:
            self.pending.remove(best)
        for k, v in list(self.by_id.items()):
            if v is best:
                self.by_id[k] = new

    def _expire(self, t: float) -> None:
        seen: dict[int, Live] = {id(v): v for v in self.by_id.values()}
        for live in seen.values():
            if t - live.last_seen > self._end_gap:
                for k, v in list(self.by_id.items()):
                    if v is live:
                        del self.by_id[k]
                self.pending.append(live)
        keep = []
        for live in self.pending:
            if t - live.last_seen > max(self.thr.stitch_window_s, self._end_gap) + 0.5:
                self._commit(live)
            else:
                keep.append(live)
        self.pending = keep

    def _commit(self, live: Live) -> None:
        if live.t:
            self.stats["committed"] += 1
            self.on_commit(_to_tracklet(live))

    def flush(self) -> None:
        for live in {id(v): v for v in self.by_id.values()}.values():
            self._commit(live)
        self.by_id.clear()
        for live in self.pending:
            self._commit(live)
        self.pending = []

    def live_count(self) -> int:
        return len({id(v) for v in self.by_id.values()})
