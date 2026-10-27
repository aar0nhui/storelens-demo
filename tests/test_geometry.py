import numpy as np

from storelens.geometry import Homography, angle_diff, points_in_polygon
from storelens.sim.layout import build_store_config, cam_homography, default_layout


def test_homography_recovers_floor_points(cfg):
    H = Homography.from_camera(cfg.cameras[0])
    # calibration used 1.5 px of click noise; reprojection error should be a few cm
    assert H.reprojection_rmse_cm() < 8
    # a floor point mapped to the true camera pixel and back lands within a few cm
    import cv2
    truth = cam_homography()
    floor = np.array([[9.0, 6.0], [3.0, 10.0], [15.0, 2.0]])
    px = cv2.perspectiveTransform(floor.reshape(-1, 1, 2), truth).reshape(-1, 2)
    back = H.to_floor(px)
    assert np.hypot(*(back - floor).T).max() < 0.15


def test_homography_roundtrip():
    px = [(0, 0), (100, 0), (100, 50), (0, 50)]
    fl = [(0, 0), (10, 0), (10, 5), (0, 5)]
    h = Homography(px, fl)
    pts = np.array([[25.0, 10.0], [80.0, 40.0]])
    assert np.allclose(h.to_pixel(h.to_floor(pts)), pts, atol=1e-6)
    assert h.reprojection_rmse_cm() < 1e-6


def test_bad_calibration_is_visible():
    # one mis-clicked point inflates the reported error: this is the silent-failure guard
    px = [(0, 0), (100, 0), (100, 50), (0, 50), (50, 25)]
    fl = [(0, 0), (10, 0), (10, 5), (0, 5), (9, 1)]
    assert Homography(px, fl).reprojection_rmse_cm() > 20


def test_points_in_polygon():
    poly = [(0, 0), (4, 0), (4, 3), (0, 3)]
    pts = np.array([[1, 1], [5, 1], [2, 2.9], [-0.1, 1], [4.1, 3.1]])
    assert points_in_polygon(pts, poly).tolist() == [True, False, True, False, False]
    # concave polygon
    L = [(0, 0), (4, 0), (4, 1), (1, 1), (1, 4), (0, 4)]
    assert points_in_polygon(np.array([[0.5, 3], [3, 3], [3, 0.5]]), L).tolist() == [True, False, True]


def test_angle_diff_wraps():
    assert np.isclose(angle_diff(np.pi - 0.1, -np.pi + 0.1), 0.2)
    assert np.isclose(angle_diff(0.0, np.pi), np.pi)
