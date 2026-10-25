"""Behavioural model: scripted shoppers and staff -> ground-truth trajectories.

Every person follows a *plan* (door -> fixtures -> checkout? -> door). At each fixture a shopper
picks one of four behaviours, which is what makes the validation meaningful:

  browse      walk to the display, stop, turn to face it, look for a few seconds   (engaged)
  away_stop   stop in the exposure zone but face AWAY (phone call, waiting)        (impression only)
  glance      slow walk through the zone, no stop                                   (impression only)
  pass        walk through the zone at normal pace                                  (impression only)

Trajectories are generated at the video frame rate so ground truth aligns frame-for-frame.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import numpy as np

from .layout import Fixture, Layout, Rect

TWO_PI = 2 * math.pi


def wrap(a: float) -> float:
    return (a + math.pi) % TWO_PI - math.pi


@dataclass
class Person:
    pid: int
    role: str  # customer | staff
    group: int
    start_frame: int
    x: np.ndarray
    y: np.ndarray
    h: np.ndarray  # heading (rad), atan2(dy, dx) in floor coords
    clothing: tuple[int, int, int]
    hair: tuple[int, int, int]
    script: list[dict] = field(default_factory=list)

    @property
    def end_frame(self) -> int:
        return self.start_frame + len(self.x)


# ----------------------------------------------------------------------------- navigation

class NavGrid:
    def __init__(self, layout: Layout, cell: float = 0.2, inflate: float = 0.38):
        self.cell = cell
        self.nx = int(layout.width / cell)
        self.ny = int(layout.height / cell)
        free = np.ones((self.ny, self.nx), dtype=bool)
        for x0, y0, x1, y1 in layout.obstacles():
            i0, i1 = int((x0 - inflate) / cell), int(math.ceil((x1 + inflate) / cell))
            j0, j1 = int((y0 - inflate) / cell), int(math.ceil((y1 + inflate) / cell))
            free[max(j0, 0):max(j1, 0), max(i0, 0):max(i1, 0)] = False
        self.free = free

    def _cell(self, p) -> tuple[int, int]:
        return (min(max(int(p[0] / self.cell), 0), self.nx - 1), min(max(int(p[1] / self.cell), 0), self.ny - 1))

    def _nearest_free(self, c):
        if self.free[c[1], c[0]]:
            return c
        for r in range(1, 12):
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    i, j = c[0] + di, c[1] + dj
                    if 0 <= i < self.nx and 0 <= j < self.ny and self.free[j, i]:
                        return (i, j)
        return c

    def _line_free(self, a, b) -> bool:
        n = int(max(abs(b[0] - a[0]), abs(b[1] - a[1])) / (self.cell * 0.5)) + 1
        for k in range(n + 1):
            t = k / n
            i, j = self._cell((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            if not self.free[j, i]:
                return False
        return True

    def path(self, a, b) -> list[tuple[float, float]]:
        if self._line_free(a, b):
            return [tuple(a), tuple(b)]
        s, g = self._nearest_free(self._cell(a)), self._nearest_free(self._cell(b))
        open_ = [(0.0, s)]
        came: dict = {}
        cost = {s: 0.0}
        moves = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                 (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)]
        while open_:
            _, cur = heapq.heappop(open_)
            if cur == g:
                break
            for di, dj, c in moves:
                nb = (cur[0] + di, cur[1] + dj)
                if not (0 <= nb[0] < self.nx and 0 <= nb[1] < self.ny) or not self.free[nb[1], nb[0]]:
                    continue
                nc = cost[cur] + c
                if nc < cost.get(nb, 1e18):
                    cost[nb] = nc
                    came[nb] = cur
                    h = math.hypot(g[0] - nb[0], g[1] - nb[1])
                    heapq.heappush(open_, (nc + h, nb))
        cells = [g]
        while cells[-1] in came:
            cells.append(came[cells[-1]])
        cells.reverse()
        pts = [tuple(a)] + [((c[0] + 0.5) * self.cell, (c[1] + 0.5) * self.cell) for c in cells[1:-1]] + [tuple(b)]
        # greedy string pulling
        out = [pts[0]]
        i = 0
        while i < len(pts) - 1:
            j = len(pts) - 1
            while j > i + 1 and not self._line_free(pts[i], pts[j]):
                j -= 1
            out.append(pts[j])
            i = j
        return out


# ----------------------------------------------------------------------------- timeline

class Timeline:
    """Appends samples at a fixed dt while a person walks / stands."""

    def __init__(self, pos, heading, dt: float, rng: np.random.Generator, speed: float):
        self.dt, self.rng, self.speed = dt, rng, speed
        self.xs = [pos[0]]
        self.ys = [pos[1]]
        self.hs = [heading]

    @property
    def pos(self):
        return (self.xs[-1], self.ys[-1])

    @property
    def heading(self):
        return self.hs[-1]

    def _push(self, x, y, h):
        self.xs.append(x)
        self.ys.append(y)
        self.hs.append(h)

    def walk(self, path, speed: float | None = None, max_turn: float = 4.5):
        sp = speed if speed is not None else self.speed
        for target in path[1:]:
            while True:
                x, y = self.pos
                dx, dy = target[0] - x, target[1] - y
                d = math.hypot(dx, dy)
                step = sp * self.dt * float(np.clip(self.rng.normal(1.0, 0.08), 0.7, 1.3))
                if d <= step:
                    desired = math.atan2(dy, dx) if d > 1e-6 else self.heading
                    self._push(target[0], target[1], self._turn(desired, max_turn))
                    break
                desired = math.atan2(dy, dx)
                h = self._turn(desired, max_turn)
                self._push(x + math.cos(h) * step if abs(wrap(desired - h)) < 0.9 else x + dx / d * step * 0.3,
                           y + math.sin(h) * step if abs(wrap(desired - h)) < 0.9 else y + dy / d * step * 0.3, h)

    def _turn(self, desired: float, max_turn: float) -> float:
        diff = wrap(desired - self.heading)
        lim = max_turn * self.dt
        return wrap(self.heading + float(np.clip(diff, -lim, lim)))

    def stand(self, duration: float, face: float | None, max_turn: float = 3.0, sway: float = 0.012):
        n = max(int(duration / self.dt), 1)
        ox, oy = self.pos
        jx = jy = 0.0
        for _ in range(n):
            jx = 0.92 * jx + float(self.rng.normal(0, sway))
            jy = 0.92 * jy + float(self.rng.normal(0, sway))
            h = self._turn(face, max_turn) if face is not None else self.heading
            self._push(ox + jx, oy + jy, h)

    def arrays(self):
        return np.array(self.xs), np.array(self.ys), np.array(self.hs)


# ----------------------------------------------------------------------------- planning

@dataclass
class Goal:
    kind: str  # browse | away_stop | glance | pass | checkout | exit
    fixture: str | None
    pos: tuple[float, float]
    face: float | None = None
    dwell: float = 0.0


def _rand_in(rect: Rect, rng) -> tuple[float, float]:
    return (float(rng.uniform(rect[0], rect[2])), float(rng.uniform(rect[1], rect[3])))


def _in_rect(p, r: Rect) -> bool:
    return r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]


def plan_customer(rng, layout: Layout, fixtures: list[Fixture], with_checkout_p: float = 0.55) -> list[Goal]:
    """A shopper's ordered goals. Fixtures are visited in nearest-neighbour order from the door."""
    chosen = [f for f in fixtures if rng.random() < f.p_visit]
    rng.shuffle(chosen)
    cur = ((layout.door_x[0] + layout.door_x[1]) / 2, layout.door_y)
    ordered: list[Fixture] = []
    remaining = list(chosen)
    while remaining:
        nxt = min(remaining, key=lambda f: math.dist(cur, _centroid(f.exposure)) + float(rng.normal(0, 1.2)))
        ordered.append(nxt)
        remaining.remove(nxt)
        cur = _centroid(nxt.exposure)

    goals: list[Goal] = []
    cur = ((layout.door_x[0] + layout.door_x[1]) / 2, layout.door_y)
    for f in ordered:
        r = rng.random()
        if r < f.p_browse:
            kind = "browse"
        elif r < f.p_browse + f.p_away:
            kind = "away_stop"
        elif r < f.p_browse + f.p_away + f.p_glance:
            kind = "glance"
        else:
            kind = "pass"
        if kind in ("browse", "away_stop"):
            sr = _closest_rect(f.stand_rects, cur, rng)
            pos = _rand_in(sr, rng)
            bearing = math.atan2(f.target[1] - pos[1], f.target[0] - pos[0])
            if kind == "browse":
                dwell = float(np.clip(rng.lognormal(math.log(f.dwell_median_s), 0.45), 3.0, 70.0))
                goals.append(Goal(kind, f.id, pos, face=bearing, dwell=dwell))
            else:
                goals.append(Goal(kind, f.id, pos, face=wrap(bearing + math.pi + float(rng.normal(0, 0.6))),
                                  dwell=float(rng.uniform(4.0, 12.0))))
        else:
            pos = _rand_in(_shrink(f.exposure, 0.15), rng)
            goals.append(Goal(kind, f.id, pos))
        cur = goals[-1].pos
    if rng.random() < with_checkout_p:
        pos = _rand_in(layout.checkout_stand, rng)
        face = math.atan2(layout.checkout_target[1] - pos[1], layout.checkout_target[0] - pos[0])
        goals.append(Goal("checkout", None, pos, face=face, dwell=float(rng.uniform(10, 28))))
    goals.append(Goal("exit", None, ((layout.door_x[0] + layout.door_x[1]) / 2 + float(rng.uniform(-0.8, 0.8)), 11.95)))
    return goals


