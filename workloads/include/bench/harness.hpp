// Benchmark harness: runs a workload body for warmup + measured iterations,
// records per-iteration wall-clock latency, and optionally injects an
// artificial slowdown so the regression detector can be validated end to end.
#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <vector>

namespace bench {

enum class SlowdownMode { kNone, kUniform, kTail };

struct SlowdownConfig {
  SlowdownMode mode = SlowdownMode::kNone;
  // Percent added. kUniform: every iteration is stretched by pct.
  // kTail: only iterations slower than the warmup p95 are stretched, which
  // moves p99 by ~pct while leaving the median essentially untouched.
  double pct = 0.0;
};

struct RunConfig {
  int warmup = 50;
  int iterations = 500;
  uint64_t seed = 42;
  SlowdownConfig slowdown;
};

struct RunResult {
  std::string workload;
  std::vector<int64_t> iteration_ns;
  std::map<std::string, double> params;
  double checksum = 0.0;  // keeps the optimizer honest and detects wrong answers
};

// A workload prepares state once, then exposes one iteration of work.
class Workload {
 public:
  virtual ~Workload() = default;
  virtual std::string name() const = 0;
  virtual void setup(uint64_t seed) = 0;
  // Executes one iteration; returns a value folded into the checksum.
  virtual double iterate(int i) = 0;
  virtual std::map<std::string, double> params() const = 0;
};

RunResult run(Workload& w, const RunConfig& cfg);

double percentile(std::vector<int64_t> v, double p);

// Busy-wait for ns nanoseconds (sleeping would hand the core away and the
// "slowdown" would show up as scheduler noise rather than CPU time).
void spin_for_ns(int64_t ns);

std::string to_json(const RunResult& r, const RunConfig& cfg,
                    const std::string& commit);

SlowdownMode parse_slowdown_mode(const std::string& s);

}  // namespace bench
