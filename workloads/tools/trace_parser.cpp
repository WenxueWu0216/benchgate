// trace_parser: streaming converter from raw profiler output to JSONL rows.
//
// Reads stdin line by line (constant memory apart from per-pid state), so it
// can sit at the end of a pipe from `perf script` on multi-GB traces.
//
//   perf stat -x, -I 100 -e ... 2>&1 | trace_parser --format perf-stat
//   perf script -F comm,pid,cpu,time,event,trace | trace_parser --format perf-sched --pid 1234
//   bpftrace runqlat.bt | trace_parser --format bpftrace-hist
//
// Output: one JSON object per line. Every row has a "kind" field.
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace {

std::string_view trim(std::string_view s) {
  while (!s.empty() && std::isspace(static_cast<unsigned char>(s.front()))) s.remove_prefix(1);
  while (!s.empty() && std::isspace(static_cast<unsigned char>(s.back()))) s.remove_suffix(1);
  return s;
}

std::vector<std::string_view> split(std::string_view s, char sep) {
  std::vector<std::string_view> out;
  size_t start = 0;
  for (size_t i = 0; i <= s.size(); ++i) {
    if (i == s.size() || s[i] == sep) {
      out.push_back(s.substr(start, i - start));
      start = i + 1;
    }
  }
  return out;
}

bool to_double(std::string_view s, double* out) {
  s = trim(s);
  if (s.empty()) return false;
  std::string tmp(s);
  char* end = nullptr;
  *out = std::strtod(tmp.c_str(), &end);
  return end && *end == '\0';
}

std::string esc(std::string_view s) {
  std::string o;
  o.reserve(s.size());
  for (char c : s) {
    if (c == '"' || c == '\\') { o += '\\'; o += c; }
    else if (static_cast<unsigned char>(c) >= 0x20) o += c;
  }
  return o;
}

// ---------------------------------------------------------------- perf stat
// CSV from `perf stat -x,` : value,unit,event,run_time,run_pct[,metric,unit]
// With -I the first column is the interval timestamp.
struct PerfStatStats { long rows = 0, skipped = 0, not_counted = 0; };

void parse_perf_stat(std::istream& in, std::ostream& out, PerfStatStats& st) {
  std::string line;
  while (std::getline(in, line)) {
    std::string_view l = trim(line);
    if (l.empty() || l[0] == '#') continue;
    auto f = split(l, ',');
    if (f.size() < 3) { ++st.skipped; continue; }
    size_t base = 0;
    double t = NAN, probe;
    // Interval mode: first field is a float timestamp AND the third field is
    // not an event name only when there are enough columns.
    if (f.size() >= 6 && to_double(f[0], &probe) && !trim(f[3]).empty() &&
        !std::isdigit(static_cast<unsigned char>(trim(f[3])[0]))) {
      t = probe;
      base = 1;
    }
    const std::string_view val = trim(f[base]);
    const std::string_view unit = trim(f[base + 1]);
    const std::string_view event = trim(f[base + 2]);
    if (event.empty()) { ++st.skipped; continue; }
    out << "{\"kind\":\"counter\"";
    if (!std::isnan(t)) out << ",\"t\":" << t;
    out << ",\"event\":\"" << esc(event) << "\"";
    double v;
    if (to_double(val, &v)) {
      out << ",\"value\":" << v;
    } else {
      // "<not counted>" / "<not supported>": keep the row so the consumer
      // can tell "zero" from "unavailable".
      out << ",\"value\":null,\"status\":\"" << esc(val) << "\"";
      ++st.not_counted;
    }
    if (!unit.empty()) out << ",\"unit\":\"" << esc(unit) << "\"";
    double pct;
    if (f.size() > base + 4 && to_double(f[base + 4], &pct))
      out << ",\"run_pct\":" << pct;
    out << "}\n";
    ++st.rows;
  }
}

// --------------------------------------------------------------- perf sched
// `perf script` lines for sched:sched_switch, e.g.
//   bench 1234 [002] 5678.123456: sched:sched_switch: prev_comm=bench prev_pid=1234
//        prev_prio=120 prev_state=R ==> next_comm=kworker/2:1 next_pid=77 next_prio=120
// We track when the target pid leaves a CPU and when it comes back, and emit
// one "offcpu" row per gap. A gap with prev_state=R means the task was
// runnable but preempted: that is scheduling jitter, not the program sleeping.
struct SchedStats {
  long switches = 0, offcpu = 0, preempted = 0, migrations = 0;
  double preempted_ns_total = 0, max_preempt_ns = 0;
};

std::string_view field_value(std::string_view l, std::string_view key) {
  size_t p = 0;
  while ((p = l.find(key, p)) != std::string_view::npos) {
    if (p == 0 || l[p - 1] == ' ') {
      size_t s = p + key.size();
      size_t e = l.find(' ', s);
      return l.substr(s, e == std::string_view::npos ? l.size() - s : e - s);
    }
    p += key.size();
  }
  return {};
}

void parse_perf_sched(std::istream& in, std::ostream& out, long target_pid,
                      SchedStats& st) {
  struct Out { double t; int cpu; bool runnable; std::string by; };
  std::unordered_map<long, Out> away;      // pid -> when it left the CPU
  std::unordered_map<long, int> last_cpu;  // pid -> last CPU it ran on
  std::string line;
  while (std::getline(in, line)) {
    std::string_view l = line;
    const size_t ev = l.find("sched:sched_switch:");
    if (ev == std::string_view::npos) continue;
    // Header: "<comm> <pid> [cpu] <time>:" — comm may contain spaces, so
    // parse from the right of the header.
    std::string_view head = trim(l.substr(0, ev));
    if (!head.empty() && head.back() == ':') head.remove_suffix(1);
    const size_t sp_t = head.rfind(' ');
    if (sp_t == std::string_view::npos) continue;
    double t;
    if (!to_double(head.substr(sp_t + 1), &t)) continue;
    const size_t lb = head.rfind('['), rb = head.rfind(']');
    if (lb == std::string_view::npos || rb == std::string_view::npos || rb < lb) continue;
    const int cpu = std::atoi(std::string(head.substr(lb + 1, rb - lb - 1)).c_str());
    ++st.switches;

    std::string_view body = l.substr(ev);
    const long prev_pid = std::atol(std::string(field_value(body, "prev_pid=")).c_str());
    const long next_pid = std::atol(std::string(field_value(body, "next_pid=")).c_str());
    const std::string_view prev_state = field_value(body, "prev_state=");
    const std::string_view next_comm = field_value(body, "next_comm=");

    auto tracked = [&](long pid) { return pid != 0 && (target_pid < 0 || pid == target_pid); };

    if (tracked(prev_pid)) {
      const bool runnable = !prev_state.empty() && (prev_state[0] == 'R');
      away[prev_pid] = Out{t, cpu, runnable, std::string(next_comm)};
    }
    if (tracked(next_pid)) {
      auto it = away.find(next_pid);
      if (it != away.end()) {
        const double dur_ns = (t - it->second.t) * 1e9;
        out << "{\"kind\":\"offcpu\",\"pid\":" << next_pid
            << ",\"t\":" << it->second.t << ",\"dur_ns\":" << std::llround(dur_ns)
            << ",\"cpu_out\":" << it->second.cpu << ",\"cpu_in\":" << cpu
            << ",\"preempted\":" << (it->second.runnable ? "true" : "false")
            << ",\"by\":\"" << esc(it->second.by) << "\"}\n";
        ++st.offcpu;
        if (it->second.runnable) {
          ++st.preempted;
          st.preempted_ns_total += dur_ns;
          if (dur_ns > st.max_preempt_ns) st.max_preempt_ns = dur_ns;
        }
        away.erase(it);
      }
      auto lc = last_cpu.find(next_pid);
      if (lc != last_cpu.end() && lc->second != cpu) {
        ++st.migrations;
        out << "{\"kind\":\"migration\",\"pid\":" << next_pid << ",\"t\":" << t
            << ",\"from\":" << lc->second << ",\"to\":" << cpu << "}\n";
      }
      last_cpu[next_pid] = cpu;
    }
  }
}

// ------------------------------------------------------------ bpftrace hist
// @usecs:
// [0]                    5 |@@@@                       |
// [2, 4)                20 |@@@@@@@@@@@@@@@@@@@@@@@@   |
// [1K, 2K)               3 |@@                         |
double parse_size(std::string_view s) {
  s = trim(s);
  double mult = 1;
  if (!s.empty()) {
    switch (s.back()) {
      case 'K': mult = 1024.0; break;
      case 'M': mult = 1024.0 * 1024; break;
      case 'G': mult = 1024.0 * 1024 * 1024; break;
      default: break;
    }
    if (mult != 1) s.remove_suffix(1);
  }
  double v = 0;
  to_double(s, &v);
  return v * mult;
}

struct HistStats { long buckets = 0, maps = 0; };

void parse_bpftrace_hist(std::istream& in, std::ostream& out, HistStats& st) {
  std::string map = "@";
  std::string line;
  while (std::getline(in, line)) {
    std::string_view l = trim(line);
    if (l.empty()) continue;
    if (l[0] == '@' && l.back() == ':') {
      map = std::string(l.substr(0, l.size() - 1));
      ++st.maps;
      continue;
    }
    if (l[0] != '[') continue;
    const size_t close = l.find_first_of(")]");
    if (close == std::string_view::npos) continue;
    std::string_view range = l.substr(1, close - 1);
    double lo, hi;
    const size_t comma = range.find(',');
    if (comma == std::string_view::npos) {
      lo = parse_size(range);
      hi = lo + 1;  // single-value bucket [0], [1]
    } else {
      lo = parse_size(range.substr(0, comma));
      hi = parse_size(range.substr(comma + 1));
    }
    std::string_view rest = trim(l.substr(close + 1));
    const size_t bar = rest.find('|');
    double count = 0;
    if (!to_double(bar == std::string_view::npos ? rest : rest.substr(0, bar), &count))
      continue;
    out << "{\"kind\":\"hist\",\"map\":\"" << esc(map) << "\",\"lo\":" << lo
        << ",\"hi\":" << hi << ",\"count\":" << static_cast<long long>(count) << "}\n";
    ++st.buckets;
  }
}

void usage() {
  std::fprintf(stderr,
      "usage: trace_parser --format perf-stat|perf-sched|bpftrace-hist [--pid PID]\n"
      "  reads stdin, writes JSONL rows to stdout and a summary row last\n");
}

}  // namespace

