"""Layout / marketing recommendations: the arithmetic is checked against figures re-derived from the same data."""
import numpy as np
import pytest

from storelens.queries import Analytics, Window
from storelens.recommend import recommend

from .test_queries import FIX, T0, Builder

# fixture -> (share of visitors who come near it, share of those who stop)
PROFILE = {"front_table": (0.60, 0.40), "wall_a": (0.60, 0.40), "gondola_c": (0.60, 0.40),
           "endcap_b": (0.60, 0.12),    # seen, not engaging
           "back_promo": (0.20, 0.55)}  # engaging, rarely reached


def _store(tmp_path, n=1200, seed=4):
    b = Builder(tmp_path / "r.db")
    b.run("week", T0)
    rng = np.random.default_rng(seed)
    for _ in range(n):
        zones = []
        for z in FIX:
            reach, rate = PROFILE[z]
            if rng.random() < reach:
                zones.append((z, 12.0, 10.0 if rng.random() < rate else 0.5, 0.0))
        b.visit(zones, dt=12.0)
    b.finish()
    return b.st


def _rec(cfg, st):
    an = Analytics(st, cfg)
    return an, recommend(an, Window())


def test_finds_the_two_planted_problems(cfg, tmp_path):
    an, out = _rec(cfg, _store(tmp_path))
    by_zone = {r["zone_id"]: r for r in out["recommendations"] if r["lever"] == "layout"}
    assert by_zone["endcap_b"]["type"] == "improve_conversion"
    assert by_zone["back_promo"]["type"] == "increase_reach"
    for typical in ("front_table", "wall_a", "gondola_c"):
        assert typical not in by_zone, "an average display is not a problem"


def test_revenue_follows_the_stated_formula(cfg, tmp_path):
    an, out = _rec(cfg, _store(tmp_path))
    z = {z["zone_id"]: z for z in an.zones(Window())}["endcap_b"]
    rec = next(r for r in out["recommendations"] if r["zone_id"] == "endcap_b")
    med_rate = out["benchmarks"]["median_engagement_rate"]
    rate, reach = z["engagement_rate"], z["reach"]
    d = [reach * (rate + 0.5 * (med_rate - rate) - rate), reach * (med_rate - rate)]
    V, pr, value = (out["assumptions"][k] for k in ("visitors_per_day", "purchase_rate", "avg_item_value"))
    assert rec["effect"]["extra_engaged_per_day"] == pytest.approx([d[0] * V, d[1] * V], rel=1e-6)
    assert rec["effect"]["revenue_per_day"] == pytest.approx([d[0] * V * pr[0] * value, d[1] * V * pr[1] * value], rel=1e-6)
    assert rec["effect"]["revenue_per_month"] == pytest.approx([x * 30 for x in rec["effect"]["revenue_per_day"]])
    lo, hi = rec["effect"]["revenue_per_day"]
    assert 0 < lo < hi


def test_hidden_gem_reports_extra_passers_but_the_conversion_fix_does_not(cfg, tmp_path):
    an, out = _rec(cfg, _store(tmp_path))
    recs = {r["zone_id"]: r for r in out["recommendations"] if r["lever"] == "layout"}
    assert "passers_per_visitor" in recs["back_promo"]["effect"]
    assert "passers_per_visitor" not in recs["endcap_b"]["effect"]


def test_more_traffic_means_proportionally_more_money(cfg, tmp_path):
    st = _store(tmp_path)
    a = recommend(Analytics(st, cfg), Window())
    cfg2 = cfg.model_copy(deep=True)
    cfg2.business.visitors_per_day = 2 * cfg.business.visitors_per_day
    b = recommend(Analytics(st, cfg2), Window())
    ra = next(r for r in a["recommendations"] if r["zone_id"] == "endcap_b")["effect"]["revenue_per_day"]
    rb = next(r for r in b["recommendations"] if r["zone_id"] == "endcap_b")["effect"]["revenue_per_day"]
    assert rb == pytest.approx([2 * x for x in ra])


def test_summary_is_the_sum_of_one_scenario_per_display(cfg, tmp_path):
    an, out = _rec(cfg, _store(tmp_path))
    layout = {}
    for r in out["recommendations"]:
        if r["lever"] == "layout":
            layout.setdefault(r["zone_id"], r)
    s = out["summary"]
    assert s["revenue_per_day"] == pytest.approx([sum(r["effect"]["revenue_per_day"][i] for r in layout.values()) for i in (0, 1)])
    assert "assum" in s["note"].lower()


def test_assumptions_and_caveats_are_always_stated(cfg, tmp_path):
    _, out = _rec(cfg, _store(tmp_path))
    a = out["assumptions"]
    assert a["visitors_per_day_source"].startswith("assumed")
    assert a["purchase_rate"][0] < a["purchase_rate"][1]
    assert any("not predictions" in c for c in out["caveats"])


def test_too_little_data_gives_a_warning_not_a_guess(cfg, tmp_path):
    out = recommend(Analytics(_store(tmp_path, n=20), cfg), Window())
    assert out["recommendations"] == [] and any("Fewer than 30" in w for w in out["warnings"])


def test_measured_traffic_is_used_when_no_assumption_is_configured(cfg, tmp_path):
    cfg2 = cfg.model_copy(deep=True)
    cfg2.business.visitors_per_day = None
    out = recommend(Analytics(_store(tmp_path), cfg2), Window())
    assert out["assumptions"]["visitors_per_day_source"].startswith("measured")
    assert out["assumptions"]["visitors_per_day"] > 0


def test_identical_displays_are_rarely_flagged_by_chance(cfg, tmp_path):
    """Five displays with the SAME true reach and stopping rate: sampling noise alone must seldom raise a flag."""
    flagged = checked = 0
    for seed in range(20):
        b = Builder(tmp_path / f"n{seed}.db")
        b.run("week", T0)
        rng = np.random.default_rng(100 + seed)
        for _ in range(250):
            zones = [(z, 12.0, 10.0 if rng.random() < 0.4 else 0.5, 0.0) for z in FIX if rng.random() < 0.6]
            b.visit(zones, dt=12.0)
        b.finish()
        out = recommend(Analytics(b.st, cfg), Window())
        flagged += len({r["zone_id"] for r in out["recommendations"] if r["lever"] == "layout"})
        checked += len(FIX)
    assert flagged / checked <= 0.08, f"{flagged} of {checked} identical displays flagged"
