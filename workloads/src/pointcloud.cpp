#include <cmath>
#include <cstdint>
#include <random>
#include <unordered_map>
#include <vector>

#include "bench/workloads.hpp"

namespace bench {
namespace {

struct Point { float x, y, z, intensity; };

class PointCloudWorkload : public Workload {
 public:
  explicit PointCloudWorkload(int n) : n_(n) {}
  std::string name() const override { return "pointcloud"; }

  void setup(uint64_t seed) override {
    std::mt19937_64 rng(seed);
    // A synthetic spinning-LiDAR sweep: 32 rings, mostly ground returns plus
    // box-shaped "vehicles" and uniform clutter.
    std::uniform_real_distribution<float> az(0.f, 2.f * static_cast<float>(M_PI));
    std::uniform_real_distribution<float> range(2.f, 80.f);
    std::normal_distribution<float> ground_noise(0.f, 0.03f);
    std::uniform_real_distribution<float> u(0.f, 1.f);
    frames_.resize(kFrames);
    for (auto& f : frames_) {
      f.resize(n_);
      for (auto& p : f) {
        const float a = az(rng), r = range(rng);
        p.x = r * std::cos(a);
        p.y = r * std::sin(a);
        const float kind = u(rng);
        if (kind < 0.6f) p.z = -1.7f + ground_noise(rng);         // ground
        else if (kind < 0.9f) p.z = -1.7f + 1.8f * u(rng);       // objects
        else p.z = -1.7f + 6.f * u(rng);                          // clutter
        p.intensity = u(rng);
      }
    }
    voxels_.reserve(n_);
  }

  double iterate(int i) override {
    const auto& pts = frames_[i % kFrames];
    voxels_.clear();
    out_.clear();
    for (const auto& p : pts) {
      // Crop box (ego-centric ROI) and ground removal.
      if (std::fabs(p.x) > 50.f || std::fabs(p.y) > 50.f) continue;
      if (p.z < -1.5f || p.z > 2.5f) continue;
      // Voxel grid downsample: average points per 0.2 m voxel.
      const int64_t vx = static_cast<int64_t>(std::floor(p.x * 5.f));
      const int64_t vy = static_cast<int64_t>(std::floor(p.y * 5.f));
      const int64_t vz = static_cast<int64_t>(std::floor(p.z * 5.f));
      const int64_t key = (vx * 73856093) ^ (vy * 19349663) ^ (vz * 83492791);
      auto& acc = voxels_[key];
      acc.x += p.x; acc.y += p.y; acc.z += p.z; acc.n += 1;
    }
    double check = 0;
    for (const auto& [k, a] : voxels_) {
      out_.push_back({a.x / a.n, a.y / a.n, a.z / a.n, 0.f});
      check += a.z / a.n;
    }
    return check + static_cast<double>(out_.size());
  }

  std::map<std::string, double> params() const override {
    return {{"num_points", static_cast<double>(n_)}, {"voxel_m", 0.2}};
  }

 private:
  struct Acc { float x = 0, y = 0, z = 0; int n = 0; };
  static constexpr int kFrames = 4;
  int n_;
  std::vector<std::vector<Point>> frames_;
  std::unordered_map<int64_t, Acc> voxels_;
  std::vector<Point> out_;
};

}  // namespace

std::unique_ptr<Workload> make_pointcloud(int n) {
  return std::make_unique<PointCloudWorkload>(n);
}

}  // namespace bench
