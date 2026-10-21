// Workloads that mimic the hot loops of an autonomy stack.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include "bench/harness.hpp"

namespace bench {

// Perception: crop box -> ground removal -> voxel-grid downsample on a
// synthetic LiDAR sweep. Hash-map heavy, branchy, memory-bound.
std::unique_ptr<Workload> make_pointcloud(int num_points = 60000);

// Planning: A* on an occupancy grid with a binary-heap priority queue.
// Pointer chasing + unpredictable branches.
std::unique_ptr<Workload> make_planner(int grid = 160);

// Estimation: dense matmul + Cholesky solve, the shape of a Kalman update.
// Compute-bound, FP heavy.
std::unique_ptr<Workload> make_matrix(int n = 96);

std::unique_ptr<Workload> make_workload(const std::string& name);
std::vector<std::string> workload_names();

}  // namespace bench
