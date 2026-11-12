#include "bench/harness.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <sstream>
#include <stdexcept>
#include <unistd.h>

namespace bench {

using Clock = std::chrono::steady_clock;

static inline int64_t now_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             Clock::now().time_since_epoch())
      .count();
}

void spin_for_ns(int64_t ns) {
  if (ns <= 0) return;
  const int64_t end = now_ns() + ns;
  while (now_ns() < end) {
#if defined(__x86_64__)
    __builtin_ia32_pause();
#endif
  }
}

double percentile(std::vector<int64_t> v, double p) {
  if (v.empty()) return 0.0;
  std::sort(v.begin(), v.end());
  // Linear interpolation between closest ranks (numpy's default).
  const double rank = p / 100.0 * static_cast<double>(v.size() - 1);
  const size_t lo = static_cast<size_t>(std::floor(rank));
  const size_t hi = static_cast<size_t>(std::ceil(rank));
  const double frac = rank - static_cast<double>(lo);
  return static_cast<double>(v[lo]) * (1.0 - frac) +
         static_cast<double>(v[hi]) * frac;
}

SlowdownMode parse_slowdown_mode(const std::string& s) {
  if (s == "none") return SlowdownMode::kNone;
  if (s == "uniform") return SlowdownMode::kUniform;
  if (s == "tail") return SlowdownMode::kTail;
  throw std::invalid_argument("unknown slowdown mode: " + s);
}

static const char* mode_name(SlowdownMode m) {
  switch (m) {
    case SlowdownMode::kUniform: return "uniform";
    case SlowdownMode::kTail: return "tail";
    default: return "none";
  }
}

RunResult run(Workload& w, const RunConfig& cfg) {
  RunResult r;
  r.workload = w.name();
  w.setup(cfg.seed);
  r.params = w.params();

  std::vector<int64_t> warm;
  warm.reserve(cfg.warmup);
  for (int i = 0; i < cfg.warmup; ++i) {
    const int64_t t0 = now_ns();
    r.checksum += w.iterate(i);
    warm.push_back(now_ns() - t0);
  }
  // Tail injection threshold is fixed from warmup so the decision of which
  // iterations to slow down does not depend on the slowdown itself.
  const double tail_threshold = percentile(warm, 95.0);

  r.iteration_ns.reserve(cfg.iterations);
  for (int i = 0; i < cfg.iterations; ++i) {
    const int64_t t0 = now_ns();
    r.checksum += w.iterate(cfg.warmup + i);
    const int64_t work = now_ns() - t0;
    if (cfg.slowdown.mode == SlowdownMode::kUniform) {
      spin_for_ns(static_cast<int64_t>(work * cfg.slowdown.pct / 100.0));
    } else if (cfg.slowdown.mode == SlowdownMode::kTail &&
               static_cast<double>(work) > tail_threshold) {
      spin_for_ns(static_cast<int64_t>(work * cfg.slowdown.pct / 100.0));
    }
    r.iteration_ns.push_back(now_ns() - t0);
  }
  return r;
}

static std::string json_escape(const std::string& s) {
  std::string o;
  for (char c : s) {
    if (c == '"' || c == '\\') { o += '\\'; o += c; }
    else if (static_cast<unsigned char>(c) < 0x20) { o += ' '; }
    else { o += c; }
  }
  return o;
}

std::string to_json(const RunResult& r, const RunConfig& cfg,
                    const std::string& commit) {
  std::ostringstream os;
  os.precision(10);
  char host[256] = {0};
  gethostname(host, sizeof(host) - 1);
  double sum = 0;
  for (auto v : r.iteration_ns) sum += static_cast<double>(v);
  const double mean = r.iteration_ns.empty() ? 0 : sum / r.iteration_ns.size();

  os << "{\"schema\":\"benchgate.run.v1\",";
  os << "\"workload\":\"" << json_escape(r.workload) << "\",";
  os << "\"commit\":\"" << json_escape(commit) << "\",";
  os << "\"host\":\"" << json_escape(host) << "\",";
  os << "\"seed\":" << cfg.seed << ",";
  os << "\"warmup\":" << cfg.warmup << ",";
  os << "\"slowdown\":{\"mode\":\"" << mode_name(cfg.slowdown.mode)
     << "\",\"pct\":" << cfg.slowdown.pct << "},";
  os << "\"params\":{";
  bool first = true;
  for (const auto& [k, v] : r.params) {
    os << (first ? "" : ",") << "\"" << json_escape(k) << "\":" << v;
    first = false;
  }
  os << "},";
  os << "\"checksum\":" << r.checksum << ",";
  os << "\"summary\":{\"n\":" << r.iteration_ns.size()
     << ",\"mean_ns\":" << mean
     << ",\"p50_ns\":" << percentile(r.iteration_ns, 50)
     << ",\"p90_ns\":" << percentile(r.iteration_ns, 90)
     << ",\"p99_ns\":" << percentile(r.iteration_ns, 99) << "},";
  os << "\"iteration_ns\":[";
  for (size_t i = 0; i < r.iteration_ns.size(); ++i) {
    os << (i ? "," : "") << r.iteration_ns[i];
  }
  os << "]}";
  return os.str();
}

}  // namespace bench