def _centroid(r: Rect):
    return ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)


def _shrink(r: Rect, m: float) -> Rect:
    return (r[0] + m, r[1] + m, r[2] - m, r[3] - m)


def _closest_rect(rects: list[Rect], cur, rng) -> Rect:
    d = np.array([math.dist(cur, _centroid(r)) for r in rects]) + rng.normal(0, 0.8, len(rects))
    return rects[int(np.argmin(d))]


def execute_goals(rng, nav: NavGrid, layout: Layout, goals: list[Goal], t_arrive: float, dt: float,
                  offset: tuple[float, float] = (0.0, 0.0), speed: float | None = None):
    """Turn goals into a trajectory. Returns (x, y, h, script)."""
    sp = speed if speed is not None else float(np.clip(rng.normal(1.15, 0.15), 0.75, 1.55))
    door_mid = ((layout.door_x[0] + layout.door_x[1]) / 2 + float(rng.uniform(-0.8, 0.8)) + offset[0], 11.95)
    tl = Timeline(door_mid, -math.pi / 2, dt, rng, sp)
    script: list[dict] = []
    for g in goals:
        gp = (min(max(g.pos[0] + offset[0] * 0.6, 0.15), layout.width - 0.15),
              min(max(g.pos[1] + offset[1] * 0.6, 0.15), layout.height - 0.05))
        t0 = t_arrive + len(tl.xs) * dt
        if g.kind in ("browse", "away_stop"):
            tl.walk(nav.path(tl.pos, gp))
            tl.stand(g.dwell, g.face)
        elif g.kind == "glance":
            path = nav.path(tl.pos, gp)
            tl.walk(path, speed=min(sp, 0.55))
        elif g.kind == "pass":
            tl.walk(nav.path(tl.pos, gp))
        elif g.kind == "checkout":
            tl.walk(nav.path(tl.pos, gp))
            tl.stand(g.dwell, g.face)
        elif g.kind == "exit":
            tl.walk(nav.path(tl.pos, gp))
        script.append({"kind": g.kind, "fixture": g.fixture, "t0": round(t0, 2),
                       "t1": round(t_arrive + len(tl.xs) * dt, 2)})
    x, y, h = tl.arrays()
    return x, y, h, script


