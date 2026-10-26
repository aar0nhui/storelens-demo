"""Lift with several cameras: the bootstrap resamples TIME BLOCKS (visitors cannot be linked across cameras)."""
import numpy as np

from glance.queries import lift_stats

IDS = ["a", "b", "c", "d"]
NAMES = {i: i for i in IDS}


def _blocks(rng, n_blocks, per_block, rates, block_sd=0.0):
    """Counts per time block: engaged visitors per display and number of store visitors. ``block_sd`` adds a shared
    busy/quiet-hour effect to every display's rate in a block."""
    den = rng.poisson(per_block, n_blocks).clip(min=3).astype(float)
    eng = np.zeros((n_blocks, len(rates)))
    shift = rng.normal(0, block_sd, n_blocks)
    for j, r in enumerate(rates):
        p = np.clip(r * np.exp(shift), 0.0, 0.95)
        eng[:, j] = rng.binomial(den.astype(int), p)
    return eng, den


def _zone(res, z):
    return next(r for r in res["zones"] if r["zone_id"] == z)


def test_block_mode_finds_a_real_improvement_and_leaves_the_controls_alone():
    rng = np.random.default_rng(1)
    ea, da = _blocks(rng, 60, 12, [0.20, 0.20, 0.20, 0.20])
    eb, db = _blocks(rng, 60, 12, [0.40, 0.20, 0.20, 0.20])  # display "a" doubled
    res = lift_stats(ea, eb, ea, eb, IDS, IDS, NAMES, n_boot=2000, seed=3, den_a=da, den_b=db)
    assert res["unit"] == "blocks" and "time blocks" in res["note"]
    a = _zone(res, "a")
    assert a["adjusted"]["verdict"] == "improved" and a["adjusted"]["ci_low_pct"] > 0
    assert a["a"]["visitors"] == int(da.sum()) and a["b"]["visitors"] == int(db.sum())
    for z in ("b", "c", "d"):
        assert _zone(res, z)["adjusted"]["verdict"] != "improved", "an untouched display must not look like a win"


def test_rate_is_total_engaged_over_total_visitors_not_a_mean_of_block_rates():
    # one huge block with a low rate and one tiny block with a high rate: the pooled rate is what a store owner means
    ea = np.array([[1.0, 1, 1, 1], [20.0, 20, 20, 20]] * 3)
    da = np.array([100.0, 100.0] * 3)
    res = lift_stats(ea, ea, ea, ea, IDS, IDS, NAMES, n_boot=500, seed=1, den_a=da, den_b=da)
    a = _zone(res, "a")
    assert abs(a["a"]["per_100"] - 100 * ea[:, 0].sum() / da.sum()) < 1e-9
    assert abs(a["lift_pct"]) < 1e-9


def test_block_intervals_are_wider_than_per_visitor_ones_when_hours_differ_in_how_busy_they_are():
    rng = np.random.default_rng(5)
    ea, da = _blocks(rng, 50, 14, [0.25] * 4, block_sd=0.5)
    eb, db = _blocks(rng, 50, 14, [0.25] * 4, block_sd=0.5)
    blocks = lift_stats(ea, eb, ea, eb, IDS, IDS, NAMES, n_boot=3000, seed=2, den_a=da, den_b=db)

    def expand(e, d):  # the same counts as one 0/1 row per visitor, which treats every visitor as independent
        out = []
        for j in range(len(d)):
            blk = np.zeros((int(d[j]), len(IDS)))
            for z in range(len(IDS)):
                blk[: int(e[j, z]), z] = 1.0
            out.append(blk)
        return np.vstack(out)

    xa, xb = expand(ea, da), expand(eb, db)
    visitors = lift_stats(xa, xb, xa, xb, IDS, IDS, NAMES, n_boot=3000, seed=2)
    wb = _zone(blocks, "a")["ci_high_pct"] - _zone(blocks, "a")["ci_low_pct"]
    wv = _zone(visitors, "a")["ci_high_pct"] - _zone(visitors, "a")["ci_low_pct"]
    assert wb > wv, f"blocks {wb:.0f} points vs visitors {wv:.0f} points: block resampling should not be over-confident"


def test_no_change_rarely_flags_a_winner_or_loser_in_block_mode():
    flagged = 0
    for s in range(40):
        rng = np.random.default_rng(100 + s)
        ea, da = _blocks(rng, 40, 12, [0.22] * 4, block_sd=0.3)
        eb, db = _blocks(rng, 40, 12, [0.22] * 4, block_sd=0.3)
        res = lift_stats(ea, eb, ea, eb, IDS, IDS, NAMES, n_boot=600, seed=s, den_a=da, den_b=db)
        flagged += any(z.get("adjusted", {}).get("verdict") in ("improved", "worse") for z in res["zones"])
    assert flagged <= 6, f"{flagged} of 40 no-change comparisons named a winner or loser"


def test_too_few_blocks_is_an_error_not_a_number():
    rng = np.random.default_rng(0)
    ea, da = _blocks(rng, 3, 20, [0.2] * 4)
    eb, db = _blocks(rng, 40, 12, [0.2] * 4)
    res = lift_stats(ea, eb, ea, eb, IDS, IDS, NAMES, n_boot=200, seed=0, den_a=da, den_b=db)
    assert "error" in res and "time blocks" in res["error"]
