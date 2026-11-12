"""HTTP API (stdlib only).

POST /jobs                 {spec}                     -> job
GET  /jobs/<id>                                       -> job (+runs with ?runs=1)
POST /compare              {base, head, workloads, repetitions, ...} -> comparison
GET  /compare/<id>                                    -> comparison (+result when done)
GET  /plots/<id>.png                                  -> verdict plot
GET  /history/<workload>?metric=p50_ns&branch=main    -> change points over history
POST /commits              {sha, parent, seq, branch} -> register commit order
GET  /healthz
Auth: if BENCHGATE_TOKEN is set, every request but /healthz needs
"Authorization: Bearer <token>".
"""

from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import re
from urllib.parse import parse_qs, urlparse

from benchgate.models import JobSpec
from benchgate.service import BenchGateService

log = logging.getLogger("benchgate.api")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def make_handler(svc: BenchGateService, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BenchGate/0.1"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            if not token:
                return True
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got, f"Bearer {token}"):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if parts == ["healthz"]:
                return self._send(200, {"ok": True, "nodes": svc.orch.executor.nodes()})
            if not self._authed():
                return
            if len(parts) == 2 and parts[0] == "jobs":
                j = svc.orch.get(parts[1])
                return self._send(200, j.to_dict(with_runs=q.get("runs") == "1")) if j \
                    else self._send(404, {"error": "no such job"})
            if len(parts) == 2 and parts[0] == "compare":
                c = svc.comparisons.get(parts[1])
                return self._send(200, c.to_dict()) if c else self._send(404, {"error": "not found"})
            if len(parts) == 2 and parts[0] == "plots" and parts[1].endswith(".png"):
                cid = parts[1][:-4]
                c = svc.comparisons.get(cid)
                if not SAFE_ID.match(cid) or not c or not c.plot or not os.path.exists(c.plot):
                    return self._send(404, {"error": "no plot"})
                with open(c.plot, "rb") as f:
                    return self._send(200, f.read(), "image/png")
            if len(parts) == 2 and parts[0] == "history":
                try:
                    return self._send(200, svc.history_check(
                        parts[1], q.get("metric", "p50_ns"), q.get("branch", "main"),
                        float(q.get("min_effect", 0.01))))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._authed():
                return
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                body = self._body()
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})
            try:
                if parts == ["jobs"]:
                    return self._send(201, svc.orch.submit(JobSpec.from_dict(body)).to_dict())
                if parts == ["compare"]:
                    c = svc.start_comparison(
                        body["base"], body["head"], body.get("workloads") or ["pointcloud", "planner", "matrix"],
                        repetitions=int(body.get("repetitions", 10)),
                        iterations=int(body.get("iterations", 300)),
                        head_image=body.get("head_image"), base_image=body.get("base_image"),
                        slowdown_mode=body.get("slowdown_mode", "none"),
                        slowdown_pct=float(body.get("slowdown_pct", 0.0)))
                    return self._send(202, c.to_dict())
                if parts == ["commits"]:
                    svc.store.record_commit(body["sha"], body.get("parent"), int(body["seq"]),
                                            body.get("branch", "main"), body.get("committed_at"))
                    return self._send(201, {"ok": True})
            except (KeyError, TypeError, ValueError) as e:
                return self._send(400, {"error": f"bad request: {e}"})
            self._send(404, {"error": "not found"})

    return Handler


def serve(svc: BenchGateService, host: str = "0.0.0.0", port: int = 8080) -> None:
    token = os.environ.get("BENCHGATE_TOKEN")
    httpd = ThreadingHTTPServer((host, port), make_handler(svc, token))
    log.info("listening on %s:%d (auth %s)", host, port, "on" if token else "off")
    httpd.serve_forever()
