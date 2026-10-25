"""Business metrics over a hand-built event store (so every expected number is known in advance)."""
import copy

import numpy as np
import pytest

from storelens.analysis import ZoneVisit
from storelens.insights import generate_insights
from storelens.queries import Analytics, Window
from storelens.store import EventStore

T0 = 1_800_000_000.0
FIX = ["front_table", "wall_a", "gondola_c", "endcap_b", "back_promo"]


class Builder:
    """Tiny helper that writes anonymous visits straight into the store."""

    def __init__(self, path):
        self.st = EventStore(path)
        self.n = 0

    def run(self, label, t_start):
        self.rid = self.st.begin_run(label, "cam1", f"{label}.mp4", t_start)
        self.t = t_start
        return self.rid

    def visit(self, zones=(), role="customer", enter=True, exit_=True, dt=20.0):
        """zones: iterable of (zone_id, dwell_s, stopped_s, facing_stopped_s)."""
        self.n += 1
        key = f"{self.n:012x}"
        t = self.t
        self.st.add_visit(self.rid, key, role, t, t + 60)
        zvs, tz = [], t + 5
        for zid, dwell, stopped, facing in zones:
            zvs.append(ZoneVisit(zid, tz, tz + dwell, dwell, stopped, facing))
            tz += dwell + 1
        self.st.add_zone_visits(self.rid, key, zvs)
        ents = ([(t, 1)] if enter else []) + ([(t + 59, -1)] if exit_ else [])
        self.st.add_entries(self.rid, key, ents)
        self.t += dt
        return key

    def finish(self):
        self.st.finish_run(self.rid, self.t, {"customers": self.n})
        self.st.commit()


@pytest.fixture
def db(tmp_path):
    return Builder(tmp_path / "t.db")


def test_summary_counts_and_staff_exclusion(cfg, db):
    db.run("a", T0)
    db.visit([("front_table", 6, 4.0, 4.0)])                      # impression + engaged
    db.visit([("front_table", 3, 0.0, 0.0), ("wall_a", 8, 5.0, 0.0)])  # 2 impressions, engaged only at wall_a
    db.visit([("checkout", 12, 10.0, 0.0)])                       # area zone: never an impression/engagement
    db.visit([("front_table", 30, 28.0, 28.0)], role="staff")     # staff rows (should never be written; ignored if they are)
    db.finish()
    s = Analytics(db.st, cfg).summary(Window())
    assert s["visitors"] == 3 and s["staff_tracklets"] == 1
    assert s["footfall"] == 3 and s["exits"] == 3  # staff door crossings are excluded too
    assert s["impressions"] == 3 and s["engaged"] == 2
    assert s["engaged_visitors"] == 2
    assert s["engagement_rate"] == pytest.approx(2 / 3)


def test_staff_zone_and_unknown_zone_never_become_metrics(cfg, db):
    db.run("a", T0)
    db.visit([("staff_area", 30, 25.0, 0.0)])
    db.visit([("front_table", 5, 3.0, 3.0)])
    db.finish()
    an = Analytics(db.st, cfg)
    assert {z["zone_id"] for z in an.zones(Window())} == {"front_table", "wall_a", "gondola_c", "endcap_b", "back_promo", "checkout"}
    assert an.summary(Window())["impressions"] == 1


def test_engagement_mode_is_applied_at_query_time_to_the_same_data(cfg, db):
    db.run("a", T0)
    for _ in range(10):
        db.visit([("front_table", 8, 5.0, 0.0)])  # stood there 5 s but never facing it (back turned / on the phone)
    for _ in range(10):
        db.visit([("front_table", 8, 5.0, 5.0)])  # stood there 5 s, facing it
    db.finish()
    stop = Analytics(db.st, cfg).zones(Window())[0]
    c2 = copy.deepcopy(cfg)
    c2.thresholds.engagement_mode = "stop_facing"
    facing = Analytics(db.st, c2).zones(Window())[0]
    assert stop["impressions"] == facing["impressions"] == 20
    assert stop["engaged"] == 20 and facing["engaged"] == 10  # stricter definition, same stored facts


def test_per_zone_min_engage_seconds_is_respected(cfg, db):
    db.run("a", T0)
    for _ in range(5):
        db.visit([("front_table", 8, 4.0, 4.0)])
    db.finish()
    c2 = copy.deepcopy(cfg)
    c2.zone("front_table").min_engage_s = 6.0
    assert Analytics(db.st, cfg).zones(Window())[0]["engaged"] == 5
    assert Analytics(db.st, c2).zones(Window())[0]["engaged"] == 0


