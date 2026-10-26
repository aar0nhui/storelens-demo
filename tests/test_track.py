"""Tracker + stitcher behaviour on synthetic detections (floor metres <-> pixels at 50 px/m)."""
import numpy as np
import supervision as sv

from glance.config import Thresholds
from glance.track import TrackManager

PX = 50.0
FPS = 10.0


def box(x, y, w=0.5, h=0.3):
    return [(x - w / 2) * PX, (y - h / 2) * PX, (x + w / 2) * PX, (y + h / 2) * PX]


def run(frames, floor=(18.0, 12.0)):
    """frames: list of list[(x, y)] in metres per frame. Returns committed tracklets."""
    out = []
    tm = TrackManager(Thresholds(), FPS, out.append, floor)
    for i, people in enumerate(frames):
        xyxy = np.array([box(x, y) for x, y in people]).reshape(-1, 4)
        det = sv.Detections(xyxy=xyxy, confidence=np.full(len(people), 0.9), class_id=np.zeros(len(people), int))
        tm.update(i / FPS, det, np.array(people, float).reshape(-1, 2), np.zeros(len(people), bool))
    tm.flush()
    return out, tm


def walker(x0, y0, vx, n, start=0):
    return {i: (x0 + vx * (i - start), y0) for i in range(start, start + n)}


def test_single_person_is_one_tracklet():
    a = walker(5.0, 6.0, 0.1, 40)
    out, _ = run([[a[i]] for i in range(40)])
    assert len(out) == 1 and len(out[0].t) >= 36


def test_short_occlusion_at_constant_velocity_is_bridged_by_the_tracker_itself():
    # 1 s gap, the person keeps walking: the Kalman prediction still overlaps the re-detection, so
    # ByteTrack keeps the id and the stitcher has nothing to do
    frames = [[(5.0 + 0.1 * i, 6.0)] if i < 30 else [] if i < 40 else [(8.8 + 0.1 * (i - 40), 6.0)] for i in range(70)]
    out, tm = run(frames)
    assert tm.stats["stitched"] == 0 and len(out) == 1


def test_lost_track_is_stitched_when_new_one_appears_close_by():
    # A walks east, vanishes for 1 s, re-appears 0.9 m behind where it vanished (turned around), so the
    # constant-velocity prediction misses it entirely and ByteTrack starts a new id; motion-only stitching joins them
    frames = [[(5.0 + 0.1 * i, 6.0)] if i < 30 else [] if i < 40 else [(7.0 - 0.1 * (i - 40), 6.0)] for i in range(70)]
    out, tm = run(frames)
    assert tm.stats["stitched"] == 1
    assert len(out) == 1 and out[0].t[-1] - out[0].t[0] > 5.5


def test_far_apart_tracks_are_not_stitched():
    frames = []
    for i in range(70):
        if i < 30:
            frames.append([(5.0 + 0.1 * i, 6.0)])
        elif i < 40:
            frames.append([])
        else:
            frames.append([(14.0, 3.0)])  # 6+ m away
    out, tm = run(frames)
    assert tm.stats["stitched"] == 0 and len(out) == 2


def test_never_stitch_across_the_floor_edge():
    # a person leaves through the door (y -> 11.9) and a different one enters 1.2 s later at the same spot
    frames = []
    for i in range(80):
        if i < 30:
            frames.append([(9.0, 8.0 + 0.13 * i)])
        elif i < 42:
            frames.append([])
        else:
            frames.append([(9.2, 11.9 - 0.13 * (i - 42))])
    out, tm = run(frames)
    assert tm.stats["stitched"] == 0 and len(out) == 2


def test_concurrent_people_are_never_merged():
    a, b = walker(5.0, 6.0, 0.08, 60), walker(5.0, 6.7, 0.08, 60)  # two people walking side by side
    out, tm = run([[a[i], b[i]] for i in range(60)])
    assert len(out) == 2 and tm.stats["stitched"] == 0
