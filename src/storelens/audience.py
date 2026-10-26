"""Aggregate audience statistics with small-count protection (pure functions, no I/O).

Rules every number shown to a user passes through here:

* ``unknown`` is never folded into a group: it is reported as a coverage figure instead.
* A group is only shown if it has at least ``k`` people. Adjacent age bands are MERGED until they do (``<13`` + ``13-24``
  becomes ``<25``); a gender split is shown only if both sides reach ``k``. Merging (instead of blanking one cell)
  also avoids the classic "total minus the other cells" leak.
* Shares and affinity indices come with intervals, and the sample size is always shown, so a thin number looks thin.
"""
from __future__ import annotations

import math
import re

Z95 = 1.959964


def wilson(x: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = x / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _parse_band(b: str) -> tuple[float, float] | None:
    b = b.strip()
    m = re.fullmatch(r"<\s*(\d+)", b)
    if m:
        return (0, int(m.group(1)) - 1)
    m = re.fullmatch(r"(\d+)\s*\+", b)
    if m:
        return (int(m.group(1)), math.inf)
    m = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", b)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def band_label(bands: list[str]) -> str:
    if len(bands) == 1:
        return bands[0]
    lo, hi = _parse_band(bands[0]), _parse_band(bands[-1])
    if lo is None or hi is None:
        return f"{bands[0]} to {bands[-1]}"
    if lo[0] == 0 and math.isinf(hi[1]):
        return "all ages"
    if lo[0] == 0:
        return f"<{int(hi[1]) + 1}"
    if math.isinf(hi[1]):
        return f"{int(lo[0])}+"
    return f"{int(lo[0])}-{int(hi[1])}"


def merge_groups(counts: dict[str, int], bands: list[str], k: int) -> list[list[str]]:
    """Adjacent bands merged until every group has >= k people. [] if even everyone together is below k."""
    def tot(g: list[str]) -> int:
        return sum(counts.get(b, 0) for b in g)

    groups = [[b] for b in bands]
    if tot(bands) < k:
        return []
    while len(groups) > 1 and min(tot(g) for g in groups) < k:
        i = min(range(len(groups)), key=lambda j: tot(groups[j]))
        nbrs = [j for j in (i - 1, i + 1) if 0 <= j < len(groups)]
        j = min(nbrs, key=lambda j: tot(groups[j]))
        lo, hi = sorted((i, j))
        groups[lo:hi + 1] = [groups[lo] + groups[hi]]
    return groups


def share_ci(x: int, n: int) -> list[float]:
    lo, hi = wilson(x, n)
    return [round(lo, 4), round(hi, 4)]


def index_ci(x1: int, n1: int, x2: int, n2: int) -> dict:
    """Over/under-indexing of a group: its share among ``n1`` events divided by its share among ``n2`` visitors.

    Interval from the log-ratio delta method treating the two shares as independent. They are not (the events are a
    subset of the visitors), which makes the interval slightly WIDE (conservative), never narrow."""
    if n1 <= 0 or n2 <= 0 or x2 <= 0:
        return {"index": None, "ci": None, "verdict": "n/a"}
    p1, p2 = x1 / n1, x2 / n2
    if x1 == 0:
        return {"index": 0.0, "ci": None, "verdict": "n/a"}
    idx = p1 / p2
    se = math.sqrt(max((1 - p1) / x1 + (1 - p2) / x2, 0.0))
    lo, hi = math.exp(math.log(idx) - Z95 * se), math.exp(math.log(idx) + Z95 * se)
    verdict = "over" if lo > 1 else ("under" if hi < 1 else "in line")
    return {"index": round(idx, 3), "ci": [round(lo, 3), round(hi, 3)], "verdict": verdict}


def known_total(counts: dict[str, int]) -> int:
    return sum(v for key, v in counts.items() if key != "unknown")
