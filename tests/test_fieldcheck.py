"""Field validation harness: known hand tallies against a store built so every expected number is known."""
import json

import numpy as np
import pytest
import yaml

from glance.fieldcheck import (DEFAULT_GATES, check_empty, gates_digest, judge, load_spec, parse_time, read_rows,
                                  run_fieldcheck, worst, write_templates)
from glance.queries import Analytics, Window
from glance.store import EventStore

from .test_queries import T0, Builder

TZ = "America/Los_Angeles"


def _csv(path, header, rows, comment="# note, with a comma"):
    path.write_text(comment + "\n" + header + "\n" + "\n".join(",".join(str(c) for c in r) for r in rows) + "\n")
    return path.name


def _door_store(tmp_path, per_window, n_windows=40, hours=8, win=600.0):
    """Entries placed inside each 10-minute window; per_window(k) = how many the pipeline sees."""
    b = Builder(tmp_path / "d.db")
    rid = b.run("day", T0)
    for k in range(n_windows):
        for j in range(per_window(k)):
            b.t = T0 + k * win + 20 + j * 7
            b.visit(dt=0)
    b.st.finish_run(rid, T0 + hours * 3600, {})
    b.st.commit()
    return b.st, rid


def _spec(tmp_path, **kw):
    spec = {"gates": {}, **kw}
    (tmp_path / "fieldcheck.yaml").write_text(yaml.safe_dump({k: v for k, v in spec.items()}))
    return load_spec(tmp_path / "fieldcheck.yaml")


