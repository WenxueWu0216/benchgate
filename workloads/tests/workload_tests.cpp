// Minimal self-checking tests (no framework dependency).
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>

#include "bench/workloads.hpp"

#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d %s\n", __FILE__, __LINE__, #c); std::exit(1); } } while (0)

int main() {
  // Percentile matches numpy's linear interpolation.
  CHECK(std::fabs(bench::percentile({1, 2, 3, 4}, 50) - 2.5) < 1e-9);
  CHECK(std::fabs(bench::percentile({10, 20, 30, 40, 50}, 90) - 46.0) < 1e-9);

  for (const auto& name : bench::workload_names()) {
    auto w = bench::make_workload(name);
    CHECK(w != nullptr);
    bench::RunConfig cfg;
    cfg.warmup = 10;
    cfg.iterations = 20;
    const auto cold = bench::run(*w, cfg);  // first run pays page faults, freq ramp
    CHECK(cold.iteration_ns.size() == 20);
    // Deterministic: same seed -> same checksum; the slowdown must not change
    // what is computed, only how long it takes. +100% is injected; minimum
    // iteration times are compared because noise only ever adds time. On a
    // shared VM a whole run occasionally executes at half speed (host steal),
    // so the timing half of the check gets a few attempts.
    bench::RunConfig slow_cfg = cfg;
    slow_cfg.slowdown = {bench::SlowdownMode::kUniform, 100.0};
    bool slowed = false;
    for (int attempt = 0; attempt < 5 && !slowed; ++attempt) {
      auto w2 = bench::make_workload(name);
      const auto b = bench::run(*w2, slow_cfg);
      auto w3 = bench::make_workload(name);
      const auto a = bench::run(*w3, cfg);
      CHECK(a.checksum == b.checksum && a.checksum == cold.checksum);
      const auto mina = *std::min_element(a.iteration_ns.begin(), a.iteration_ns.end());
      const auto minb = *std::min_element(b.iteration_ns.begin(), b.iteration_ns.end());
      slowed = minb > mina * 1.5;
      if (!slowed)
        std::fprintf(stderr, "%s attempt %d: min base %lld ns, min slowed %lld ns\n",
                     name.c_str(), attempt, static_cast<long long>(mina),
                     static_cast<long long>(minb));
    }
    CHECK(slowed);
    std::printf("ok %s checksum=%.6f\n", name.c_str(), cold.checksum);
  }
  return 0;
}
