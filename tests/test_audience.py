"""Audience statistics, the attribute estimator contract, and the aggregate counter."""
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from glance.attributes import (UNKNOWN, AudienceCounter, Estimate, NullEstimator, SimulatedEstimator,
                                  bucket_start, make_estimator)
from glance.audience import band_label, index_ci, merge_groups, wilson

BANDS = ["<13", "13-24", "25-44", "45-64", "65+"]
LA = ZoneInfo("America/Los_Angeles")


# ----------------------------------------------------------------------------------------------- statistics
def test_wilson_interval_behaves():
    assert wilson(0, 0) == (0.0, 1.0)
    lo, hi = wilson(50, 100)
    assert lo < 0.5 < hi and 0.40 < lo and hi < 0.60
    assert wilson(50, 1000)[1] - wilson(50, 1000)[0] < wilson(5, 100)[1] - wilson(5, 100)[0]
    assert wilson(0, 30)[0] == 0.0 and wilson(30, 30)[1] == 1.0


def test_small_age_groups_are_merged_until_everyone_has_at_least_k():
    counts = {"<13": 2, "13-24": 30, "25-44": 40, "45-64": 3, "65+": 4}
    groups = merge_groups(counts, BANDS, 10)
    assert sum(groups, []) == BANDS, "groups are contiguous and cover every band exactly once"
    assert all(sum(counts[b] for b in g) >= 10 for g in groups)
    assert groups == [["<13", "13-24"], ["25-44", "45-64", "65+"]]


def test_too_few_people_in_total_gives_no_groups_at_all():
    assert merge_groups({"<13": 1, "13-24": 2, "25-44": 3}, BANDS, 10) == []


def test_enough_people_keeps_every_band_separate():
    assert merge_groups({b: 25 for b in BANDS}, BANDS, 10) == [[b] for b in BANDS]


def test_band_labels_for_merged_groups():
    assert band_label(["25-44"]) == "25-44"
    assert band_label(["<13", "13-24"]) == "<25"
    assert band_label(["45-64", "65+"]) == "45+"
    assert band_label(BANDS) == "all ages"


def test_index_over_under_and_in_line():
    over = index_ci(60, 100, 300, 1000)     # 60% of engagers vs 30% of visitors
    assert over["verdict"] == "over" and over["index"] == pytest.approx(2.0) and over["ci"][0] > 1
    assert index_ci(30, 100, 300, 1000)["verdict"] == "in line"
    assert index_ci(5, 100, 300, 1000)["verdict"] == "under"
    assert index_ci(0, 100, 300, 1000)["verdict"] == "n/a"
    assert index_ci(10, 100, 0, 1000)["verdict"] == "n/a"


def test_a_thin_sample_gives_a_wide_interval():
    thin, thick = index_ci(6, 10, 30, 100), index_ci(600, 1000, 3000, 10000)
    assert thin["ci"][1] / thin["ci"][0] > 2 * thick["ci"][1] / thick["ci"][0]


# ----------------------------------------------------------------------------------------------- estimator contract
def _people(n=300, age="25-44", gender="female"):
    return [{"id": i, "role": "customer", "start_frame": 0, "x": [3.0 + 0.02 * i] * 200, "y": [4.0] * 200,
             "age_band": age, "gender": gender} for i in range(n)]


def _run(est, uid, pid, frames=40):
    for f in range(frames):
        est.observe(uid, f, f / 10, None, np.zeros(4), (3.0 + 0.02 * pid, 4.0))
    return est.finalize(uid)


def test_estimate_carries_only_a_band_and_a_gender():
    assert set(Estimate.__dataclass_fields__) == {"age_band", "gender"}


def test_estimator_forgets_a_person_after_finalize_and_discard():
    est = SimulatedEstimator(_people(5), BANDS, 10.0, seed=1)
    for i in range(5):
        for f in range(12):
            est.observe(i, f, f / 10, None, np.zeros(4), (3.0 + 0.02 * i, 4.0))
    assert est.pending() == 5
    est.finalize(0)
    est.discard(1)
    assert est.pending() == 3
    for i in (2, 3, 4):
        est.finalize(i)
    assert est.pending() == 0 and est._state == {}


