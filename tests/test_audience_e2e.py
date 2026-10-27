"""Demographics end to end on a short synthetic clip: simulated attribute model -> pipeline -> aggregate table -> audit."""
import pytest

from storelens.attributes import SimulatedEstimator
from storelens.audit import attack_with_truth, file_scan, run_audit, schema_scan, store_exposure, time_check
from storelens.pipeline import Pipeline
from storelens.queries import Analytics, Window
from storelens.sim.generate import Period, generate_period
from storelens.sim.layout import build_store_config, default_layout
from storelens.store import EventStore
from storelens.validate import load_gt, validate

pytestmark = pytest.mark.slow
START = 1_800_000_000.0


@pytest.fixture(scope="module")
def aud(tmp_path_factory):
    d = tmp_path_factory.mktemp("aud")
    layout = default_layout()
    cfg = build_store_config(layout, seed=21)
    video, gt, _ = generate_period(layout, Period("clip", minutes=4.0, seed=21, base_per_min=7.0), d, verbose=False)
    st = EventStore(d / "a.db")
    est = SimulatedEstimator.from_gt(gt, cfg.demographics.age_bands, seed=3)
    meta = Pipeline(cfg, st, label="clip", attributes=est).run(video, START, progress=False)
    return {"dir": d, "cfg": cfg, "st": st, "est": est, "meta": meta, "gt": gt}


def test_every_tracked_customer_is_counted_once_and_no_state_is_left(aud):
    an = Analytics(aud["st"], aud["cfg"])
    visitors = an.summary(Window())["visitors"]
    stored = aud["st"].query("SELECT SUM(n) FROM audience WHERE metric='visitor'")[0][0]
    assert visitors > 10 and stored == visitors
    assert aud["est"].pending() == 0, "no per-person state survives the run"
    assert aud["meta"]["demographics"]["enabled"] and aud["meta"]["demographics"]["simulated"]


def test_stored_audience_has_no_visit_key_and_no_finer_time_than_the_bucket(aud):
    cols = [r[1] for r in aud["st"].query("PRAGMA table_info(audience)")]
    assert "visit_key" not in cols
    assert time_check(aud["st"], aud["cfg"])["status"] == "PASS"
    assert schema_scan(aud["st"])["status"] == "PASS"
    assert file_scan(aud["dir"])["status"] in ("PASS", "WARN")  # the simulator's own video is the only WARN


def test_audience_numbers_track_the_truth_within_small_sample_limits(aud):
    rep = validate(aud["cfg"], aud["st"], aud["meta"]["run_id"], load_gt(aud["gt"]), aud["est"])["audience"]
    assert rep["visitors_counted"] > 10
    assert rep["unknown_age_share"] < 0.5 and rep["age_tvd"] < 0.3
    e = rep["estimator"]
    assert e["age_within_one_band"] > 0.8 and e["gender_correct"] > 0.8


def test_the_dashboard_view_never_shows_a_group_below_k(aud):
    out = Analytics(aud["st"], aud["cfg"]).audience(Window())
    assert out["enabled"] and out["simulated"]
    if out.get("has_data"):
        k = out["min_cell"]
        shown = list(out["store"]["by_age"]) + list(out["store"]["by_gender"] or [])
        if out["store"]["matrix"]:
            shown += [{"n": c} for row in out["store"]["matrix"]["n"] for c in row]
        for z in out["zones"]:
            for dim in ("age", "gender"):
                shown += list(z[dim] or [])
        assert shown, "the fixture should have something to show, otherwise this test checks nothing"
        for g in shown:
            assert g["n"] >= k, f"a group of {g['n']} people (< k={k}) reached the dashboard payload: {g}"


def test_linkage_attack_and_exposure_run_on_the_stored_counts(aud):
    ex = store_exposure(aud["st"], aud["cfg"])
    assert ex["metrics"]["visitor"]["people"] > 10
    att = attack_with_truth(aud["cfg"], aud["st"], aud["meta"]["run_id"], load_gt(aud["gt"]))
    assert att["n"] > 10 and 0 <= att["age"]["attack"] <= 1 and att["time_leak_testable"] is False
    assert att["status"] == "WARN", "one time bucket cannot show what the time resolution leaks: say so, do not pass it"


def test_run_audit_end_to_end(aud):
    aud["st"].commit()
    r = run_audit(aud["cfg"], aud["dir"] / "a.db", data_dir=aud["dir"], gt=load_gt(aud["gt"]), run_id=aud["meta"]["run_id"],
                  coarsen_days=1, now=START + 10 * 86400)
    assert r["overall"] in ("PASS", "WARN") and r["what_if"]["exposure_after"]["metrics"]
