"""Generate a synthetic multi-period dataset: store.yaml + (video, ground truth) per period."""
from __future__ import annotations

import copy
import gzip
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..config import dump_config
from .layout import CAM_SIZE, Layout, build_store_config, default_layout
from .render import Renderer
from .world import (Goal, NavGrid, Person, arrival_times, execute_goals, plan_customer, staff_counter,
                    staff_roamer)

# Customer clothing avoids red hues (staff vests are red) and includes a few low-contrast outfits.
PALETTE = [(120, 60, 30), (60, 100, 40), (110, 110, 110), (38, 38, 40), (50, 85, 120), (110, 50, 100),
           (40, 150, 190), (120, 120, 30), (80, 60, 50), (150, 110, 60), (70, 70, 120)]
LOW_CONTRAST = (192, 202, 207)
HAIR = [(25, 25, 30), (30, 45, 70), (50, 70, 100), (70, 110, 150), (140, 150, 160)]
STAFF_VEST = (35, 35, 205)


@dataclass
class Period:
    label: str
    minutes: float = 30.0
    seed: int = 1
    base_per_min: float = 5.0
    group_prob: float = 0.15
    # fixture_id -> {param: value}, e.g. {"endcap_b": {"p_browse": 0.40}}
    overrides: dict = field(default_factory=dict)


def apply_overrides(layout: Layout, overrides: dict) -> Layout:
    lay = copy.deepcopy(layout)
    for fid, params in overrides.items():
        f = lay.fixture(fid)
        for k, v in params.items():
            setattr(f, k, v)
    return lay


def build_people(layout: Layout, period: Period, fps: int) -> list[Person]:
    rng = np.random.default_rng(period.seed)
    dt = 1.0 / fps
    n_frames = int(period.minutes * 60 * fps)
    nav = NavGrid(layout)
    people: list[Person] = []
    pid = 0
    gid = 0

    def clothing():
        if rng.random() < 0.04:
            return LOW_CONTRAST
        return PALETTE[int(rng.integers(len(PALETTE)))]

    for t_arr in arrival_times(rng, period.minutes * 60 - 100, period.base_per_min):
        goals = plan_customer(rng, layout, layout.fixtures)
        gid += 1
        size = 2 if rng.random() < period.group_prob else 1
        for k in range(size):
            if k == 0:
                g = goals
                off = (0.0, 0.0)
            else:
                g = []
                for goal in goals:
                    if goal.kind == "browse" and rng.random() > 0.6:
                        goal = Goal("pass", goal.fixture, goal.pos)
                    g.append(goal)
                off = (float(rng.choice([-0.55, 0.55])), float(rng.uniform(-0.2, 0.3)))
            start = int(t_arr * fps) + (0 if k == 0 else int(rng.uniform(4, 14)))
            if start >= n_frames:
                continue
            x, y, h, script = execute_goals(rng, nav, layout, g, start * dt, dt, offset=off)
            m = n_frames - start
            people.append(Person(pid, "customer", gid, start, x[:m], y[:m], h[:m], clothing(),
                                 HAIR[int(rng.integers(len(HAIR)))], script))
            pid += 1
    # staff
    x, y, h = staff_counter(rng, layout, n_frames, dt)
    people.append(Person(pid, "staff", -1, 0, x[:n_frames], y[:n_frames], h[:n_frames], STAFF_VEST,
                         HAIR[1], [{"kind": "counter"}]))
    pid += 1
    x, y, h = staff_roamer(rng, nav, layout, layout.fixtures, n_frames, dt)
    people.append(Person(pid, "staff", -2, 0, x[:n_frames], y[:n_frames], h[:n_frames], STAFF_VEST,
                         HAIR[0], [{"kind": "roamer"}]))
    return people


def write_ground_truth(path: Path, people: list[Person], fps: int, n_frames: int, period: Period) -> None:
    payload = {
        "fps": fps, "n_frames": n_frames, "label": period.label, "overrides": period.overrides,
        "people": [{
            "id": p.pid, "role": p.role, "group": p.group, "start_frame": p.start_frame,
            "x": np.round(p.x, 3).tolist(), "y": np.round(p.y, 3).tolist(), "h": np.round(p.h, 3).tolist(),
            "script": p.script,
        } for p in people],
    }
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))


def generate_period(layout: Layout, period: Period, out_dir: Path, fps: int = 10, verbose: bool = True):
    lay = apply_overrides(layout, period.overrides)
    people = build_people(lay, period, fps)
    n_frames = int(period.minutes * 60 * fps)
    out_dir.mkdir(parents=True, exist_ok=True)
    video = out_dir / f"{period.label}.mp4"
    gt = out_dir / f"{period.label}_gt.json.gz"
    renderer = Renderer(lay, seed=period.seed + 100)
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), fps, CAM_SIZE)
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open a video writer (mp4v)")
    by_start = sorted(people, key=lambda p: p.start_frame)
    active: list[Person] = []
    nxt = 0
    t0 = time.time()
    for fi in range(n_frames):
        while nxt < len(by_start) and by_start[nxt].start_frame <= fi:
            active.append(by_start[nxt])
            nxt += 1
        active = [p for p in active if fi < p.end_frame]
        states = []
        for p in active:
            i = fi - p.start_frame
            states.append((float(p.x[i]), float(p.y[i]), float(p.h[i]), p.clothing, p.hair))
        writer.write(renderer.frame(states, fi / fps))
        if verbose and fi % 3000 == 0 and fi:
            print(f"  [{period.label}] frame {fi}/{n_frames} ({fi / max(time.time() - t0, 1e-6):.0f} fps)")
    writer.release()
    write_ground_truth(gt, people, fps, n_frames, period)
    return video, gt, people


def default_periods(minutes: float = 30.0, seed: int = 11) -> list[Period]:
    return [
        Period("baseline", minutes=minutes, seed=seed),
        # "We put new signage on Endcap B": browse probability 0.22 -> 0.40; everything else identical.
        Period("after_change", minutes=minutes, seed=seed + 1,
               overrides={"endcap_b": {"p_browse": 0.40}}),
    ]


def generate_dataset(out_dir: str | Path, minutes: float = 30.0, seed: int = 11, fps: int = 10,
                     verbose: bool = True) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    layout = default_layout()
    cfg = build_store_config(layout, seed=seed)
    cfg_path = out / "store.yaml"
    dump_config(cfg, cfg_path)
    result = {"config": str(cfg_path), "periods": []}
    for period in default_periods(minutes, seed):
        if verbose:
            print(f"generating {period.label} ({period.minutes:.0f} min)...")
        video, gt, people = generate_period(layout, period, out, fps=fps, verbose=verbose)
        result["periods"].append({"label": period.label, "video": str(video), "gt": str(gt),
                                  "n_people": len(people)})
    return result
