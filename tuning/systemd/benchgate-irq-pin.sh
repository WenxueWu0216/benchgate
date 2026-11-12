#!/usr/bin/env bash
# Move every movable IRQ onto the housekeeping CPUs. Re-run periodically
# (timer) because hot-plugged devices and drivers can reset affinities.
set -uo pipefail
[[ -f /etc/default/benchgate-tune ]] && . /etc/default/benchgate-tune
: "${HOUSEKEEPING_CPUS:=0-1}"
moved=0; fixed=0
echo "$HOUSEKEEPING_CPUS" > /proc/irq/default_smp_affinity_list 2>/dev/null || true
for irq in /proc/irq/[0-9]*; do
  f=$irq/smp_affinity_list
  [[ -w $f ]] || continue
  cur=$(cat "$f")
  if [[ $cur != "$HOUSEKEEPING_CPUS" ]]; then
    if echo "$HOUSEKEEPING_CPUS" > "$f" 2>/dev/null; then moved=$((moved+1)); else fixed=$((fixed+1)); fi
  fi
done
echo "IRQs moved to CPUs $HOUSEKEEPING_CPUS: $moved (unmovable/per-CPU: $fixed)"