def test_zone_metrics_are_per_visitor_not_per_pass(cfg, db):
    db.run("a", T0)
    # one visitor walks past the table twice and stops once: still ONE impression, ONE engaged visitor
    db.visit([("front_table", 3, 0.0, 0.0), ("wall_a", 4, 0.0, 0.0), ("front_table", 7, 5.0, 5.0)])
    db.visit([("wall_a", 4, 0.0, 0.0)])
    db.finish()
    z = {r["zone_id"]: r for r in Analytics(db.st, cfg).zones(Window())}
    assert z["front_table"]["impressions"] == 1 and z["front_table"]["engaged"] == 1
    assert z["front_table"]["reach"] == pytest.approx(0.5)
    assert z["front_table"]["engaged_per_100"] == pytest.approx(50.0)
    assert z["front_table"]["median_dwell_s"] == pytest.approx(10.0)  # dwell summed over the visitor's passes
    assert z["wall_a"]["impressions"] == 2 and z["wall_a"]["engaged"] == 0


def test_window_filters_by_run(cfg, db):
    r1 = db.run("a", T0)
    db.visit([("front_table", 5, 3.0, 3.0)])
    db.finish()
    r2 = db.run("b", T0 + 7 * 86400)
    db.visit([("front_table", 5, 3.0, 3.0)])
    db.visit([("front_table", 5, 0.0, 0.0)])
    db.finish()
    an = Analytics(db.st, cfg)
    assert an.summary(Window([r1]))["visitors"] == 1 and an.summary(Window([r2]))["visitors"] == 2
    assert an.summary(Window())["visitors"] == 3
    assert an.summary(Window(t0=T0 + 86400))["visitors"] == 2


def test_flows_include_entrance_and_exit(cfg, db):
    db.run("a", T0)
    for _ in range(4):
        db.visit([("front_table", 5, 3.0, 3.0), ("wall_a", 5, 3.0, 3.0)])
    db.visit([("wall_a", 5, 3.0, 3.0)], exit_=False)
    db.finish()
    f = Analytics(db.st, cfg).flows(Window())
    edges = {(e["from"], e["to"]): e["count"] for e in f["edges"]}
    assert edges[("ENTRANCE", "front_table")] == 4
    assert edges[("front_table", "wall_a")] == 4
    assert edges[("wall_a", "EXIT")] == 4 and edges[("ENTRANCE", "wall_a")] == 1
    assert f["n_paths"] == 5


def test_timeseries_zero_fills_short_holes_and_flags_long_gaps(cfg, db):
    db.run("a", T0)
    db.visit([("front_table", 5, 3.0, 3.0)])
    db.t = T0 + 15 * 60                       # 15 min later: 2 empty 5-min buckets in between
    db.visit([("front_table", 5, 0.0, 0.0)])
    db.t = T0 + 3600 * 5                      # 5 h later: far too long to zero-fill
    db.visit([("front_table", 5, 3.0, 3.0)])
    db.finish()
    ts = Analytics(db.st, cfg).timeseries(Window())
    assert [b["entries"] for b in ts[:4]] == [1, 0, 0, 1]
    assert not any(b["gap"] for b in ts[:4]) and ts[-1]["gap"] is True
    assert len(ts) == 5  # no thousands of empty buckets between the two days


def test_heatmap_sums_seconds_inside_the_grid(cfg, db):
    db.run("a", T0)
    db.st.add_heat(db.rid, {(T0, 3, 4): 10.0, (T0 + 300, 3, 4): 5.0, (T0, 0, 0): 1.0, (T0, 999, 999): 50.0})
    db.finish()
    h = Analytics(db.st, cfg).heatmap(Window())
    assert h["grid"][4][3] == 15.0 and h["total_s"] == pytest.approx(16.0)  # out-of-grid cell ignored
    assert (h["nx"], h["ny"]) == (int(np.ceil(18 / h["cell_m"])), int(np.ceil(12 / h["cell_m"])))


