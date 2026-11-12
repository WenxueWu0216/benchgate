"""Human-readable verdict summaries (GitHub check run text, CLI output)."""

from __future__ import annotations

ICON = {"pass": "✅", "regression": "❌", "improvement": "🚀", "inconclusive": "⚠️"}


def markdown_summary(verdict: dict, base: str, head: str, plot_url: str | None = None) -> str:
    v = verdict["verdict"]
    cfg = verdict.get("config", {})
    lines = [
        f"## {ICON.get(v, '')} BenchGate: **{v.upper()}**",
        "",
        f"`{head[:12]}` vs base `{base[:12]}` — one-sided Mann-Whitney U on per-run "
        f"statistics, Holm-corrected at α={cfg.get('alpha', 0.01)}; a regression must also "
        f"exceed {cfg.get('min_effect', 0.01) * 100:.1f}% with a 95% bootstrap CI above zero.",
        "",
        "| workload | metric | base | head | Δ | 95% CI | p (adj) | verdict |",
        "|---|---|---:|---:|---:|---|---:|---|",
    ]
    for t in verdict["tests"]:
        ci = t["ci"]
        lines.append(
            f"| {t['workload']} | {t['metric']} | {t['base_median'] / 1e3:.1f} µs | "
            f"{t['cand_median'] / 1e3:.1f} µs | {ci['estimate'] * 100:+.2f}% | "
            f"[{ci['low'] * 100:+.2f}%, {ci['high'] * 100:+.2f}%] | {t['p_adjusted']:.2g} | "
            f"{ICON.get(t['verdict'], '')} {t['verdict']} |")
    if plot_url:
        lines += ["", f"![verdict plot]({plot_url})"]
    if v == "inconclusive":
        lines += ["", "_The noise is too high to rule out a meaningful slowdown. "
                      "Re-run with more repetitions (`/benchgate rerun`)._"]
    return "\n".join(lines)