def staff_counter(rng, layout: Layout, n_frames: int, dt: float) -> tuple:
    c = _centroid(layout.staff_zone)
    tl = Timeline(c, math.pi / 2, dt, rng, 1.0)
    nxt = float(rng.uniform(40, 90))
    while len(tl.xs) < n_frames:
        t = len(tl.xs) * dt
        if t > nxt:
            tl.walk([tl.pos, (c[0] - 1.2, c[1] - 0.2)], speed=0.8)
            tl.stand(float(rng.uniform(3, 6)), math.pi / 2)
            tl.walk([tl.pos, c], speed=0.8)
            nxt = t + float(rng.uniform(40, 90))
        else:
            tl.stand(min(5.0, (n_frames - len(tl.xs)) * dt + dt), math.pi / 2 + float(rng.normal(0, 0.4)), sway=0.02)
    return tl.arrays()


def staff_roamer(rng, nav: NavGrid, layout: Layout, fixtures: list[Fixture], n_frames: int, dt: float) -> tuple:
    c = _centroid(layout.staff_zone)
    home = (c[0] - 1.0, c[1])
    tl = Timeline(home, math.pi / 2, dt, rng, 1.0)
    tl.stand(float(rng.uniform(10, 40)), math.pi / 2)
    while len(tl.xs) < n_frames:
        f = fixtures[int(rng.integers(len(fixtures)))]
        sr = f.stand_rects[int(rng.integers(len(f.stand_rects)))]
        pos = _rand_in(sr, rng)
        face = math.atan2(f.target[1] - pos[1], f.target[0] - pos[0])
        tl.walk(nav.path(tl.pos, pos), speed=0.95)
        tl.stand(float(rng.uniform(15, 40)), face)  # restocking: stationary AND facing a display
        tl.walk(nav.path(tl.pos, home), speed=0.95)
        tl.stand(float(rng.uniform(25, 70)), math.pi / 2)
    return tl.arrays()


def arrival_times(rng, duration_s: float, base_per_min: float) -> list[float]:
    """Inhomogeneous Poisson arrivals: quiet start, busy middle, quiet end."""
    times = []
    for s in range(int(duration_s)):
        lam = base_per_min / 60.0 * (0.45 + 1.1 * math.sin(math.pi * s / duration_s) ** 2)
        if rng.random() < lam:
            times.append(s + float(rng.random()))
    return times