# ----------------------------------------------------------------------------------------------- lift
def _populate(db, label, t0, n, rates, rng):
    db.run(label, t0)
    for _ in range(n):
        zs = []
        for z in FIX:
            if rng.random() < 0.6:                      # reached
                eng = rng.random() < rates[z]
                zs.append((z, 8.0 if eng else 3.0, 5.0 if eng else 0.0, 5.0 if eng else 0.0))
        db.visit(zs)
    db.finish()


def test_lift_detects_a_real_improvement_and_does_not_invent_others(cfg, db):
    rng = np.random.default_rng(3)
    base = {z: 0.25 for z in FIX}
    after = dict(base, endcap_b=0.50)
    a = _populate(db, "before", T0, 400, base, rng) or 1
    b = _populate(db, "after", T0 + 7 * 86400, 400, after, rng) or 2
    res = Analytics(db.st, cfg).lift(Window([1]), Window([2]), n_boot=1000)
    by = {r["zone_id"]: r for r in res["zones"]}
    assert by["endcap_b"]["adjusted"]["verdict"] == "improved"
    assert by["endcap_b"]["adjusted"]["lift_pct"] > 40
    assert by["endcap_b"]["a"]["per_100"] < by["endcap_b"]["b"]["per_100"]
    for z in ("front_table", "wall_a", "gondola_c", "back_promo"):  # untouched controls must not read as winners
        assert by[z]["verdict"] != "improved", z
    cards = generate_insights(Analytics(db.st, cfg).zones(Window([2])), None, res)
    assert any(c["id"] == "lift:endcap_b" and c["kind"] == "result" for c in cards)


def test_lift_adjusts_for_a_store_wide_change(cfg, db):
    """Everything engages 40% more in week 2 (nicer weather): the raw numbers say 'improved', the control-adjusted
    ones must not credit the endcap."""
    rng = np.random.default_rng(5)
    _populate(db, "before", T0, 600, {z: 0.20 for z in FIX}, rng)
    _populate(db, "after", T0 + 7 * 86400, 600, {z: 0.28 for z in FIX}, rng)
    res = Analytics(db.st, cfg).lift(Window([1]), Window([2]), zone_ids=["endcap_b"], n_boot=1000)
    (r,) = res["zones"]
    assert r["verdict"] == "improved"                       # raw lift is real...
    assert r["adjusted"]["verdict"] == "inconclusive"       # ...but not attributable to the endcap
    assert abs(r["adjusted"]["lift_pct"]) < 25


def test_lift_refuses_tiny_samples(cfg, db):
    rng = np.random.default_rng(1)
    _populate(db, "a", T0, 3, {z: 0.3 for z in FIX}, rng)
    _populate(db, "b", T0 + 86400, 300, {z: 0.3 for z in FIX}, rng)
    assert "error" in Analytics(db.st, cfg).lift(Window([1]), Window([2]))


# ------------------------------------------------------------------------------------------- insights
def _z(zid, kind="fixture", impressions=60, reach=0.5, rate=0.3, dwell=6.0, med_eng=6.0, engaged=18):
    return {"zone_id": zid, "name": zid, "kind": kind, "impressions": impressions, "reach": reach,
            "engagement_rate": rate if kind == "fixture" else None, "median_dwell_s": dwell,
            "median_engaged_s": med_eng if kind == "fixture" else None, "engaged": engaged if kind == "fixture" else None}


def test_seen_not_engaging_and_hidden_gem_rules():
    zones = [_z("a"), _z("b"), _z("c"), _z("busy", reach=0.8, rate=0.08), _z("hidden", reach=0.2, rate=0.6)]
    ids = {c["id"] for c in generate_insights(zones)}
    assert "seen-not-engaging:busy" in ids and "hidden-gem:hidden" in ids
    assert not any(i.endswith(":a") or i.endswith(":b") for i in ids)  # median zones get no card


def test_no_insights_from_tiny_samples():
    zones = [_z("a", impressions=5), _z("b", impressions=5), _z("c", impressions=5),
             _z("busy", impressions=5, reach=0.9, rate=0.01)]
    assert not [c for c in generate_insights(zones) if c["kind"] == "opportunity"]


def test_long_dwell_warning_is_for_areas_only():
    zones = [_z("checkout", kind="area", dwell=45.0), _z("a", dwell=45.0)]
    ids = {c["id"] for c in generate_insights(zones)}
    assert "long-dwell:checkout" in ids and "long-dwell:a" not in ids


