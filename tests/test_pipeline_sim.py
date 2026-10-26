"""End-to-end on a short synthetic clip: render -> detect -> track -> analyse -> store -> validate.

Takes ~1 minute (renders ~3 minutes of video). Skip with: pytest -m "not slow"
"""
import sqlite3

import cv2
import numpy as np
import pytest
import supervision as sv

from glance.pipeline import Pipeline
from glance.queries import Analytics, Window
from glance.sim.generate import Period, generate_period
from glance.sim.layout import build_store_config, default_layout
from glance.store import EventStore
from glance.validate import load_gt, validate

pytestmark = pytest.mark.slow
START = 1_800_000_000.0


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    d = tmp_path_factory.mktemp("e2e")
    layout = default_layout()
    cfg = build_store_config(layout, seed=21)
    video, gt, _ = generate_period(layout, Period("clip", minutes=3.0, seed=21, base_per_min=6.0), d, verbose=False)
    st = EventStore(d / "e2e.db")
    meta = Pipeline(cfg, st, label="clip").run(video, START, preview=d / "preview.mp4", progress=False)
    report = validate(cfg, st, meta["run_id"], load_gt(gt))
    return {"dir": d, "cfg": cfg, "st": st, "meta": meta, "report": report, "video": video}


def test_footfall_is_close_to_ground_truth(e2e):
    f = e2e["report"]["footfall"]
    assert f["gt"] >= 8, "the clip should contain a meaningful number of shoppers"
    assert abs(f["pipeline"] - f["gt"]) <= max(3, 0.25 * f["gt"])
    assert f["precision"] >= 0.8 and f["recall"] >= 0.7


def test_fixture_impressions_are_close_and_staff_never_leak(e2e):
    zs = [z for z in e2e["report"]["zones"].values() if z["kind"] == "fixture"]
    assert sum(z["impressions"]["staff_leak"] for z in zs) == 0
    gt, pl = sum(z["impressions"]["gt"] for z in zs), sum(z["impressions"]["pipeline"] for z in zs)
    assert gt > 0 and abs(pl - gt) <= max(4, 0.25 * gt)


def test_store_has_customers_and_flags_staff_separately(e2e):
    s = Analytics(e2e["st"], e2e["cfg"]).summary(Window())
    assert s["visitors"] >= 8 and s["footfall"] >= 8
    assert e2e["meta"]["staff"] >= 1  # the simulator always has two people in red vests


def test_database_contains_no_trajectories_images_or_stable_ids(e2e):
    e2e["st"].conn.commit()
    con = sqlite3.connect(e2e["st"].path)
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        assert not ({"x", "y", "xy", "lat", "lon", "image", "frame", "face", "embedding", "blob"} & cols), (t, cols)
    keys = [k for (k,) in con.execute("SELECT visit_key FROM visits")]
    assert all(len(k) == 12 for k in keys) and len(set(keys)) == len(keys)
    # Binary payloads would be a red flag: nothing in the DB should be a BLOB
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        for c in cols:
            assert con.execute(f"SELECT COUNT(*) FROM {t} WHERE typeof({c})='blob'").fetchone()[0] == 0, (t, c)
    meta_priv = e2e["meta"]["privacy"]
    assert meta_priv["trajectories_persisted"] is False and meta_priv["raw_frames_persisted"] == 0


def test_preview_video_is_written_and_playable(e2e):
    cap = cv2.VideoCapture(str(e2e["dir"] / "preview.mp4"))
    assert cap.isOpened() and cap.get(cv2.CAP_PROP_FRAME_COUNT) > 100
    ok, frame = cap.read()
    assert ok and frame.ndim == 3


def test_preview_frames_are_obscured_before_anything_is_drawn(e2e):
    cfg = e2e["cfg"]
    pipe = Pipeline(cfg, EventStore(e2e["dir"] / "tmp.db"), label="x")
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (640, 960, 3), dtype=np.uint8)
    box = np.array([[300.0, 200.0, 360.0, 320.0]])
    det = sv.Detections(xyxy=box, tracker_id=np.array([1]), confidence=np.array([0.9]), class_id=np.array([0]))
    out = pipe._render_preview(frame.copy(), det, {}, 1, 0)
    inner = (slice(230, 290), slice(310, 350))  # inside the box, away from outline/label
    assert out[inner].std() < frame[inner].std() * 0.4
    assert len({tuple(p) for p in out[inner].reshape(-1, 3)}) <= 40  # a mosaic, not a faint blur