int main(int argc, char** argv) {
  std::ios::sync_with_stdio(false);
  std::string fmt;
  long pid = -1;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--format" && i + 1 < argc) fmt = argv[++i];
    else if (a == "--pid" && i + 1 < argc) pid = std::atol(argv[++i]);
    else { usage(); return 2; }
  }
  auto& in = std::cin;
  auto& out = std::cout;
  out.precision(12);
  if (fmt == "perf-stat") {
    PerfStatStats st;
    parse_perf_stat(in, out, st);
    out << "{\"kind\":\"summary\",\"format\":\"perf-stat\",\"rows\":" << st.rows
        << ",\"not_counted\":" << st.not_counted << ",\"skipped\":" << st.skipped << "}\n";
  } else if (fmt == "perf-sched") {
    SchedStats st;
    parse_perf_sched(in, out, pid, st);
    out << "{\"kind\":\"summary\",\"format\":\"perf-sched\",\"switches\":" << st.switches
        << ",\"offcpu\":" << st.offcpu << ",\"preempted\":" << st.preempted
        << ",\"migrations\":" << st.migrations
        << ",\"preempted_ns_total\":" << std::llround(st.preempted_ns_total)
        << ",\"max_preempt_ns\":" << std::llround(st.max_preempt_ns) << "}\n";
  } else if (fmt == "bpftrace-hist") {
    HistStats st;
    parse_bpftrace_hist(in, out, st);
    out << "{\"kind\":\"summary\",\"format\":\"bpftrace-hist\",\"maps\":" << st.maps
        << ",\"buckets\":" << st.buckets << "}\n";
  } else {
    usage();
    return 2;
  }
  return 0;
}