def _check(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


# ----------------------------------------------------------------------------------------------- helpers
def test_times_accept_epoch_and_iso_in_the_store_timezone():
    assert parse_time("1800000000", TZ) == 1800000000.0
    assert parse_time("2026-11-02T10:00", TZ) == parse_time("2026-11-02T10:00:00-08:00", TZ)


def test_csv_reader_skips_comments_and_blank_lines(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("# why, how, when\nstart , COUNT_IN\n\n# another\n1,2\n3,4\n")
    assert read_rows(p) == [{"start": "1", "count_in": "2"}, {"start": "3", "count_in": "4"}]


def test_judge_distinguishes_pass_uncertain_fail_insufficient():
    assert judge(1.0, (0.5, 2.0), 3.0, 40, 30) == "PASS"
    assert judge(1.0, (-1.0, 4.0), 3.0, 40, 30) == "UNCERTAIN"
    assert judge(4.0, (3.5, 4.5), 3.0, 40, 30) == "FAIL"
    assert judge(0.0, (0.0, 0.0), 3.0, 10, 30) == "INSUFFICIENT"
    assert judge(float("nan"), None, 3.0, 99, 30) == "INSUFFICIENT"
    assert worst(["PASS", "UNCERTAIN", "FAIL"]) == "FAIL" and worst([]) == "INSUFFICIENT"


def test_gate_digest_changes_with_any_threshold():
    g = dict(DEFAULT_GATES)
    assert gates_digest(g) == gates_digest(dict(g))
    g["door_bias_max_pct"] = 4.0
    assert gates_digest(g) != gates_digest(DEFAULT_GATES)


def test_templates_are_written_once_and_never_overwrite_labels(tmp_path):
    first = write_templates(tmp_path)
    (tmp_path / "door_tally.csv").write_text("my real labels")
    assert write_templates(tmp_path) == []
    assert (tmp_path / "door_tally.csv").read_text() == "my real labels" and "fieldcheck.yaml" in first


# ----------------------------------------------------------------------------------------------- door count
def test_perfect_door_tally_passes_with_zero_bias(cfg, tmp_path):
    st, rid = _door_store(tmp_path, lambda k: 3 + k % 3)
    rows = [(T0 + k * 600, T0 + (k + 1) * 600, 3 + k % 3) for k in range(40)]
    spec = _spec(tmp_path, runs=[rid], door_tally=_csv(tmp_path / "door.csv", "start,end,count_in", rows))
    r = _check(run_fieldcheck(cfg, st, spec), "door_count")
    assert r["status"] == "PASS" and r["bias_pct"] == 0 and r["window_error_pct"] == 0 and r["n"] == 40


def test_an_undercounting_pipeline_fails_and_the_sign_is_reported(cfg, tmp_path):
    st, rid = _door_store(tmp_path, lambda k: 8)          # pipeline sees 8 per window ...
    rows = [(T0 + k * 600, T0 + (k + 1) * 600, 10) for k in range(40)]   # ... a person at the door counted 10
    r = _check(run_fieldcheck(cfg, st, _spec(tmp_path, runs=[rid], door_tally=_csv(tmp_path / "d.csv", "start,end,count_in", rows))), "door_count")
    assert r["status"] == "FAIL" and r["bias_pct"] == pytest.approx(-20.0) and r["bias_ci_pct"][1] < -15


def test_too_few_windows_is_insufficient_not_pass(cfg, tmp_path):
    st, rid = _door_store(tmp_path, lambda k: 5, n_windows=10)
    rows = [(T0 + k * 600, T0 + (k + 1) * 600, 5) for k in range(10)]
    r = _check(run_fieldcheck(cfg, st, _spec(tmp_path, runs=[rid], door_tally=_csv(tmp_path / "d.csv", "start,end,count_in", rows))), "door_count")
    assert r["status"] == "INSUFFICIENT" and r["n"] == 10


def test_windows_outside_the_processed_footage_are_dropped_and_counted(cfg, tmp_path):
    st, rid = _door_store(tmp_path, lambda k: 4, n_windows=5, hours=1)
    rows = [(T0 + k * 600, T0 + (k + 1) * 600, 4) for k in range(5)] + [(T0 + 99 * 3600, T0 + 99 * 3600 + 600, 7)]
    r = _check(run_fieldcheck(cfg, st, _spec(tmp_path, runs=[rid], door_tally=_csv(tmp_path / "d.csv", "start,end,count_in", rows))), "door_count")
    assert r["n"] == 5 and r["skipped_outside_footage"] == 1


def test_bootstrap_is_reproducible(cfg, tmp_path):
    st, rid = _door_store(tmp_path, lambda k: 3 + k % 4)
    rows = [(T0 + k * 600, T0 + (k + 1) * 600, 3 + (k * 7) % 5) for k in range(40)]
    spec = _spec(tmp_path, runs=[rid], door_tally=_csv(tmp_path / "d.csv", "start,end,count_in", rows))
    a, b = (_check(run_fieldcheck(cfg, st, spec, seed=5), "door_count") for _ in range(2))
    assert a["bias_ci_pct"] == b["bias_ci_pct"]


# ----------------------------------------------------------------------------------------------- crowd
def _crowd_store(tmp_path, seen=3):
    b = Builder(tmp_path / "c.db")
    rid = b.run("busy", T0)
    for s in range(40):                                   # 40 snapshots, `seen` customers in view at each
        for j in range(seen):
            b.t = T0 + s * 300 + 10 * j
            b.visit(dt=0)
    b.st.finish_run(rid, T0 + 40 * 300 + 120, {})
    b.st.commit()
    return b.st, rid


def test_crowd_undercount_is_found_per_level(cfg, tmp_path):
    st, rid = _crowd_store(tmp_path, seen=2)
    rows = [(T0 + s * 300 + 40, 3) for s in range(40)]   # a human sees 3 where the pipeline holds 2
    r = _check(run_fieldcheck(cfg, st, _spec(tmp_path, runs=[rid], crowd=_csv(tmp_path / "c.csv", "t,people", rows))), "crowd_counts")
    lv = next(l for l in r["levels"] if l["level"] == "1-3")
    assert lv["direction"] == "undercounts" and lv["bias_pct"] == pytest.approx(-100 / 3, abs=0.1) and lv["status"] == "FAIL"
    assert next(l for l in r["levels"] if l["level"] == "9+")["status"] == "INSUFFICIENT"
    assert r["status"] == "FAIL"


def test_a_view_with_nobody_in_it_reports_false_people(cfg, tmp_path):
    st, rid = _crowd_store(tmp_path, seen=2)
    rows = [(T0 + s * 300 + 40, 0) for s in range(5)]
    r = _check(run_fieldcheck(cfg, st, _spec(tmp_path, runs=[rid], crowd=_csv(tmp_path / "c.csv", "t,people", rows))), "crowd_counts")
    assert r["empty_view_snapshots"] == 5 and r["empty_view_false_people"] == 10


# ----------------------------------------------------------------------------------------------- stops
def _stops_store(tmp_path):
    b = Builder(tmp_path / "s.db")
    rid = b.run("stops", T0)
    truth = []
    zones = ["front_table", "wall_a", "gondola_c"]
    for i in range(30):
        z = zones[i % 3]
        b.t = T0 + i * 60
        b.visit([(z, 15.0, 12.0, 0.0)], dt=0)
        truth.append((T0 + i * 60 + 5, T0 + i * 60 + 20, z))
    b.st.finish_run(rid, T0 + 31 * 60, {})
    b.st.commit()
    return b.st, rid, truth


def test_every_labelled_stop_is_found_when_labels_match(cfg, tmp_path):
    st, rid, truth = _stops_store(tmp_path)
    spec = _spec(tmp_path, runs=[rid], stops=_csv(tmp_path / "s.csv", "t_enter,t_exit,zone", truth), stops_exhaustive=True,
                 gates={"stops_min": 10})
    r = _check(run_fieldcheck(cfg, st, spec), "zone_stops")
    assert r["matched"] == 30 and r["missed"] == 0 and r["wrong_zone"] == 0 and r["precision"] == 1.0
    assert r["status"] in ("PASS", "UNCERTAIN")


def test_a_stop_at_the_wrong_display_is_told_apart_from_a_missed_one_only_when_labels_are_exhaustive(cfg, tmp_path):
    st, rid, truth = _stops_store(tmp_path)
    t0, t1, z = truth[0]
    labels = truth[1:] + [(t0, t1, "wall_a" if z != "wall_a" else "gondola_c")]   # one label names the wrong display
    labels += [(T0 + 900, T0 + 920, "endcap_b")]                                   # one stop nothing recorded anywhere
    name = _csv(tmp_path / "s.csv", "t_enter,t_exit,zone", labels)
    ex = _check(run_fieldcheck(cfg, st, _spec(tmp_path, runs=[rid], stops=name, stops_exhaustive=True, gates={"stops_min": 10})), "zone_stops")
    assert ex["wrong_zone"] == 1 and ex["missed"] == 1
    loose = _check(run_fieldcheck(cfg, st, _spec(tmp_path, runs=[rid], stops=name, stops_exhaustive=False, gates={"stops_min": 10})), "zone_stops")
    assert loose["wrong_zone"] is None and loose["missed"] == 2


# ----------------------------------------------------------------------------------------------- empty store
def _empty_store(tmp_path, hours, false_visits=0):
    b = Builder(tmp_path / "e.db")
    rid = b.run("night", T0)
    for i in range(false_visits):
        b.t = T0 + 100 * i
        b.visit([("front_table", 5.0, 0.0, 0.0)], dt=0)
    b.st.finish_run(rid, T0 + hours * 3600, {})
    b.st.commit()
    return b.st, rid


def test_empty_store_needs_enough_hours_to_prove_anything(tmp_path):
    g = dict(DEFAULT_GATES)
    short = tmp_path / "short"
    short.mkdir()
    st, rid = _empty_store(short, hours=2)
    assert check_empty(st, [rid], g)["status"] == "INSUFFICIENT"
    nine = tmp_path / "nine"
    nine.mkdir()
    st, rid = _empty_store(nine, hours=9)
    r = check_empty(st, [rid], g)
    assert r["false_visits"] == 0 and r["status"] == "UNCERTAIN"      # zero events in 9 h: bound is 3.69/9 = 0.41/h > 0.25
    assert r["false_per_hour_upper95"] == pytest.approx(3.69 / 9, rel=0.02)


def test_enough_quiet_hours_pass_and_false_alarms_fail(tmp_path):
    g = dict(DEFAULT_GATES)
    quiet = tmp_path / "quiet"
    quiet.mkdir()
    st, rid = _empty_store(quiet, hours=16)
    assert check_empty(st, [rid], g)["status"] == "PASS"
    d2 = tmp_path / "noisy"
    d2.mkdir()
    st2, rid2 = _empty_store(d2, hours=10, false_visits=6)
    r = check_empty(st2, [rid2], g)
    assert r["status"] == "FAIL" and r["false_zone_visits"] == 6 and r["false_zone_visits_by_zone"] == {"front_table": 6}


# ----------------------------------------------------------------------------------------------- whole run
def test_overall_is_the_worst_check_and_the_spec_runs_end_to_end(cfg, tmp_path):
    st, rid = _door_store(tmp_path, lambda k: 3 + k % 3)
    good = [(T0 + k * 600, T0 + (k + 1) * 600, 3 + k % 3) for k in range(40)]
    spec = _spec(tmp_path, runs=[rid], door_tally=_csv(tmp_path / "d.csv", "start,end,count_in", good))
    r = run_fieldcheck(cfg, st, spec)
    assert r["overall"] == "PASS" and r["gates_sha"] == gates_digest(spec["gates"])
    assert json.loads(json.dumps(r, default=float))["checks"][0]["name"] == "door_count"
    bad = _spec(tmp_path, runs=[rid], door_tally=good and _csv(tmp_path / "d2.csv", "start,end,count_in", [(a, b, c + 5) for a, b, c in good]))
    assert run_fieldcheck(cfg, st, bad)["overall"] == "FAIL"


def test_demographic_checks_only_run_when_demographics_are_enabled(cfg, tmp_path):
    st, rid = _door_store(tmp_path, lambda k: 3)
    labels = _csv(tmp_path / "l.csv", "start,end,age_band,gender,count", [(T0, T0 + 3600, "25-44", "female", 10)])
    off = run_fieldcheck(cfg.model_copy(update={"demographics": cfg.demographics.model_copy(update={"enabled": False})}), st,
                         _spec(tmp_path, runs=[rid], demo_labels=labels))
    assert not [c for c in off["checks"] if c["name"].startswith("audience")]
