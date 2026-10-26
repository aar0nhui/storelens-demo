import numpy as np
import pytest

from glance.sim.layout import build_store_config, default_layout


@pytest.fixture(scope="session")
def cfg():
    return build_store_config(default_layout(), seed=1)


def make_traj(segments, fps=10.0, t0=1_800_000_000.0):
    """segments: list of ('walk', (x, y), speed) | ('stand', seconds). Starts at the first walk's origin.

    Returns (t, xy, heading) sampled at fps.
    """
    xs, ys, hs = [], [], []
    pos = np.array(segments[0][1], float)
    heading = 0.0
    xs.append(pos[0]); ys.append(pos[1]); hs.append(heading)
    for seg in segments[1:]:
        if seg[0] == "walk":
            target, speed = np.array(seg[1], float), seg[2]
            d = target - pos
            dist = float(np.hypot(*d))
            n = max(int(round(dist / speed * fps)), 1)
            heading = float(np.arctan2(d[1], d[0]))
            for k in range(1, n + 1):
                p = pos + d * k / n
                xs.append(p[0]); ys.append(p[1]); hs.append(heading)
            pos = target
        elif seg[0] == "stand":
            for _ in range(int(seg[1] * fps)):
                xs.append(pos[0]); ys.append(pos[1]); hs.append(heading)
        elif seg[0] == "turn":  # change heading in place for `seconds`
            heading = float(seg[1])
            for _ in range(int(seg[2] * fps)):
                xs.append(pos[0]); ys.append(pos[1]); hs.append(heading)
    t = t0 + np.arange(len(xs)) / fps
    return t, np.stack([xs, ys], axis=1), np.array(hs)
