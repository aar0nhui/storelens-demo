"""Simulated live view: camera geometry, privacy of the rendered feed, painter's order, live counters."""
import json
import math

import cv2
import numpy as np
import pytest

from glance import live as live_mod
from glance.live import FloorPanel, LiveState
from glance.sim import webcam
from glance.sim.layout import default_layout
from glance.sim.webcam import Camera, WebcamScene, _seg_hits_rect


@pytest.fixture(scope="module")
def scene():
    return WebcamScene(default_layout())


def person(x, y, h=-1.5, pid=1, shirt=(120, 60, 30), **kw):
    return dict(pid=pid, x=x, y=y, h=h, speed=kw.pop("speed", 0.0), dist=0.0, role=kw.pop("role", "customer"),
                shirt=shirt, hair=(25, 25, 30), tag=None, **kw)


# ----------------------------------------------------------------------------------- geometry
def test_camera_is_not_mirrored_relative_to_the_floor_plan():
    cam = Camera()
    px = {n: cam.project(p)[0] for n, p in {"door": (9, 12, 0), "promo": (9, 0.8, 1), "checkout": (16, 8.6, 1),
                                           "wall_a": (0.7, 5, 1), "endcap": (12, 5, 1)}.items()}
    # door is on the left of the image, the back-wall promo to the right of it, the checkout lower right
    assert px["door"][0] < px["promo"][0] < px["checkout"][0]
    assert px["promo"][1] < px["door"][1] < px["checkout"][1]
    assert px["wall_a"][0] < px["endcap"][0]  # Wall A is further left than Endcap B, as on the plan
    for n, (u, v) in px.items():
        assert 0 <= u <= 960 and 0 <= v <= 540, n


def test_segment_rect_intersection():
    r = (11.0, 4.6, 13.0, 5.6)
    assert _seg_hits_rect((17.3, 11.35), (12.0, 3.6), r)       # person standing behind the endcap
    assert not _seg_hits_rect((17.3, 11.35), (14.0, 8.0), r)    # person in front of it


# ----------------------------------------------------------------------------------- privacy
def test_every_customer_head_is_mosaicked_by_the_products_head_rule(monkeypatch):
    sc = WebcamScene(default_layout())
    seen = []
    real = webcam.obscure
    monkeypatch.setattr(webcam, "obscure", lambda img, regions: (seen.append(np.array(regions)), real(img, regions))[1])
    p = person(9.0, 7.0, speed=1.0)
    sc.render([p], show_overlay=False)
    (regions,) = seen
    hx, hy, r, box = sc._figure(np.zeros((540, 960, 3), np.uint8), dict(p))
    x1, y1, x2, y2 = regions[0]
    assert x1 <= hx <= x2 and y1 <= hy <= y2          # the head is inside the blurred region
    assert (y2 - y1) < 0.5 * (box[3] - box[1])         # ... and it is the head, not the whole body


def test_mannequins_are_not_blurred_but_staff_and_customers_are(monkeypatch):
    sc = WebcamScene(default_layout())
    calls = []
    monkeypatch.setattr(webcam, "obscure", lambda img, regions: (calls.append(len(regions)), img)[1])
    sc.render([dict(person(2.2, 10.2), mannequin=True, role="mannequin")], show_overlay=False)
    assert calls == []
    sc.render([person(9, 7), person(8, 6, pid=2, role="staff"), dict(person(2.2, 10.2), mannequin=True)], show_overlay=False)
    assert calls == [2]


# ----------------------------------------------------------------------------------- painter's order
def _green(img):
    b, g, r = (img[..., i].astype(int) for i in range(3))
    return (g > 190) & (r < 90) & (b < 90)


def test_person_behind_a_fixture_is_hidden_by_it(scene):
    it = next(i for i in scene.items if i["id"] == "endcap_b")
    y0, y1, x0, x1 = it["bbox"]
    full = np.zeros((540, 960), np.uint8)
    full[y0:y1, x0:x1] = it["mask"].astype(np.uint8)
    inside = cv2.erode(full, np.ones((5, 5), np.uint8)) > 0
    green = (0, 255, 0)
    behind = scene.render([person(12.0, 3.6, shirt=green)], show_overlay=False)
    front = scene.render([person(14.5, 8.0, shirt=green)], show_overlay=False)
    assert _green(front).sum() > 150                       # sanity: a green shirt is visible when nothing hides it
    assert (_green(behind) & inside).sum() == 0            # ... but never paints over the endcap in front of him


# ----------------------------------------------------------------------------------- live counters
def _walk(uid, pts, t0=0.0, vote=False):
    return [(t0 + i / 10, [(uid, np.array(p, float), vote)]) for i, p in enumerate(pts)]


def _feed(cfg, frames):
    st, out = LiveState(cfg), None
    for t, items in frames:
        out = st.update(t, items)
    return st, out


def test_standing_at_a_display_counts_as_engaged(cfg):
    pts = [(6.0, 11.0 - 0.1 * i) for i in range(20)] + [(6.0, 9.2)] * 50   # walk in, then stand 5 s at the Front Table
    st, out = _feed(cfg, _walk(1, pts))
    assert out["counts"] == {"in_store": 1, "visitors": 1, "impressions": 1, "engaged": 1}
    assert out["engaged_zones"] == {"front_table"} and out["tracks"][1]["engaged"] == "front_table"


def test_walking_through_a_display_is_an_impression_not_engagement(cfg):
    pts = [(2.0 + 0.12 * i, 9.2) for i in range(60)]
    _, out = _feed(cfg, _walk(1, pts))
    assert out["counts"]["impressions"] >= 1 and out["counts"]["engaged"] == 0 and not out["engaged_zones"]


def test_staff_are_flagged_and_never_counted(cfg):
    pts = [(16.0, 7.3)] * 60
    st, out = _feed(cfg, _walk(7, pts, vote=True))
    assert out["tracks"][7]["staff"] and out["counts"] == {"in_store": 0, "visitors": 0, "impressions": 0, "engaged": 0}


def test_floor_panel_draws_one_marker_per_cluster_of_staff_ghosts(cfg):
    panel = FloorPanel(cfg, default_layout())
    t = lambda x, y: {"x": x, "y": y, "staff": True, "stopped": True, "engaged": None}
    img = panel.render({1: t(16.0, 7.2), 2: t(16.3, 7.3), 3: t(16.2, 7.0), 4: t(3.0, 5.0)}, set(),
                       {"in_store": 0, "visitors": 0, "impressions": 0, "engaged": 0}, "2026-11-02 10:00:00")
    assert img.shape == (540, 720, 3) and set(panel.trails) == {1, 4}


# ----------------------------------------------------------------------------------- end to end
@pytest.mark.slow
def test_make_live_clip_writes_two_synchronised_videos(tmp_path):
    info = live_mod.make_live_clip(tmp_path / "live", seconds=5, warmup_s=50, progress=False)
    assert info["simulated"] is True and info["pipeline"]["detector"] == "bgsub"
    frames = {}
    for name in ("camera", "floorplan"):
        cap = cv2.VideoCapture(str(tmp_path / "live" / info[name]))
        assert cap.isOpened()
        frames[name] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        ok, f = cap.read()
        assert ok and f.shape[0] in (540,) and f.shape[1] in (960, 720)
    assert frames["camera"] == frames["floorplan"] == 100
    assert json.loads((tmp_path / "live" / "live.json").read_text())["fps"] == 20
    assert not (tmp_path / "live" / "_work").exists()  # scratch files (incl. the temp event store) are removed
