"""Generate Grafana dashboards (JSON) from code, so they are reviewed like code.

  python -m benchgate.dashboards --out dashboards/grafana          # write
  python -m benchgate.dashboards --check dashboards/grafana        # CI: up to date?

Per flavor (duckdb = local, motherduck-duckdb-datasource plugin; bigquery =
GKE, grafana-bigquery-datasource):
  benchgate-module-<workload>.json  one per workload: latency per commit, run-to-run
                                    noise, trace metrics, verdict history
  benchgate-commit.json             one commit ($commit): every test, its CI, the runs
  benchgate-fleet.json              node health: per-node offset vs fleet, jitter, jobs
"""

from __future__ import annotations

import argparse
import json
import os
import sys

WORKLOADS = ["pointcloud", "planner", "matrix"]
DS = {"duckdb": {"type": "motherduck-duckdb-datasource", "uid": "${datasource}"},
      "bigquery": {"type": "grafana-bigquery-datasource", "uid": "${datasource}"}}


class SQL:
    def __init__(self, flavor: str):
        self.f = flavor

    def t(self, name: str) -> str:
        return name if self.f == "duckdb" else f"`${{project}}.${{dataset}}.{name}`"

    def median(self, col: str) -> str:
        return (f"quantile_cont({col}, 0.5)" if self.f == "duckdb"
                else f"APPROX_QUANTILES({col}, 100)[OFFSET(50)]")

    def stddev(self, col: str) -> str:
        return f"stddev_samp({col})" if self.f == "duckdb" else f"STDDEV_SAMP({col})"


def target(sql: str, flavor: str, fmt: str = "table", ref: str = "A") -> dict:
    t = {"refId": ref, "datasource": DS[flavor], "rawSql": " ".join(sql.split()), "format": fmt}
    if flavor == "bigquery":
        t.update({"editorMode": "code", "rawQuery": True})
    return t


_pid = 0


def panel(title, kind, sql, flavor, x, y, w=12, h=8, unit=None, fmt="table", extra=None,
          description=None):
    global _pid
    _pid += 1
    p = {"id": _pid, "type": kind, "title": title, "datasource": DS[flavor],
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [target(sql, flavor, fmt)],
         "fieldConfig": {"defaults": {}, "overrides": []}, "options": {}}
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if description:
        p["description"] = description
    if extra:
        for k, v in extra.items():
            p[k] = v if not isinstance(v, dict) else {**p.get(k, {}), **v}
    return p


def variables(flavor: str, extra: list[dict] | None = None) -> dict:
    v = [{"name": "datasource", "type": "datasource", "query": DS[flavor]["type"],
          "label": "Data source", "hide": 0}]
    if flavor == "bigquery":
        v += [{"name": "project", "type": "textbox", "query": "my-project", "label": "GCP project"},
              {"name": "dataset", "type": "textbox", "query": "benchgate", "label": "Dataset"}]
    return {"list": v + (extra or [])}


def dashboard(uid, title, panels, flavor, templating, tags):
    return {"uid": f"{uid}-{flavor}"[:40], "title": f"{title} ({flavor})", "tags": ["benchgate", *tags],
            "timezone": "utc", "schemaVersion": 39, "version": 1, "editable": True,
            "time": {"from": "now-30d", "to": "now"}, "refresh": "5m",
            "templating": templating, "panels": panels}


