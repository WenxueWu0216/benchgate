import numpy as np
import pytest
from scipy import stats as sps

from benchgate.stats.bootstrap import bootstrap_relative_change
from benchgate.stats.changepoint import cusum, pelt, robust_sigma
from benchgate.stats.gate import GateConfig, Verdict, evaluate, holm
from benchgate.stats.mannwhitney import mann_whitney_u


@pytest.mark.parametrize("alt", ["greater", "less", "two-sided"])
@pytest.mark.parametrize("m,n", [(5, 5), (10, 10), (8, 12), (30, 40)])
def test_mwu_matches_scipy(alt, m, n):
    rng = np.random.default_rng(m * 100 + n)
    for _ in range(20):
        x = rng.normal(0, 1, m)
        y = rng.normal(0.5, 1, n)
        ours = mann_whitney_u(x, y, alt)
        method = "exact" if ours.method == "exact" else "asymptotic"
        ref = sps.mannwhitneyu(y, x, alternative=alt, method=method)
        assert ours.u == pytest.approx(ref.statistic)
        assert ours.p_value == pytest.approx(ref.pvalue, rel=1e-6, abs=1e-12)


def test_mwu_ties_asymptotic_matches_scipy():
    x = np.array([1, 2, 2, 3, 3, 3, 4, 5], float)
    y = np.array([2, 3, 4, 4, 5, 5, 6], float)
    ours = mann_whitney_u(x, y, "greater")
    ref = sps.mannwhitneyu(y, x, alternative="greater", method="asymptotic")
    assert ours.method == "asymptotic"
    assert ours.p_value == pytest.approx(ref.pvalue, rel=1e-9)


def test_bootstrap_bca_close_to_scipy():
    rng = np.random.default_rng(3)
    b = rng.lognormal(0, 0.05, 15)
    c = rng.lognormal(0.04, 0.05, 15)
    ours = bootstrap_relative_change(b, c, n_boot=20000, rng=np.random.default_rng(1))
    ref = sps.bootstrap((b, c), lambda x, y, axis=-1: np.median(y, axis=axis) / np.median(x, axis=axis) - 1,
                        method="BCa", n_resamples=20000, random_state=1, vectorized=True)
    assert ours.low == pytest.approx(ref.confidence_interval.low, abs=0.006)
    assert ours.high == pytest.approx(ref.confidence_interval.high, abs=0.006)


def test_bootstrap_coverage():
    """95% interval should cover the true ratio roughly 95% of the time."""
    rng = np.random.default_rng(7)
    true = 1.03
    hits = 0
    trials = 300
    for _ in range(trials):
        b = rng.normal(100, 2, 20)
        c = rng.normal(100 * true, 2 * true, 20)
        ci = bootstrap_relative_change(b, c, n_boot=2000, rng=rng)
        hits += ci.contains(true - 1)
    assert 0.88 <= hits / trials <= 0.99


def test_holm():
    adj = holm([0.01, 0.04, 0.03, 0.5])
    assert list(np.round(adj, 4)) == [0.04, 0.09, 0.09, 0.5]


def _runs(rng, n, shift=0.0, sigma_run=0.01, n_iter=200, sigma_iter=0.05):
    return [rng.lognormal(np.log(1e6 * (1 + shift)) + rng.normal(0, sigma_run), sigma_iter, n_iter)
            for _ in range(n)]


def test_gate_detects_regression_and_passes_clean():
    rng = np.random.default_rng(11)
    cfg = GateConfig(n_boot=2000)
    base = {"w": _runs(rng, 10)}
    assert evaluate(base, {"w": _runs(rng, 10, shift=0.10)}, cfg).verdict == Verdict.REGRESSION
    assert evaluate(base, {"w": _runs(rng, 10, shift=-0.10)}, cfg).verdict == Verdict.IMPROVEMENT
    clean = evaluate(base, {"w": _runs(rng, 10)}, cfg)
    assert clean.verdict == Verdict.PASS and not clean.blocking


def test_gate_small_significant_change_below_min_effect_passes():
    rng = np.random.default_rng(5)
    cfg = GateConfig(n_boot=2000, min_effect=0.01)
    base = {"w": _runs(rng, 20, sigma_run=0.0005, sigma_iter=0.005)}
    head = {"w": _runs(rng, 20, shift=0.004, sigma_run=0.0005, sigma_iter=0.005)}
    res = evaluate(base, head, cfg)
    p50 = next(t for t in res.tests if t.metric == "p50")
    assert p50.p_adjusted < 0.01           # real...
    assert p50.verdict != Verdict.REGRESSION  # ...but too small to block


def test_gate_inconclusive_when_noise_is_huge():
    rng = np.random.default_rng(9)
    cfg = GateConfig(n_boot=2000)
    res = evaluate({"w": _runs(rng, 4, sigma_run=0.15)}, {"w": _runs(rng, 4, sigma_run=0.15)}, cfg)
    assert res.verdict in (Verdict.INCONCLUSIVE, Verdict.PASS)
    assert any(t.verdict == Verdict.INCONCLUSIVE for t in res.tests) or res.verdict == Verdict.PASS


def test_pelt_matches_ruptures():
    rpt = pytest.importorskip("ruptures")
    rng = np.random.default_rng(0)
    for _ in range(5):
        x = np.r_[rng.normal(0, 1, 50), rng.normal(2.5, 1, 30), rng.normal(-1, 1, 40)]
        s = robust_sigma(x)
        ours = pelt(x, sigma=s, penalty=2 * np.log(x.size))
        ref = rpt.Pelt(model="l2", min_size=3, jump=1).fit((x / s).reshape(-1, 1)).predict(
            pen=2 * np.log(x.size))[:-1]
        assert ours == ref


def test_pelt_quiet_on_noise_and_finds_step():
    rng = np.random.default_rng(1)
    quiet = sum(len(pelt(rng.normal(0, 1, 120))) > 0 for _ in range(50))
    assert quiet <= 4
    x = np.r_[rng.normal(0, 1, 80), rng.normal(3, 1, 40)]
    assert any(abs(c - 80) <= 2 for c in pelt(x))


def test_cusum_detects_step_once():
    rng = np.random.default_rng(2)
    x = np.r_[rng.normal(0, 1, 60), rng.normal(2, 1, 60)]
    alarms = cusum(x)
    assert len(alarms) >= 1
    first = alarms[0]
    # The alarm fires after the change; the start estimate is rougher.
    assert 60 <= first.index <= 75 and 50 <= first.start_estimate <= 66