def test_simulated_accuracy_is_in_the_advertised_range_and_unknown_is_allowed():
    est = SimulatedEstimator(_people(400), BANDS, 10.0, seed=2)
    out = [_run(est, i, i) for i in range(400)]
    got = [e for e in out if e is not None]
    assert len(got) > 300
    known = [e for e in got if e.age_band != UNKNOWN]
    exact = np.mean([e.age_band == "25-44" for e in known])
    gen = np.mean([e.gender == "female" for e in got if e.gender != UNKNOWN])
    assert 0.65 < exact < 0.95 and 0.85 < gen < 1.0
    assert any(e.age_band == UNKNOWN or e.gender == UNKNOWN for e in got), "unknown is a real outcome, not forced to a guess"


def test_poor_camera_quality_is_worse_than_good():
    good = SimulatedEstimator(_people(300), BANDS, 10.0, seed=3, quality="good")
    poor = SimulatedEstimator(_people(300), BANDS, 10.0, seed=3, quality="poor")
    acc = lambda est: np.mean([(_run(est, i, i) or Estimate("", "")).age_band == "25-44" for i in range(300)])  # noqa: E731
    assert acc(poor) < acc(good) - 0.1


def test_a_track_next_to_a_staff_member_gets_no_customer_attributes():
    ppl = [{"id": 1, "role": "customer", "start_frame": 0, "x": [3.4] * 200, "y": [4.0] * 200,
            "age_band": "25-44", "gender": "female"},
           {"id": 99, "role": "staff", "start_frame": 0, "x": [3.0] * 200, "y": [4.0] * 200,
            "age_band": "25-44", "gender": "male"}]
    est = SimulatedEstimator(ppl, BANDS, 10.0, seed=1)
    for f in range(30):
        est.observe(7, f, f / 10, None, np.zeros(4), (3.1, 4.0))  # nearer the staff member (0.1 m) than the customer (0.3 m)
    assert est.finalize(7) is None


def test_no_model_is_bundled_so_production_stays_off(cfg):
    assert isinstance(make_estimator(cfg), NullEstimator)
    on = cfg.model_copy(deep=True)
    on.demographics.enabled = True
    assert isinstance(make_estimator(on), NullEstimator), "enabled but no model and no ground truth: still off"
    nul = NullEstimator()
    assert nul.finalize(1) is None and nul.pending() == 0


# ----------------------------------------------------------------------------------------------- aggregation
def test_buckets_align_to_local_midnight_even_across_a_dst_change():
    t = datetime(2026, 11, 1, 8, 30, tzinfo=LA).timestamp()           # the day the clocks go back (25 hours)
    b = bucket_start(t, 6, LA)
    assert datetime.fromtimestamp(b, LA) == datetime(2026, 11, 1, 6, 0, tzinfo=LA)
    assert bucket_start(b, 6, LA) == b


def test_counter_reconciles_and_never_stores_anything_finer_than_the_bucket(cfg):
    on = cfg.model_copy(deep=True)
    on.demographics.enabled = True
    c = AudienceCounter(on)
    t0 = datetime(2026, 11, 2, 10, 17, 33, tzinfo=LA).timestamp()
    for i in range(7):
        est = Estimate("25-44", "female") if i % 2 else (None if i == 0 else Estimate(UNKNOWN, "male"))
        c.add(est, t0 + 37 * i, True, [("front_table", True, i % 3 == 0)])
    visitors = sum(n for (b, z, m, a, g), n in c.counts.items() if m == "visitor")
    assert visitors == 7 == c.stats["people"]
    assert c.counts[(bucket_start(t0, 6, LA), "", "visitor", UNKNOWN, UNKNOWN)] >= 1, "a missing estimate is counted as unknown"
    assert {k[0] for k in c.counts} == {bucket_start(t0, 6, LA)}
    assert all(set(k) and len(k) == 5 for k in c.counts), "keys are (bucket, zone, metric, age, gender): no visit id, no time"
