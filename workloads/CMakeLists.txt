cmake_minimum_required(VERSION 3.16)
project(benchgate_workloads CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
# -O2 and no -march=native: results must be comparable across bench nodes,
# so the binary must not change with the host it happened to be built on.
set(CMAKE_CXX_FLAGS_RELEASE "-O2 -g -fno-omit-frame-pointer")
add_compile_options(-Wall -Wextra)

add_library(benchcore STATIC
  src/harness.cpp
  src/pointcloud.cpp
  src/planner.cpp
  src/matrix.cpp)
target_include_directories(benchcore PUBLIC include)

add_executable(benchgate_workloads src/main.cpp)
target_link_libraries(benchgate_workloads PRIVATE benchcore)

add_executable(trace_parser tools/trace_parser.cpp)

enable_testing()
add_executable(workload_tests tests/workload_tests.cpp)
target_link_libraries(workload_tests PRIVATE benchcore)
add_test(NAME workload_tests COMMAND workload_tests)
add_test(NAME trace_parser_tests
  COMMAND ${CMAKE_COMMAND} -DPARSER=$<TARGET_FILE:trace_parser>
          -DDATA=${CMAKE_CURRENT_SOURCE_DIR}/tests/data
          -P ${CMAKE_CURRENT_SOURCE_DIR}/tests/run_parser_tests.cmake)
