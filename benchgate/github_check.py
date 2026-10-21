"""Post the verdict as a GitHub check run.

A check run with conclusion "failure" blocks the merge once the check name
("BenchGate") is listed as required in branch protection / rulesets.
"""

from __future__ import annotations

import json
import os
import urllib.request

from benchgate.report import markdown_summary

CONCLUSION = {"pass": "success", "improvement": "success", "regression": "failure",
              "inconclusive": "neutral"}


def check_payload(verdict: dict, base: str, head: str, plot_url: str | None,
                  details_url: str | None = None, name: str = "BenchGate") -> dict:
    v = verdict["verdict"]
    conclusion = CONCLUSION[v]
    if v == "inconclusive" and verdict.get("blocking"):
        conclusion = "failure"
    n_reg = sum(t["verdict"] == "regression" for t in verdict["tests"])
    title = (f"{n_reg} regression(s) detected" if v == "regression"
             else f"{v}: {len(verdict['tests'])} tests")
    out = {"title": title, "summary": markdown_summary(verdict, base, head, plot_url)[:65000]}
    if plot_url and plot_url.startswith("https://"):
        out["images"] = [{"alt": "BenchGate verdict plot", "image_url": plot_url}]
    payload = {"name": name, "head_sha": head, "status": "completed",
               "conclusion": conclusion, "output": out}
    if details_url:
        payload["details_url"] = details_url
    return payload


def post_check(payload: dict, repo: str, token: str,
               api: str = os.environ.get("GITHUB_API_URL", "https://api.github.com")) -> dict:
    req = urllib.request.Request(
        f"{api}/repos/{repo}/check-runs", data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())
