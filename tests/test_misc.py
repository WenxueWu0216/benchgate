from benchgate.capacity.queue_sim import Workload, erlang_c_wait, mean_service_s, simulate
from benchgate.github_check import check_payload
from benchgate.service import history_report


def test_capacity_matches_erlang_c():
    mu = 1 / mean_service_s(Workload())
    lam = 5.5 * mu
    sim = simulate(8, Workload(), days=120, seed=1,
                   service_override=lambda r: r.exponential(1 / mu),
                   arrival_override=lambda r: r.exponential(1 / lam))
    assert abs(sim["mean_job_wait_s"] / erlang_c_wait(lam, mu, 8) - 1) < 0.15


def test_capacity_more_nodes_less_wait():
    w = [simulate(n, Workload(), days=7)["peak_turnaround_p95_min"] for n in (6, 9, 14)]
    assert w[0] > w[1] > w[2]


def _verdict(v, blocking=False):
    t = {"workload": "matrix", "metric": "p50", "base_median": 1e6, "cand_median": 1.1e6,
         "ci": {"estimate": 0.1, "low": 0.08, "high": 0.12}, "p_adjusted": 0.001, "verdict": v}
    return {"verdict": v, "blocking": blocking, "config": {}, "tests": [t]}


def test_check_conclusions():
    assert check_payload(_verdict("regression", True), "a", "b", None)["conclusion"] == "failure"
    assert check_payload(_verdict("pass"), "a", "b", None)["conclusion"] == "success"
    assert check_payload(_verdict("inconclusive"), "a", "b", None)["conclusion"] == "neutral"
    p = check_payload(_verdict("regression", True), "a", "b", "https://x/p.png")
    assert p["output"]["images"][0]["image_url"] == "https://x/p.png"
    assert "| matrix | p50 |" in p["output"]["summary"]


def test_history_report_finds_step():
    import numpy as np
    rng = np.random.default_rng(0)
    vals = np.r_[rng.normal(1000, 3, 40), rng.normal(1050, 3, 20)]
    hist = [(f"c{i}", i, v, 10) for i, v in enumerate(vals)]
    rep = history_report(hist)
    assert [c["commit"] for c in rep["change_points"]] == ["c40"]
    assert rep["change_points"][0]["kind"] == "regression"