def module_dashboard(w: str, flavor: str) -> dict:
    global _pid
    _pid = 0
    q = SQL(flavor)
    runs, verdict_tests = q.t("runs"), q.t("verdict_tests")
    flt = f"workload = '{w}' AND $__timeFilter(started_at)"
    panels = [
        panel("p50 / p99 per commit (median over runs)", "timeseries", f"""
            SELECT min(started_at) AS time, {q.median('p50_ns')} / 1000 AS p50_us,
                   {q.median('p99_ns')} / 1000 AS p99_us
            FROM {runs} WHERE {flt} AND slowdown_mode = 'none'
            GROUP BY commit ORDER BY time""", flavor, 0, 0, 24, 9, "µs", "time_series",
              description="Each point is one commit: the median of its runs' p50/p99. "
                          "Steps here are what change-point detection looks for."),
        panel("Run-to-run noise: CV of per-run p50, per commit", "timeseries", f"""
            SELECT min(started_at) AS time,
                   100 * {q.stddev('p50_ns')} / avg(p50_ns) AS cv_pct, count(*) AS runs
            FROM {runs} WHERE {flt} GROUP BY commit ORDER BY time""", flavor, 0, 9, 12, 8,
              "percent", "time_series",
              description="If this rises, the fleet got noisier and the gate loses power "
                          "(see docs/results/detection.md)."),
        panel("Scheduling jitter p99 (timer probe)", "timeseries", f"""
            SELECT started_at AS time, sched_jitter_p99_us AS jitter_p99_us, node
            FROM {runs} WHERE {flt} AND sched_jitter_p99_us IS NOT NULL ORDER BY time""",
              flavor, 12, 9, 12, 8, "µs", "time_series"),
        panel("CPU utilization / involuntary context switches", "timeseries", f"""
            SELECT started_at AS time, cpu_utilization, involuntary_cs
            FROM {runs} WHERE {flt} AND cpu_utilization IS NOT NULL ORDER BY time""",
              flavor, 0, 17, 12, 8, None, "time_series"),
        panel("Memory bandwidth (perf) / IPC", "timeseries", f"""
            SELECT started_at AS time, mem_bw_gbps, ipc
            FROM {runs} WHERE {flt} AND (mem_bw_gbps IS NOT NULL OR ipc IS NOT NULL)
            ORDER BY time""", flavor, 12, 17, 12, 8, None, "time_series",
              description="Empty where perf counters are unavailable (VMs without a PMU)."),
        panel("Verdicts for this workload", "table", f"""
            SELECT created_at, head_commit, base_commit, metric,
                   round(100 * rel_change, 2) AS change_pct,
                   round(100 * ci_low, 2) AS ci_low_pct, round(100 * ci_high, 2) AS ci_high_pct,
                   p_adjusted, verdict
            FROM {verdict_tests} WHERE workload = '{w}' AND $__timeFilter(created_at)
            ORDER BY created_at DESC LIMIT 200""", flavor, 0, 25, 24, 10,
              extra={"fieldConfig": {"overrides": [{
                  "matcher": {"id": "byName", "options": "verdict"},
                  "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
                                 {"id": "mappings", "value": [{"type": "value", "options": {
                                     "regression": {"color": "red", "index": 0},
                                     "pass": {"color": "green", "index": 1},
                                     "inconclusive": {"color": "orange", "index": 2},
                                     "improvement": {"color": "blue", "index": 3}}}]}]}]}}),
    ]
    return dashboard(f"bg-module-{w}", f"BenchGate · module · {w}", panels, flavor,
                     variables(flavor), ["module", w])


def commit_dashboard(flavor: str) -> dict:
    global _pid
    _pid = 0
    q = SQL(flavor)
    vt, runs, verdicts = q.t("verdict_tests"), q.t("runs"), q.t("verdicts")
    commit_var = {"name": "commit", "type": "query", "label": "Head commit",
                  "datasource": DS[flavor], "refresh": 2,
                  "query": f"SELECT DISTINCT head_commit FROM {verdicts} ORDER BY head_commit",
                  "definition": f"SELECT DISTINCT head_commit FROM {verdicts}"}
    panels = [
        panel("Gate verdict", "stat", f"""
            SELECT verdict, blocking, base_commit FROM {verdicts}
            WHERE head_commit = '$commit' ORDER BY created_at DESC LIMIT 1""", flavor, 0, 0, 8, 5,
              extra={"options": {"reduceOptions": {"fields": "/^verdict$/", "calcs": ["lastNotNull"]},
                                 "colorMode": "background"}}),
        panel("Relative change with 95% bootstrap CI", "barchart", f"""
            SELECT workload || ' ' || metric AS test, 100 * ci_low AS ci_low_pct,
                   100 * rel_change AS change_pct, 100 * ci_high AS ci_high_pct
            FROM {vt} WHERE head_commit = '$commit'
            ORDER BY test""", flavor, 8, 0, 16, 10, "percent"),
        panel("All tests", "table", f"""
            SELECT workload, metric, base_median / 1000 AS base_us, cand_median / 1000 AS head_us,
                   round(100 * rel_change, 2) AS change_pct,
                   round(100 * ci_low, 2) AS ci_low_pct, round(100 * ci_high, 2) AS ci_high_pct,
                   p_value, p_adjusted, verdict
            FROM {vt} WHERE head_commit = '$commit' ORDER BY workload, metric""",
              flavor, 0, 10, 24, 8),
        panel("Per-run p50 (every run of this commit)", "table", f"""
            SELECT workload, node, repetition, p50_ns / 1000 AS p50_us, p99_ns / 1000 AS p99_us,
                   sched_jitter_p99_us, involuntary_cs, started_at
            FROM {runs} WHERE commit = '$commit' ORDER BY workload, started_at""",
              flavor, 0, 18, 24, 10),
    ]
    return dashboard("bg-commit", "BenchGate · commit", panels, flavor,
                     variables(flavor, [commit_var]), ["commit"])