def test_quality_reports_privacy_posture_and_calibration_warning(cfg, db):
    db.run("a", T0)
    db.finish()
    db.st.finish_run(db.rid, T0 + 10, {"calibration_rmse_cm": 25, "customers": 5, "staff": 0, "mean_detections_per_frame": 1.0})
    q = Analytics(db.st, cfg).quality()
    assert any("calibration" in w for w in q["warnings"])
    assert any("Engagement mode" in w for w in q["warnings"])
    p = q["privacy"]
    assert p["raw_frames_stored"] == 0 and p["faces_stored"] == 0 and p["trajectories_stored"] is False
    assert p["appearance_reid"] is False


def test_small_samples_are_inconclusive_and_say_how_much_data_is_needed(cfg, db):
    rng = np.random.default_rng(9)
    _populate(db, "before", T0, 40, {z: 0.20 for z in FIX}, rng)
    _populate(db, "after", T0 + 7 * 86400, 40, dict({z: 0.20 for z in FIX}, endcap_b=0.40), rng)
    res = Analytics(db.st, cfg).lift(Window([1]), Window([2]))
    assert all(z.get("adjusted", z)["verdict"] == "inconclusive" for z in res["zones"])  # 40 visitors a week: noise
    r = {z["zone_id"]: z for z in res["zones"]}["endcap_b"]
    assert r["lift_pct"] > 30 and r["visitors_needed_per_period"] > 100
    assert res["n_compared"] == 5 and res["ci_level_pct"] == 99.0


def test_needs_data_card_appears_for_a_big_but_unconfirmed_change():
    z = {"zone_id": "endcap_b", "name": "Endcap B", "a": {"visitors": 60, "per_100": 10.0}, "b": {"visitors": 60, "per_100": 18.0},
         "lift_pct": 80.0, "ci_low_pct": -20.0, "ci_high_pct": 300.0, "verdict": "inconclusive", "visitors_needed_per_period": 330}
    (card,) = [c for c in generate_insights([], None, {"zones": [z]}) if c["id"] == "needs-data:endcap_b"]
    assert card["confidence"] == "low" and "not yet conclusive" in card["title"] and "330" in card["detail"]
    z2 = dict(z, visitors_needed_per_period=50)  # already enough data: no such card
    assert not [c for c in generate_insights([], None, {"zones": [z2]}) if c["id"].startswith("needs-data")]


def test_a_big_change_to_one_display_does_not_make_its_neighbours_look_worse(cfg, db):
    """Regression: with all-other-fixtures-pooled controls, doubling Endcap B made an untouched Front Table read 'worse'."""
    rng = np.random.default_rng(11)
    base = {z: 0.25 for z in FIX}
    _populate(db, "before", T0, 500, base, rng)
    _populate(db, "after", T0 + 7 * 86400, 500, dict(base, endcap_b=0.55), rng)
    res = Analytics(db.st, cfg).lift(Window([1]), Window([2]))
    by = {z["zone_id"]: z for z in res["zones"]}
    assert by["endcap_b"]["adjusted"]["verdict"] == "improved"
    for zid in ("front_table", "wall_a", "gondola_c", "back_promo"):
        assert by[zid]["adjusted"]["verdict"] == "inconclusive", zid


def test_no_change_anywhere_rarely_flags_a_winner_or_loser():
    """Five displays, no real change: the chance that ANY display gets a verdict should stay near 5%, not 23%."""
    from storelens.queries import lift_stats
    rng = np.random.default_rng(2)
    ids = FIX
    flagged = 0
    runs = 40
    for _ in range(runs):
        p = np.array([0.30, 0.18, 0.2, 0.12, 0.15])
        ea = (rng.random((300, 5)) < p).astype(np.int8)
        eb = (rng.random((300, 5)) < p).astype(np.int8)
        res = lift_stats(ea, eb, ea, eb, ids, ids, {i: i for i in ids}, n_boot=1000, seed=int(rng.integers(1e6)))
        flagged += any(z.get("adjusted", z)["verdict"] != "inconclusive" for z in res["zones"])
    assert flagged <= 5, f"{flagged}/{runs} no-change comparisons produced a verdict"


def test_visitors_needed_matches_textbook_two_proportion_sample_size():
    from storelens.queries import _visitors_needed
    assert 300 <= _visitors_needed(0.104, 0.181) <= 340   # worked example: ~320 per arm
    assert _visitors_needed(0.1, 0.1) is None and _visitors_needed(0.0, 0.2) is None
