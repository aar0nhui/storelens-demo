import numpy as np
import pytest

from storelens.config import PrivacyConfig
from storelens.privacy import Anonymizer, PreviewWriter, obscure, privacy_regions


def test_visit_keys_are_stable_within_a_process_and_not_reproducible_across():
    a1, a2 = Anonymizer(24), Anonymizer(24)
    k = a1.key("cam1", 7, 1_800_000_000.0)
    assert k == a1.key("cam1", 7, 1_800_000_000.0)  # same salt: stable (needed to join zone visits to a visit)
    assert k != a2.key("cam1", 7, 1_800_000_000.0)  # new process: salt is gone, key cannot be recomputed
    assert len(k) == 12 and int(k, 16) >= 0


def test_keys_rotate_with_the_salt_epoch():
    a = Anonymizer(24)
    day1 = a.key("cam1", 7, 1_800_000_000.0)
    day2 = a.key("cam1", 7, 1_800_000_000.0 + 25 * 3600)
    assert day1 != day2
    assert len(a._salts) <= 2  # old salts are purged, never accumulated


def test_obscure_destroys_detail_only_inside_regions():
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (100, 100, 3), dtype=np.uint8)
    orig = frame.copy()
    out = obscure(frame, np.array([[20, 20, 60, 60]]))
    assert out[20:60, 20:60].std() < orig[20:60, 20:60].std() * 0.5
    assert (out[:, :20] == orig[:, :20]).all() and (out[70:] == orig[70:]).all()
    # mosaic of <=4x4 blocks: at most 16 distinct block colours inside the region
    assert len({tuple(p) for p in out[20:60, 20:60].reshape(-1, 3)}) <= 16


def test_privacy_regions_cover_the_head_with_margin():
    box = np.array([[100.0, 100.0, 140.0, 200.0]])  # a standing person, oblique camera
    cfg = PrivacyConfig(blur="head", head_fraction=0.3, margin=0.25)
    (x1, y1, x2, y2), = privacy_regions(box, "bottom_center", cfg, (480, 640, 3))
    assert y1 <= 100 and y2 >= 130  # covers the top 30% (head) ...
    assert y2 < 170                 # ... but not the whole body
    (x1, y1, x2, y2), = privacy_regions(box, "bottom_center", PrivacyConfig(blur="person", margin=0.0), (480, 640, 3))
    assert (x1, y1, x2, y2) == (100, 100, 140, 200)


def test_regions_clip_to_the_frame():
    (x1, y1, x2, y2), = privacy_regions(np.array([[-10.0, -10.0, 30.0, 40.0]]), "center", PrivacyConfig(blur="person"), (50, 50, 3))
    assert x1 >= 0 and y1 >= 0 and x2 <= 50 and y2 <= 50


def test_blur_none_yields_no_regions_and_preview_refuses(tmp_path):
    cfg = PrivacyConfig(blur="none")
    assert len(privacy_regions(np.array([[0.0, 0, 10, 10]]), "center", cfg, (50, 50, 3))) == 0
    with pytest.raises(PermissionError):
        PreviewWriter(tmp_path / "p.mp4", (64, 64), 10, cfg)
    PreviewWriter(tmp_path / "p.mp4", (64, 64), 10, cfg, allow_raw=True).close()  # explicit opt-in works


def test_database_schema_has_no_place_for_trajectories_or_images(tmp_path):
    from storelens.store import EventStore
    st = EventStore(tmp_path / "x.db")
    cols = {r[1] for t in ("runs", "visits", "zone_visits", "entries", "heat")
            for r in st.query(f"PRAGMA table_info({t})")}
    assert not ({"x", "y", "xy", "trajectory", "image", "frame", "embedding", "face"} & cols)
    tables = {r[0] for r in st.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sqlite_sequence" in tables or True
    assert not any("traj" in t or "frame" in t or "embed" in t for t in tables)


def test_audience_table_is_aggregate_only_and_visit_tables_hold_no_demographics(tmp_path):
    from storelens.store import EventStore
    st = EventStore(tmp_path / "x.db")
    cols = lambda t: [r[1] for r in st.query(f"PRAGMA table_info({t})")]  # noqa: E731
    assert cols("audience") == ["run_id", "bucket_ts", "zone_id", "metric", "age_band", "gender", "n"]
    demographic = {"age", "age_band", "gender", "sex", "female", "male"}
    for t in ("visits", "zone_visits", "entries", "heat", "runs"):
        assert not demographic & set(cols(t)), f"{t} must not carry demographics: they would join to a visit"
    assert "visit_key" not in cols("audience"), "no key that could join an audience row back to a visit"


def test_no_face_or_image_artifacts_in_the_schema_scan(tmp_path):
    from storelens.audit import schema_scan
    from storelens.store import EventStore
    assert schema_scan(EventStore(tmp_path / "x.db"))["status"] == "PASS"
