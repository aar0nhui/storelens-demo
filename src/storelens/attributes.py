"""Optional audience estimates: age band and perceived gender, collected as AGGREGATE COUNTS only.

Privacy design (see docs/privacy-and-legal.md):

* Off by default (``demographics.enabled: false``). With it off, no attribute model is loaded and nothing here runs.
* An estimator keeps a few votes per *live track* in memory. When the track ends, ``finalize`` returns one estimate
  and DELETES the state. The estimate is added to a counter and dropped. It is never attached to a visit key, so the
  ``audience`` table has no column that could join it to a visit, a zone visit or a time-of-day trajectory.
* No face image, crop, embedding or per-person label is written anywhere. The model sees the frame only while it runs.
* The categories are coarse, and they are *estimates*: ``perceived gender`` is what a model infers from appearance, not
  a fact about the person; ``unknown`` is a first-class outcome and is never forced into a guess.

Two estimators ship here: ``NullEstimator`` (does nothing) and ``SimulatedEstimator`` (the synthetic demo: an error
model driven by the simulator's ground truth, because rendered blobs have no faces). A production estimator has to
implement the same four methods; none is bundled and none should be switched on before the legal checklist is done.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import numpy as np

from .config import StoreConfig

UNKNOWN = "unknown"
GENDERS = ("female", "male")


@dataclass(frozen=True)
class Estimate:
    age_band: str  # one of cfg.demographics.age_bands, or "unknown"
    gender: str  # "female" | "male" | "unknown"


class AttributeEstimator(Protocol):
    name: str
    simulated: bool

    def observe(self, uid: int, frame_idx: int, t: float, frame: np.ndarray, box_xyxy: np.ndarray,
                floor_xy: np.ndarray) -> None:
        """Called once per frame for every confirmed live track. May ignore the call (rate limiting is its job)."""

    def finalize(self, uid: int) -> Estimate | None:
        """Track ended: return one estimate (or None) and FORGET the track."""

    def discard(self, uid: int) -> None:
        """Forget a track without producing an estimate."""

    def pending(self) -> int:
        """Number of tracks currently held in memory (must be 0 after a run)."""


class NullEstimator:
    name = "none"
    simulated = False

    def observe(self, *a, **k) -> None:
        return None

    def finalize(self, uid: int) -> Estimate | None:
        return None

    def discard(self, uid: int) -> None:
        return None

    def pending(self) -> int:
        return 0


# ----------------------------------------------------------------------------------------------- simulation

def _band_order(bands: list[str]) -> dict[str, int]:
    return {b: i for i, b in enumerate(bands)}


class SimulatedEstimator:
    """A stand-in for a face-region age/gender model, for the synthetic demo and for testing the audience pipeline.

    It looks the tracked person up in the simulator's ground truth (matched by floor position and frame) and then
    reports a NOISY version of the truth, shaped like a real model's errors:

    * per read: the right age band with probability ``age_exact``, else a neighbouring band (``age_adjacent``),
      else a random one; the right gender with probability ``gender_acc``; a read is unusable with
      probability ``p_no_read`` (occlusion, bad angle);
    * per person: a persistent bias (someone who looks older/younger than they are, or is consistently
      misjudged) that does not average out over more reads;
    * a few reads per person are majority-voted; too few reads or a split vote give ``unknown``.

    The accuracies are ILLUSTRATIVE defaults, not the accuracy of any real model; the dashboard says so wherever the
    numbers are shown. ``quality`` rescales them (``poor`` mimics a ceiling camera or low resolution).
    """
    name = "simulated"
    simulated = True

    QUALITY = {  # (age_exact, age_adjacent, gender_acc, p_no_read)
        "good": (0.80, 0.17, 0.93, 0.25),
        "poor": (0.55, 0.30, 0.78, 0.55),
    }

    def __init__(self, people: list[dict], bands: list[str], fps: float, seed: int = 0, quality: str = "good",
                 max_reads: int = 5, min_reads: int = 2, sample_every: int = 3, match_m: float = 1.0,
                 persist_age: float = 0.10, persist_gender: float = 0.04):
        self.bands = list(bands)
        self.order = _band_order(self.bands)
        self.fps, self.seed = fps, seed
        self.age_exact, self.age_adj, self.gender_acc, self.p_no_read = self.QUALITY[quality]
        self.max_reads, self.min_reads, self.sample_every, self.match_m = max_reads, min_reads, sample_every, match_m
        self.persist_age, self.persist_gender = persist_age, persist_gender
        cust = [p for p in people if p["role"] == "customer"]
        staff = [p for p in people if p["role"] != "customer"]
        self._gt = [(p["id"], p["start_frame"], np.asarray(p["x"]), np.asarray(p["y"]), p["age_band"], p["gender"])
                    for p in cust]
        self._staff = [(p["start_frame"], np.asarray(p["x"]), np.asarray(p["y"])) for p in staff]
        self._state: dict[int, dict] = {}
        self._rng = np.random.default_rng(seed)
        # TEST HARNESS ONLY (a real model has no ground truth): one record per finished track, kept in memory so
        # `validate` can build a confusion matrix. Never written to the database.
        self.audit_log: list[dict] = []

    @classmethod
    def from_gt(cls, gt_path: str | Path, bands: list[str], seed: int = 0, quality: str = "good") -> "SimulatedEstimator":
        with gzip.open(gt_path, "rt", encoding="utf-8") as fh:
            gt = json.load(fh)
        return cls(gt["people"], bands, gt["fps"], seed=seed, quality=quality)

    # -- ground truth lookup ----------------------------------------------------------------------
    def _nearest(self, frame_idx: int, xy) -> tuple | None:
        best, bd = None, self.match_m
        for rec in self._gt:
            pid, s, xs, ys, *_ = rec
            i = frame_idx - s
            if 0 <= i < len(xs):
                d = float(np.hypot(xs[i] - xy[0], ys[i] - xy[1]))
                if d < bd:
                    best, bd = rec, d
        if best is not None:
            for s, xs, ys in self._staff:  # a staff member standing closer: this track is not a customer
                i = frame_idx - s
                if 0 <= i < len(xs) and float(np.hypot(xs[i] - xy[0], ys[i] - xy[1])) < bd:
                    return None
        return best

    def _persistent(self, pid: int) -> tuple[int, bool]:
        r = np.random.default_rng([self.seed, int(pid), 17])
        shift = 0
        if r.random() < self.persist_age:
            shift = int(r.choice([-1, 1]))
        return shift, bool(r.random() < self.persist_gender)

    # -- estimator API ------------------------------------------------------------------------------
    def observe(self, uid, frame_idx, t, frame, box_xyxy, floor_xy) -> None:
        st = self._state.setdefault(uid, {"n": 0, "calls": 0, "ages": [], "genders": [], "pid": None, "votes": {}})
        st["calls"] += 1
        if st["calls"] % self.sample_every or len(st["ages"]) >= self.max_reads:
            return
        rec = self._nearest(frame_idx, floor_xy)
        if rec is None:
            return
        pid, _, _, _, age, gender = rec
        st["votes"][pid] = st["votes"].get(pid, 0) + 1
        if self._rng.random() < self.p_no_read:
            return
        shift, flip = self._persistent(pid)
        k = len(self.bands)
        true_i = min(max(self.order[age] + shift, 0), k - 1)
        u = self._rng.random()
        if u < self.age_exact:
            idx = true_i
        elif u < self.age_exact + self.age_adj:
            nb = [j for j in (true_i - 1, true_i + 1) if 0 <= j < k]
            idx = int(self._rng.choice(nb))
        else:
            idx = int(self._rng.integers(k))
        g_true = gender if not flip else ("male" if gender == "female" else "female")
        g = g_true if self._rng.random() < self.gender_acc else ("male" if g_true == "female" else "female")
        st["ages"].append(idx)
        st["genders"].append(g)

    def finalize(self, uid: int) -> Estimate | None:
        st = self._state.pop(uid, None)
        est = self._finalize(st)
        if st is not None and st["votes"]:
            pid = max(st["votes"], key=st["votes"].get)
            rec = next(r for r in self._gt if r[0] == pid)
            self.audit_log.append({"true_age": rec[4], "true_gender": rec[5],
                                   "age": est.age_band if est else UNKNOWN, "gender": est.gender if est else UNKNOWN})
        return est

    def _finalize(self, st: dict | None) -> Estimate | None:
        if st is None or len(st["ages"]) < self.min_reads:
            return Estimate(UNKNOWN, UNKNOWN) if st is not None and st["votes"] else None
        counts = np.bincount(st["ages"], minlength=len(self.bands))
        top = np.flatnonzero(counts == counts.max())
        age_i = int(top[len(top) // 2]) if len(top) > 1 else int(top[0])
        if counts.max() < 0.5 * len(st["ages"]) and len(top) > 1:  # split vote
            age = UNKNOWN
        else:
            age = self.bands[age_i]
        f, m = st["genders"].count("female"), st["genders"].count("male")
        gender = "female" if f > m else "male" if m > f else UNKNOWN
        if max(f, m) < 0.6 * (f + m):
            gender = UNKNOWN
        return Estimate(age, gender)

    def discard(self, uid: int) -> None:
        self._state.pop(uid, None)

    def pending(self) -> int:
        return len(self._state)


def make_estimator(cfg: StoreConfig, gt_path: str | Path | None = None, seed: int = 0,
                   quality: str = "good") -> AttributeEstimator:
    """Estimator for a run. Production has no bundled model: without one, demographics are off even if enabled."""
    if not cfg.demographics.enabled:
        return NullEstimator()
    if gt_path is not None:
        return SimulatedEstimator.from_gt(gt_path, cfg.demographics.age_bands, seed=seed, quality=quality)
    return NullEstimator()


# ----------------------------------------------------------------------------------------------- aggregation

def bucket_start(t: float, hours: int, tz: ZoneInfo) -> float:
    """Start (epoch seconds) of the local-time bucket containing ``t`` (buckets are aligned to local midnight)."""
    dt = datetime.fromtimestamp(t, tz)
    h0 = (dt.hour // hours) * hours
    return dt.replace(hour=h0, minute=0, second=0, microsecond=0).timestamp()


class AudienceCounter:
    """In-memory counters keyed by (bucket, zone, metric, age band, gender). The only thing that reaches the database."""

    def __init__(self, cfg: StoreConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.output.timezone)
        self.hours = cfg.demographics.bucket_hours
        self.counts: dict[tuple, int] = {}
        self.stats = {"people": 0, "estimated": 0, "age_unknown": 0, "gender_unknown": 0, "no_estimate": 0}

    def add(self, est: Estimate | None, t: float, visitor: bool, zones: list[tuple[str, bool, bool]]) -> None:
        """One customer: ``zones`` = [(zone_id, impression, engaged), ...] (already filtered to this camera's zones)."""
        self.stats["people"] += 1
        if est is None:  # the model produced nothing for this customer: counted as unknown, so totals still reconcile
            self.stats["no_estimate"] += 1
            est = Estimate(UNKNOWN, UNKNOWN)
        self.stats["estimated"] += 1
        self.stats["age_unknown"] += est.age_band == UNKNOWN
        self.stats["gender_unknown"] += est.gender == UNKNOWN
        b = bucket_start(t, self.hours, self.tz)

        def inc(zone: str, metric: str) -> None:
            k = (b, zone, metric, est.age_band, est.gender)
            self.counts[k] = self.counts.get(k, 0) + 1

        if visitor:
            inc("", "visitor")
        for zid, impression, engaged in zones:
            if impression:
                inc(zid, "impression")
            if engaged:
                inc(zid, "engaged")
