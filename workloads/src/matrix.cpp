#include <cmath>
#include <random>
#include <vector>

#include "bench/workloads.hpp"

namespace bench {
namespace {

// Shape of a Kalman measurement update: S = H P H^T + R, then solve S x = b
// with a Cholesky factorization.
class MatrixWorkload : public Workload {
 public:
  explicit MatrixWorkload(int n) : n_(n) {}
  std::string name() const override { return "matrix"; }

  void setup(uint64_t seed) override {
    std::mt19937_64 rng(seed);
    std::normal_distribution<double> nd(0.0, 1.0);
    const size_t nn = static_cast<size_t>(n_) * n_;
    H_.resize(nn); P_.resize(nn); T_.resize(nn); S_.resize(nn); L_.resize(nn);
    b_.resize(n_); x_.resize(n_);
    for (auto& v : H_) v = nd(rng) / std::sqrt(n_);
    // P = A A^T + I is SPD.
    std::vector<double> A(nn);
    for (auto& v : A) v = nd(rng) / std::sqrt(n_);
    gemm(A.data(), A.data(), P_.data(), /*bt=*/true);
    for (int i = 0; i < n_; ++i) P_[i * n_ + i] += 1.0;
    for (auto& v : b_) v = nd(rng);
  }

  double iterate(int i) override {
    gemm(H_.data(), P_.data(), T_.data(), false);    // T = H P
    gemm(T_.data(), H_.data(), S_.data(), true);     // S = T H^T
    for (int k = 0; k < n_; ++k) S_[k * n_ + k] += 0.5 + 1e-3 * (i % 7);  // + R
    cholesky();
    solve();
    double s = 0;
    for (double v : x_) s += v;
    return s;
  }

  std::map<std::string, double> params() const override {
    return {{"n", static_cast<double>(n_)}};
  }

 private:
  // C = A * B (or A * B^T), cache-friendly i-k-j ordering.
  void gemm(const double* A, const double* B, double* C, bool bt) {
    const int n = n_;
    std::fill(C, C + static_cast<size_t>(n) * n, 0.0);
    if (!bt) {
      for (int i = 0; i < n; ++i)
        for (int k = 0; k < n; ++k) {
          const double a = A[i * n + k];
          for (int j = 0; j < n; ++j) C[i * n + j] += a * B[k * n + j];
        }
    } else {
      for (int i = 0; i < n; ++i)
        for (int j = 0; j < n; ++j) {
          double s = 0;
          for (int k = 0; k < n; ++k) s += A[i * n + k] * B[j * n + k];
          C[i * n + j] = s;
        }
    }
  }

  void cholesky() {
    const int n = n_;
    std::fill(L_.begin(), L_.end(), 0.0);
    for (int j = 0; j < n; ++j) {
      double d = S_[j * n + j];
      for (int k = 0; k < j; ++k) d -= L_[j * n + k] * L_[j * n + k];
      const double ljj = std::sqrt(d);
      L_[j * n + j] = ljj;
      for (int i = j + 1; i < n; ++i) {
        double s = S_[i * n + j];
        for (int k = 0; k < j; ++k) s -= L_[i * n + k] * L_[j * n + k];
        L_[i * n + j] = s / ljj;
      }
    }
  }

  void solve() {
    const int n = n_;
    std::vector<double>& y = x_;
    for (int i = 0; i < n; ++i) {  // L y = b
      double s = b_[i];
      for (int k = 0; k < i; ++k) s -= L_[i * n + k] * y[k];
      y[i] = s / L_[i * n + i];
    }
    for (int i = n - 1; i >= 0; --i) {  // L^T x = y
      double s = y[i];
      for (int k = i + 1; k < n; ++k) s -= L_[k * n + i] * x_[k];
      x_[i] = s / L_[i * n + i];
    }
  }

  int n_;
  std::vector<double> H_, P_, T_, S_, L_, b_, x_;
};

}  // namespace

std::unique_ptr<Workload> make_matrix(int n) {
  return std::make_unique<MatrixWorkload>(n);
}

std::unique_ptr<Workload> make_workload(const std::string& name) {
  if (name == "pointcloud") return make_pointcloud();
  if (name == "planner") return make_planner();
  if (name == "matrix") return make_matrix();
  return nullptr;
}

std::vector<std::string> workload_names() {
  return {"pointcloud", "planner", "matrix"};
}

}  // namespace bench