def fleet_dashboard(flavor: str) -> dict:
    global _pid
    _pid = 0
    q = SQL(flavor)
    runs, jobs = q.t("runs"), q.t("jobs")
    panels = [
        panel("Per-node offset vs fleet median (matrix p50)", "barchart", f"""
            WITH per_node AS (
              SELECT node, {q.median('p50_ns')} AS p50 FROM {runs}
              WHERE workload = 'matrix' AND $__timeFilter(started_at) GROUP BY node)
            SELECT node, 100 * (p50 / (SELECT {q.median('p50')} FROM per_node) - 1) AS offset_pct
            FROM per_node ORDER BY node""", flavor, 0, 0, 12, 9, "percent",
              description="A node drifting away from the fleet (thermal, firmware, a missed "
                          "tuning step) shows up here before it causes false alarms."),
        panel("Scheduling jitter p99 by node", "timeseries", f"""
            SELECT started_at AS time, node, sched_jitter_p99_us FROM {runs}
            WHERE $__timeFilter(started_at) AND sched_jitter_p99_us IS NOT NULL ORDER BY time""",
              flavor, 12, 0, 12, 9, "µs", "time_series"),
        panel("Job outcomes", "piechart", f"""
            SELECT state, count(*) AS jobs FROM {jobs}
            WHERE $__timeFilter(created_at) GROUP BY state""", flavor, 0, 9, 8, 8),
        panel("Attempts per job (retries)", "histogram", f"""
            SELECT attempts FROM {jobs} WHERE $__timeFilter(created_at)""", flavor, 8, 9, 8, 8),
        panel("Runs per hour", "timeseries", f"""
            SELECT date_trunc('hour', started_at) AS time, count(*) AS runs FROM {runs}
            WHERE $__timeFilter(started_at) GROUP BY 1 ORDER BY 1""".replace(
                "date_trunc('hour', started_at)",
                "date_trunc('hour', started_at)" if flavor == "duckdb" else "TIMESTAMP_TRUNC(started_at, HOUR)"),
              flavor, 16, 9, 8, 8, None, "time_series"),
    ]
    return dashboard("bg-fleet", "BenchGate · fleet", panels, flavor, variables(flavor), ["fleet"])


def build_all() -> dict[str, dict]:
    out = {}
    for flavor in DS:
        for w in WORKLOADS:
            out[f"{flavor}/benchgate-module-{w}.json"] = module_dashboard(w, flavor)
        out[f"{flavor}/benchgate-commit.json"] = commit_dashboard(flavor)
        out[f"{flavor}/benchgate-fleet.json"] = fleet_dashboard(flavor)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--out")
    g.add_argument("--check")
    args = p.parse_args()
    dashboards = build_all()
    root = args.out or args.check
    stale = []
    for rel, d in dashboards.items():
        path = os.path.join(root, rel)
        text = json.dumps(d, indent=2, ensure_ascii=False) + "\n"
        if args.check:
            if not os.path.exists(path) or open(path, encoding="utf-8").read() != text:
                stale.append(rel)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
    if stale:
        print("stale dashboards (run python -m benchgate.dashboards --out dashboards/grafana):",
              *stale, sep="\n  ")
        return 1
    print(f"{len(dashboards)} dashboards {'up to date' if args.check else 'written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
