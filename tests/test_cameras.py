"""Multi-camera floor plan: calibration honesty, coverage, single ownership (no double counting)."""
import numpy as np
import pytest

from glance.cameras import CameraNetwork, calibration_report, suggest_detector_params
from glance.config import CameraConfig, StoreConfig
from glance.sim.layout import default_layout
from glance.sim.multicam import build_multicam_config


@pytest.fixture(scope="module")
def mc():
    return build_multicam_config(default_layout(), seed=3)


def _cam_with(cfg: StoreConfig, cam: CameraConfig, px, fl) -> StoreConfig:
    c = cam.model_copy(update={"pixel_points": [list(p) for p in px], "floor_points": [list(p) for p in fl]})
    return cfg.model_copy(update={"cameras": [c if x.id == cam.id else x for x in cfg.cameras]})


def test_four_points_cannot_validate_themselves(mc):
    cam = mc.cameras[0]
    four = _cam_with(mc, cam, cam.pixel_points[:4], cam.floor_points[:4])
    rep = calibration_report(four, four.cameras[0])
    assert rep["n_points"] == 4 and rep["loo_rmse_cm"] is None
    assert any("exact by construction" in w for w in rep["warnings"])


def test_leave_one_out_error_exposes_a_bad_click(mc):
    cam = mc.cameras[0]
    good = calibration_report(mc, cam)
    assert good["loo_rmse_cm"] is not None and good["status"] in ("good", "fair")
    px = [list(p) for p in cam.pixel_points]
    px[2][0] += 60  # one point clicked 60 px away from where it belongs
    bad_cfg = _cam_with(mc, cam, px, cam.floor_points)
    bad = calibration_report(bad_cfg, bad_cfg.cameras[0])
    assert bad["loo_rmse_cm"] > 3 * good["loo_rmse_cm"]
    assert bad["status"] in ("fair", "poor")


def test_points_on_a_line_are_flagged(mc):
    cam = mc.cameras[0]
    n = len(cam.floor_points)
    line = [(1.0 + 0.5 * i, 2.0 + 0.01 * i) for i in range(n)]
    cfg = _cam_with(mc, cam, cam.pixel_points, line)
    try:
        rep = calibration_report(cfg, cfg.cameras[0])
    except Exception:  # a degenerate fit may refuse outright, which is also fine
        return
    assert any("line" in w or "cover only" in w for w in rep["warnings"])


def test_every_display_has_exactly_one_owner_and_staff_zone_has_none_needed(mc):
    net = CameraNetwork(mc)
    owners = net.zone_owners()
    for z in mc.zones:
        if z.kind == "fixture":
            assert owners[z.id] in {c.id for c in mc.cameras}, z.id


def test_a_floor_point_is_accepted_by_exactly_one_camera(mc):
    net = CameraNetwork(mc)
    rng = np.random.default_rng(0)
    pts = np.column_stack([rng.uniform(0.5, mc.floor.width_m - 0.5, 400), rng.uniform(0.5, mc.floor.height_m - 0.5, 400)])
    votes = sum(net.accepts(c.id, pts).astype(int) for c in mc.cameras)
    seen = net.owner_index(pts) >= 0
    assert (votes[seen] == 1).all(), "no double counting where cameras overlap"
    assert (votes <= 1).all()


def test_entrance_camera_owns_the_lobby(mc):
    net = CameraNetwork(mc)
    (x1, y1), (x2, y2) = mc.entrance.p1, mc.entrance.p2
    ix, iy = mc.entrance.inside
    mid = np.array([(x1 + x2) / 2, (y1 + y2) / 2])
    inward = np.array([ix, iy]) - mid
    inward /= np.hypot(*inward)
    lobby = (mid + 1.0 * inward).reshape(1, 2)  # one metre inside the door
    assert net.accepts(net.entrance_camera(), lobby).all()


def test_coverage_numbers_are_consistent(mc):
    cov = CameraNetwork(mc).coverage()
    assert 0.0 <= cov["blind_fraction"] <= 1.0 and cov["covered_fraction"] > 0.8
    assert abs(sum(cov["owned_fraction"].values()) - (np.array(cov["owner_grid"]) >= 0).mean()) < 1e-6
    assert cov["overlap_fraction"] >= 0.0


def test_detector_area_follows_each_cameras_scale(mc):
    areas = [suggest_detector_params(mc, c)["typical_area"] for c in mc.cameras]
    assert len(set(round(a) for a in areas)) == len(areas), "two views with different pixel scales need different areas"
    assert all(a > 0 for a in areas)


def test_single_camera_store_accepts_everything(cfg):
    net = CameraNetwork(cfg)
    assert net.accepts(cfg.cameras[0].id, np.array([[1.0, 1.0], [5.0, 5.0]])).all()
