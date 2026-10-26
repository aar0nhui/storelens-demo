"""Privacy audit: structural checks, small-cell exposure, and the effect of coarsening, on hand-built data."""
import shutil
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from glance.attributes import UNKNOWN, bucket_start
from glance.audit import (coarsen_audience, file_scan, run_audit, schema_scan, store_exposure, time_check, vacuum)
from glance.store import EventStore

LA = ZoneInfo("America/Los_Angeles")
DAY = datetime(2026, 11, 2, 0, 0, tzinfo=LA).timestamp()


def _store(tmp_path):
    return EventStore(tmp_path / "a.db")


def _aud(st, rows):
    st.add_audience(1, {k: v for k, v in rows})
    st.commit()


def _on(cfg):
    c = cfg.model_copy(deep=True)
    c.demographics.enabled = True
    return c


# ----------------------------------------------------------------------------------------------- structure
def test_clean_database_passes_the_schema_scan(tmp_path):
    assert schema_scan(_store(tmp_path))["status"] == "PASS"


def test_extra_table_blob_column_or_visit_key_on_audience_fail(tmp_path):
    st = _store(tmp_path)
    st.conn.execute("CREATE TABLE face_crops (id INTEGER, jpeg BLOB)")
    r = schema_scan(st)
    assert r["status"] == "FAIL" and any("face_crops" in p for p in r["problems"])
    st2 = EventStore(tmp_path / "b.db")
    st2.conn.execute("ALTER TABLE audience ADD COLUMN visit_key TEXT")
    r2 = schema_scan(st2)
    assert r2["status"] == "FAIL" and any("visit key" in p for p in r2["problems"])


def test_a_blob_value_hiding_in_a_text_column_is_found(tmp_path):
    st = _store(tmp_path)
    st.begin_run("x", "cam1", "x.mp4", 1.0)
    st.conn.execute("UPDATE runs SET meta = ?", (sqlite3.Binary(b"\x89PNG\r\n"),))
    st.commit()
    assert any("BLOB values" in p for p in schema_scan(st)["problems"])


def test_file_scan_flags_images_embeddings_and_vector_dumps_but_only_warns_about_video(tmp_path):
    (tmp_path / "ok.txt").write_text("hello 1.5 2.5")
    assert file_scan(tmp_path)["status"] == "PASS"
    (tmp_path / "clip.mp4").write_bytes(b"x")
    assert file_scan(tmp_path)["status"] == "WARN"
    (tmp_path / "crop.JPG").write_bytes(b"x")
    r = file_scan(tmp_path)
    assert r["status"] == "FAIL" and r["images"] == ["crop.JPG"]
    (tmp_path / "crop.JPG").unlink()
    (tmp_path / "faces.npy").write_bytes(b"x")
    assert file_scan(tmp_path)["embeddings"] == ["faces.npy"]
    (tmp_path / "faces.npy").unlink()
    (tmp_path / "debug.log").write_text("feat " + ", ".join(f"{x:.4f}" for x in np.linspace(0.1, 0.9, 80)))
    assert file_scan(tmp_path)["numeric_vectors"] == ["debug.log"]


def test_timestamps_must_sit_on_bucket_boundaries(cfg, tmp_path):
    c = _on(cfg)
    st = _store(tmp_path)
    _aud(st, [((DAY + 6 * 3600, "", "visitor", "25-44", "female"), 12)])
    assert time_check(st, c)["status"] == "PASS"
    _aud(st, [((DAY + 6 * 3600 + 17, "", "visitor", "25-44", "female"), 1)])
    assert time_check(st, c)["status"] == "FAIL"


# ----------------------------------------------------------------------------------------------- exposure
def test_a_tiny_cell_with_one_kind_of_person_is_reported_as_fully_exposed(cfg, tmp_path):
    st = _store(tmp_path)
    rows = [((DAY, "", "visitor", "25-44", "female"), 60), ((DAY, "", "visitor", "45-64", "male"), 40),
            ((DAY, "front_table", "engaged", "13-24", "female"), 4)]
    _aud(st, rows)
    ex = store_exposure(st, _on(cfg), k=10)
    e = ex["metrics"]["engaged"]
    assert e["cells_below_k"] == 1 and e["people_in_cells_below_k"] == 4 and e["age_expected_accuracy"] == 1.0
    v = ex["metrics"]["visitor"]
    assert v["cells_below_k"] == 0 and v["age_expected_accuracy"] == pytest.approx(0.6)


