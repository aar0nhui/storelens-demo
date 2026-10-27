import copy
import math

import numpy as np

from storelens.analysis import MotionFacing, analyze_samples, entry_crossings, is_engaged
from tests.conftest import make_traj


def visits(a, zone):
    return [z for z in a.zone_visits if z.zone_id == zone]


def test_pass_through_is_impression_not_engagement(cfg):
    # walk straight through the Front Table exposure zone (x 3.7-8.3, y 8.6-9.9) without stopping
    t, xy, _ = make_traj([("start", (2.0, 9.2)), ("walk", (10.0, 9.2), 1.2)])
    a = analyze_samples(t, xy, cfg)
    (v,) = visits(a, "front_table")
    assert v.dwell_s > 2.0
    assert v.stopped_s == 0.0
    assert not is_engaged(v.stopped_s, v.facing_stopped_s, 2.0, "stop")


def test_browsing_is_engaged_in_both_modes(cfg):
    # approach from the door heading north, stop in front of the table, look, leave
    t, xy, _ = make_traj([("start", (6.0, 11.9)), ("walk", (6.0, 9.2), 1.2), ("stand", 6), ("walk", (6.0, 11.9), 1.2)])
    a = analyze_samples(t, xy, cfg)
    (v,) = visits(a, "front_table")
    assert v.stopped_s >= 5.0
    assert is_engaged(v.stopped_s, v.facing_stopped_s, 2.0, "stop")
    assert is_engaged(v.stopped_s, v.facing_stopped_s, 2.0, "stop_facing")  # heading held = north = towards table


def test_stopping_with_back_turned_needs_facing_mode(cfg):
    t, xy, h = make_traj([("start", (6.0, 11.9)), ("walk", (6.0, 9.2), 1.2), ("turn", math.pi / 2, 6),
                          ("walk", (6.0, 11.9), 1.2)])
    a = analyze_samples(t, xy, cfg, facing_override=h)  # true headings: south during the stop
    (v,) = visits(a, "front_table")
    assert is_engaged(v.stopped_s, v.facing_stopped_s, 2.0, "stop")  # a stop-only metric can't tell
    assert not is_engaged(v.stopped_s, v.facing_stopped_s, 2.0, "stop_facing")


def test_too_short_stop_is_not_engaged(cfg):
    t, xy, _ = make_traj([("start", (6.0, 11.9)), ("walk", (6.0, 9.2), 1.2), ("stand", 1.0), ("walk", (6.0, 11.9), 1.2)])
    (v,) = visits(analyze_samples(t, xy, cfg), "front_table")
    assert not is_engaged(v.stopped_s, v.facing_stopped_s, 2.0, "stop")


def test_area_zone_has_no_facing_component(cfg):
    t, xy, _ = make_traj([("start", (16.0, 11.5)), ("walk", (16.0, 10.0), 1.0), ("stand", 12)])
    (v,) = visits(analyze_samples(t, xy, cfg), "checkout")
    assert v.stopped_s > 10 and v.facing_stopped_s == 0.0


def test_entry_exit_and_jitter(cfg):
    ent = cfg.entrance
    # hover on the door line with +-10 cm jitter for 8 s, then walk in, then out again
    rng = np.random.default_rng(0)
    hover = [(9.0 + rng.normal(0, .05), 11.2 + rng.normal(0, .08)) for _ in range(80)]
    t0 = 0.0
    pts = [(9.0, 11.9)] * 5 + hover + [(9.0, 11.0 - 0.1 * i) for i in range(20)] + [(9.0, 9.0 + 0.1 * i) for i in range(30)]
    t = t0 + np.arange(len(pts)) / 10
    out = entry_crossings(t, np.array(pts), ent)
    assert [d for _, d in out] == [+1, -1]  # one entry, one exit, jitter ignored


def test_walking_around_the_door_does_not_count(cfg):
    # cross the entrance *line extension* far from the door opening (x=2): not an entry
    pts = [(2.0, 11.9 - 0.1 * i) for i in range(30)]
    assert entry_crossings(np.arange(30) / 10, np.array(pts), cfg.entrance) == []


def test_short_and_static_tracks_are_dropped(cfg):
    t, xy, _ = make_traj([("start", (5, 5)), ("walk", (5.5, 5), 1.0)])
    assert analyze_samples(t, xy, cfg).dropped == "short"
    t = np.arange(0, 120, 0.1)
    xy = np.stack([np.full_like(t, 3.0), np.full_like(t, 4.0)], axis=1) + np.random.default_rng(1).normal(0, .02, (len(t), 2))
    assert analyze_samples(t, xy, cfg).dropped == "static"  # a mannequin


def test_staff_zone_dwell_is_reported(cfg):
    t = np.arange(0, 60, 0.1)
    xy = np.stack([np.full_like(t, 16.0) + np.sin(t) * .1, np.full_like(t, 7.3)], axis=1)
    a = analyze_samples(t, xy, cfg)
    assert a.staff_zone_fraction > 0.9
    assert not a.zone_visits  # staff zone never yields customer metrics


def test_heat_seconds_match_duration(cfg):
    t, xy, _ = make_traj([("start", (6.0, 11.9)), ("walk", (6.0, 5.0), 1.2), ("stand", 4)])
    a = analyze_samples(t, xy, cfg)
    assert abs(sum(a.heat.values()) - a.duration_s) < 0.5


def test_motion_facing_semantics():
    sp = np.array([0, 0, 1, 1, 0, 0, 1.0])
    hd = np.array([0, 0, .5, .6, .6, .6, 2.0])
    f = MotionFacing().facing(sp, hd, 0.45)
    assert np.isnan(f[:2]).all() and f[2] == .5 and f[4] == .6 and f[6] == 2.0  # unknown -> moving -> held -> moving
    assert np.isnan(MotionFacing().facing(np.zeros(5), np.zeros(5), 0.45)).all()  # never moved: unknown


def test_per_zone_override_of_engage_seconds(cfg):
    c = copy.deepcopy(cfg)
    z = c.zone("front_table")
    z.min_engage_s = 10.0
    assert c.engage_seconds(z) == 10.0 and c.engage_seconds(c.zone("wall_a")) == c.thresholds.min_engage_s
