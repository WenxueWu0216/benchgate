#include <cmath>
#include <cstdint>
#include <limits>
#include <queue>
#include <random>
#include <vector>

#include "bench/workloads.hpp"

namespace bench {
namespace {

class PlannerWorkload : public Workload {
 public:
  explicit PlannerWorkload(int g) : g_(g) {}
  std::string name() const override { return "planner"; }

  void setup(uint64_t seed) override {
    rng_.seed(seed);
    grid_.assign(static_cast<size_t>(g_) * g_, 0);
    std::uniform_int_distribution<int> pos(0, g_ - 1), len(3, 20);
    std::bernoulli_distribution horiz(0.5);
    // Random wall segments: ~ an urban occupancy grid.
    for (int k = 0; k < g_ * 2; ++k) {
      int x = pos(rng_), y = pos(rng_), l = len(rng_);
      const bool h = horiz(rng_);
      for (int j = 0; j < l; ++j) {
        const int cx = h ? x + j : x, cy = h ? y : y + j;
        if (cx < g_ && cy < g_) grid_[cy * g_ + cx] = 1;
      }
    }
    // Pre-draw queries so every run solves the same problems in the same order.
    queries_.clear();
    for (int q = 0; q < 256; ++q) {
      int s, t;
      do { s = pos(rng_) * g_ + pos(rng_); } while (grid_[s]);
      do { t = pos(rng_) * g_ + pos(rng_); } while (grid_[t]);
      queries_.push_back({s, t});
    }
    cost_.resize(grid_.size());
    closed_.resize(grid_.size());
  }

  double iterate(int i) override {
    const auto [start, goal] = queries_[i % queries_.size()];
    std::fill(cost_.begin(), cost_.end(), std::numeric_limits<float>::max());
    std::fill(closed_.begin(), closed_.end(), 0);
    const int gx = goal % g_, gy = goal / g_;
    auto h = [&](int c) {
      const float dx = static_cast<float>(c % g_ - gx);
      const float dy = static_cast<float>(c / g_ - gy);
      return std::sqrt(dx * dx + dy * dy);
    };
    using Node = std::pair<float, int>;  // (f, cell)
    std::priority_queue<Node, std::vector<Node>, std::greater<Node>> open;
    cost_[start] = 0.f;
    open.push({h(start), start});
    static const int dx[8] = {1, -1, 0, 0, 1, 1, -1, -1};
    static const int dy[8] = {0, 0, 1, -1, 1, -1, 1, -1};
    static const float dc[8] = {1, 1, 1, 1, 1.41421356f, 1.41421356f,
                                1.41421356f, 1.41421356f};
    int expanded = 0;
    while (!open.empty()) {
      const auto [f, c] = open.top();
      open.pop();
      if (closed_[c]) continue;
      closed_[c] = 1;
      ++expanded;
      if (c == goal) break;
      const int cx = c % g_, cy = c / g_;
      for (int k = 0; k < 8; ++k) {
        const int nx = cx + dx[k], ny = cy + dy[k];
        if (nx < 0 || ny < 0 || nx >= g_ || ny >= g_) continue;
        const int n = ny * g_ + nx;
        if (grid_[n] || closed_[n]) continue;
        const float nc = cost_[c] + dc[k];
        if (nc < cost_[n]) {
          cost_[n] = nc;
          open.push({nc + h(n), n});
        }
      }
    }
    const float path = cost_[goal];
    return (path == std::numeric_limits<float>::max() ? -1.0 : path) +
           expanded * 1e-6;
  }

  std::map<std::string, double> params() const override {
    return {{"grid", static_cast<double>(g_)}};
  }

 private:
  int g_;
  std::mt19937_64 rng_;
  std::vector<uint8_t> grid_;
  std::vector<std::pair<int, int>> queries_;
  std::vector<float> cost_;
  std::vector<uint8_t> closed_;
};

}  // namespace

std::unique_ptr<Workload> make_planner(int g) {
  return std::make_unique<PlannerWorkload>(g);
}

}  // namespace bench