def test_people_with_nothing_known_do_not_count_as_exposed(cfg, tmp_path):
    st = _store(tmp_path)
    _aud(st, [((DAY, "front_table", "engaged", UNKNOWN, UNKNOWN), 3)])
    assert store_exposure(st, _on(cfg), k=10)["metrics"]["engaged"]["people_in_cells_below_k"] == 0


# ----------------------------------------------------------------------------------------------- mitigation
def _three_buckets(st):
    rows = []
    for h, (a, g, n) in enumerate([("25-44", "female", 3), ("25-44", "female", 2), ("45-64", "female", 4)]):
        rows.append(((DAY + 6 * 3600 * h, "front_table", "engaged", a, g), n))
        rows.append(((DAY + 6 * 3600 * h, "", "visitor", a, g), 50))
    _aud(st, rows)


def test_coarsening_merges_old_buckets_into_days_and_keeps_every_person(cfg, tmp_path):
    st = _store(tmp_path)
    _three_buckets(st)
    total = st.query("SELECT SUM(n) FROM audience")[0][0]
    now = DAY + 30 * 86400
    stats = coarsen_audience(st, _on(cfg), older_than_days=7, now=now, to_hours=24)
    assert st.query("SELECT SUM(n) FROM audience")[0][0] == total
    assert {r[0] for r in st.query("SELECT DISTINCT bucket_ts FROM audience")} == {DAY}
    assert stats["rows_after"] < stats["rows_before"]


def test_suppression_folds_small_cells_into_unknown_without_losing_people(cfg, tmp_path):
    st = _store(tmp_path)
    _three_buckets(st)
    coarsen_audience(st, _on(cfg), older_than_days=7, now=DAY + 30 * 86400, to_hours=24, suppress_below=10)
    engaged = st.query("SELECT age_band, gender, n FROM audience WHERE metric='engaged'")
    assert engaged == [(UNKNOWN, UNKNOWN, 9)], "9 engaged people in one day < k=10: nothing about them stays readable"
    visitor = st.query("SELECT SUM(n) FROM audience WHERE metric='visitor'")[0][0]
    assert visitor == 150 and st.query("SELECT COUNT(*) FROM audience WHERE metric='visitor' AND age_band!=?", (UNKNOWN,))[0][0] == 2


def test_recent_rows_are_left_alone(cfg, tmp_path):
    st = _store(tmp_path)
    _three_buckets(st)
    before = sorted(st.query("SELECT * FROM audience"))
    coarsen_audience(st, _on(cfg), older_than_days=7, now=DAY + 86400, to_hours=24)
    assert sorted(st.query("SELECT * FROM audience")) == before


def test_vacuum_runs_on_a_database_with_pending_deletes(cfg, tmp_path):
    st = _store(tmp_path)
    _three_buckets(st)
    coarsen_audience(st, _on(cfg), older_than_days=7, now=DAY + 30 * 86400)
    vacuum(st)
    assert st.query("PRAGMA freelist_count")[0][0] == 0


def test_run_audit_what_if_does_not_touch_the_real_database(cfg, tmp_path):
    st = _store(tmp_path)
    _three_buckets(st)
    st.close()
    before = EventStore(tmp_path / "a.db").query("SELECT * FROM audience ORDER BY 1,2,3,4,5")
    r = run_audit(_on(cfg), tmp_path / "a.db", data_dir=tmp_path, coarsen_days=7, now=DAY + 30 * 86400)
    assert r["what_if"]["rows_after"] <= r["what_if"]["rows_before"] and r["overall"] in ("PASS", "WARN")
    assert EventStore(tmp_path / "a.db").query("SELECT * FROM audience ORDER BY 1,2,3,4,5") == before
    assert not list(tmp_path.glob("glance-audit-*"))
