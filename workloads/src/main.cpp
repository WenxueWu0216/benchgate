// benchgate_workloads: run one workload and emit per-iteration latency JSON.
//
//   benchgate_workloads --workload planner --iterations 500 --commit abc123 --out run.json
//   benchgate_workloads --workload planner --slowdown-mode tail --slowdown-pct 5
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>

#include "bench/workloads.hpp"

static void usage() {
  std::fprintf(stderr,
      "usage: benchgate_workloads --workload NAME [--iterations N] [--warmup N]\n"
      "         [--seed S] [--commit SHA] [--out FILE]\n"
      "         [--slowdown-mode none|uniform|tail] [--slowdown-pct P]\n"
      "       benchgate_workloads --list\n");
}

int main(int argc, char** argv) {
  bench::RunConfig cfg;
  std::string workload, commit = "unknown", out;
  // The slowdown can also come from the environment so a CI job can inject a
  // regression into an otherwise identical binary without changing argv.
  if (const char* m = std::getenv("BENCHGATE_SLOWDOWN_MODE"))
    cfg.slowdown.mode = bench::parse_slowdown_mode(m);
  if (const char* p = std::getenv("BENCHGATE_SLOWDOWN_PCT"))
    cfg.slowdown.pct = std::atof(p);

  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    auto next = [&]() -> std::string {
      if (i + 1 >= argc) { usage(); std::exit(2); }
      return argv[++i];
    };
    if (a == "--workload") workload = next();
    else if (a == "--iterations") cfg.iterations = std::stoi(next());
    else if (a == "--warmup") cfg.warmup = std::stoi(next());
    else if (a == "--seed") cfg.seed = std::stoull(next());
    else if (a == "--commit") commit = next();
    else if (a == "--out") out = next();
    else if (a == "--slowdown-mode") cfg.slowdown.mode = bench::parse_slowdown_mode(next());
    else if (a == "--slowdown-pct") cfg.slowdown.pct = std::stod(next());
    else if (a == "--list") {
      for (const auto& n : bench::workload_names()) std::cout << n << "\n";
      return 0;
    } else { usage(); return 2; }
  }
  if (cfg.slowdown.mode != bench::SlowdownMode::kNone && cfg.slowdown.pct <= 0) {
    std::fprintf(stderr, "--slowdown-pct must be > 0 when a slowdown mode is set\n");
    return 2;
  }
  auto w = bench::make_workload(workload);
  if (!w) { usage(); return 2; }

  const auto r = bench::run(*w, cfg);
  const std::string js = bench::to_json(r, cfg, commit);
  if (out.empty()) {
    std::cout << js << "\n";
  } else {
    std::ofstream f(out);
    f << js << "\n";
    if (!f) { std::perror("write"); return 1; }
  }
  return 0;
}
