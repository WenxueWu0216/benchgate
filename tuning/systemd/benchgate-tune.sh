#!/usr/bin/env bash
# Runtime tuning that does not need a reboot. Idempotent; run at boot by
# benchgate-tune.service. Every knob is optional: missing sysfs files (VMs,
# AMD vs Intel) are skipped with a message rather than failing the unit.
set -uo pipefail
[[ -f /etc/default/benchgate-tune ]] && . /etc/default/benchgate-tune
: "${CPU_GOVERNOR:=performance}" "${DISABLE_TURBO:=1}" "${DISABLE_SMT:=1}"
: "${DISABLE_THP:=1}" "${DISABLE_NUMA_BALANCING:=1}" "${DISABLE_ASLR:=0}"

# "0-1,4" -> hex CPU mask
mask() {
  local m=0 part a b
  IFS=, read -ra parts <<< "$1"
  for part in "${parts[@]}"; do
    a=${part%-*}; b=${part#*-}
    for ((c=a; c<=b; c++)); do m=$(( m | (1 << c) )); done
  done
  printf '%x' "$m"
}

w() { if [[ -w "$1" ]]; then echo "$2" > "$1" && echo "set $1=$2"; else echo "skip $1 (absent)"; fi; }

# Fixed frequency: performance governor on every CPU.
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do w "$g" "$CPU_GOVERNOR"; done
# Pin min=max so the governor has nothing to decide.
for d in /sys/devices/system/cpu/cpu*/cpufreq; do
  [[ -r $d/cpuinfo_max_freq ]] || continue
  base=$(cat "$d/base_frequency" 2>/dev/null || cat "$d/cpuinfo_max_freq")
  w "$d/scaling_max_freq" "$base"; w "$d/scaling_min_freq" "$base"
done
if [[ $DISABLE_TURBO == 1 ]]; then
  w /sys/devices/system/cpu/intel_pstate/no_turbo 1      # Intel
  w /sys/devices/system/cpu/cpufreq/boost 0              # AMD / acpi-cpufreq
fi
[[ $DISABLE_SMT == 1 ]] && w /sys/devices/system/cpu/smt/control off
if [[ $DISABLE_THP == 1 ]]; then
  w /sys/kernel/mm/transparent_hugepage/enabled never
  w /sys/kernel/mm/transparent_hugepage/defrag never
fi
[[ $DISABLE_NUMA_BALANCING == 1 ]] && w /proc/sys/kernel/numa_balancing 0
[[ $DISABLE_ASLR == 1 ]] && w /proc/sys/kernel/randomize_va_space 0
# Keep the kernel's periodic work off the bench cores.
w /proc/sys/kernel/watchdog 0
: "${HOUSEKEEPING_CPUS:=0-1}"
w /sys/bus/workqueue/devices/writeback/cpumask "$(mask "$HOUSEKEEPING_CPUS")"
w /sys/devices/virtual/workqueue/cpumask "$(mask "$HOUSEKEEPING_CPUS")"
w /proc/sys/vm/stat_interval 120
exit 0
