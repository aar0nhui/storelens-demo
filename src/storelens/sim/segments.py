"""Synthetic shopper segments (age band x perceived gender) with different display preferences.

EVERYTHING HERE IS INVENTED to exercise the audience analytics: the mix of shoppers and which display each segment
prefers are made-up parameters of the simulator, not facts about real shoppers. The pipeline never imports this
module; it sees only the video (and, for the simulated attribute model in ``attributes.py``, a ground-truth
sidecar that stands in for what a face model would report).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np

from .layout import Fixture

AGE_BANDS = ["<13", "13-24", "25-44", "45-64", "65+"]
GENDERS = ["female", "male"]

# Share of shoppers by (age band, gender). Sums to 1. Children appear only with an adult (see ``sample_segment``).
MIX: dict[tuple[str, str], float] = {
    ("13-24", "female"): 0.10, ("13-24", "male"): 0.09,
    ("25-44", "female"): 0.21, ("25-44", "male"): 0.17,
    ("45-64", "female"): 0.14, ("45-64", "male"): 0.12,
    ("65+", "female"): 0.07, ("65+", "male"): 0.05,
    ("<13", "female"): 0.025, ("<13", "male"): 0.025,
}

# Per-fixture multipliers on the base p_visit / p_browse, by age band and by gender (applied together).
# 1.0 = no preference. Values are renormalised so the store-wide average stays at the base rate: the segments
# redistribute interest between displays, they do not change the total.
AGE_AFFINITY: dict[str, dict[str, tuple[float, float]]] = {
    # fixture:      band ->  (visit x, browse x)
    "front_table": {"<13": (0.8, 0.8), "13-24": (1.0, 1.1), "25-44": (1.1, 1.0), "45-64": (1.0, 0.9), "65+": (0.9, 0.9)},
    "wall_a":      {"<13": (0.5, 0.6), "13-24": (0.7, 0.7), "25-44": (0.9, 0.9), "45-64": (1.3, 1.2), "65+": (1.6, 1.3)},
    "gondola_c":   {"<13": (0.7, 0.8), "13-24": (0.9, 0.9), "25-44": (1.2, 1.1), "45-64": (1.0, 1.0), "65+": (0.9, 1.0)},
    "endcap_b":    {"<13": (1.0, 1.2), "13-24": (1.7, 1.5), "25-44": (1.0, 1.0), "45-64": (0.7, 0.7), "65+": (0.5, 0.6)},
    "back_promo":  {"<13": (0.4, 0.5), "13-24": (0.8, 0.9), "25-44": (1.1, 1.1), "45-64": (1.1, 1.0), "65+": (0.9, 1.0)},
}
GENDER_AFFINITY: dict[str, dict[str, tuple[float, float]]] = {
    "front_table": {"female": (1.1, 1.1), "male": (0.9, 0.9)},
    "wall_a":      {"female": (1.2, 1.2), "male": (0.8, 0.8)},
    "gondola_c":   {"female": (0.9, 1.0), "male": (1.1, 1.0)},
    "endcap_b":    {"female": (0.9, 0.9), "male": (1.1, 1.1)},
    "back_promo":  {"female": (0.8, 0.8), "male": (1.25, 1.3)},
}
# Older shoppers linger a little longer at displays; the youngest less.
DWELL_X = {"<13": 0.8, "13-24": 0.9, "25-44": 1.0, "45-64": 1.1, "65+": 1.25}


@dataclass(frozen=True)
class Segment:
    age_band: str
    gender: str

    @property
    def key(self) -> str:
        return f"{self.age_band}|{self.gender}"


def mix_probabilities(include_children: bool = True) -> tuple[list[Segment], np.ndarray]:
    segs = [Segment(a, g) for (a, g) in MIX if include_children or a != "<13"]
    p = np.array([MIX[(s.age_band, s.gender)] for s in segs], float)
    return segs, p / p.sum()


def sample_segment(rng: np.random.Generator, include_children: bool = False) -> Segment:
    segs, p = mix_probabilities(include_children)
    return segs[int(rng.choice(len(segs), p=p))]


def _mult(table: dict, fid: str, key: str) -> tuple[float, float]:
    return table.get(fid, {}).get(key, (1.0, 1.0))


def _raw_factors(seg: Segment, fid: str) -> tuple[float, float]:
    av, ab = _mult(AGE_AFFINITY, fid, seg.age_band)
    gv, gb = _mult(GENDER_AFFINITY, fid, seg.gender)
    return av * gv, ab * gb


def _normalisers(fixtures: list[Fixture]) -> dict[str, tuple[float, float]]:
    """Per-fixture divisor so the mix-weighted mean factor is 1 (children counted as in the full mix)."""
    segs, p = mix_probabilities(True)
    out = {}
    for f in fixtures:
        v = sum(pi * _raw_factors(s, f.id)[0] for s, pi in zip(segs, p))
        b = sum(pi * _raw_factors(s, f.id)[1] for s, pi in zip(segs, p))
        out[f.id] = (v, b)
    return out


def segment_fixtures(fixtures: list[Fixture]) -> dict[str, list[Fixture]]:
    """Fixture lists with each segment's preferences baked into p_visit / p_browse / dwell, keyed by segment key."""
    norm = _normalisers(fixtures)
    out: dict[str, list[Fixture]] = {}
    for (a, g) in MIX:
        seg = Segment(a, g)
        fl = []
        for f in fixtures:
            fv, fb = _raw_factors(seg, f.id)
            nv, nb = norm[f.id]
            c = copy.copy(f)
            c.p_visit = float(min(f.p_visit * fv / nv, 0.97))
            c.p_browse = float(min(f.p_browse * fb / nb, 0.9))
            c.dwell_median_s = f.dwell_median_s * DWELL_X[a]
            fl.append(c)
        out[seg.key] = fl
    return out
