#!/usr/bin/env bash
# Launch after launch: does sys.modules hold a freed entry once Glyphs has
# loaded its plugins? Stops at the first finding or GC crash.
S=/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad
N=${1:-40}; P=${2:-B}
cd /Users/mirkovelimirovic/Desktop/KinetiKern/Kinetikern2/notes/crash-scripts
for i in $(seq 1 "$N"); do
  L=$(printf "%s%02d" "$P" "$i")
  OUT=$(TIMEOUT=180 DEVSCRIPT=$S/crash/startscan.py bash run_verify.sh "$L" /System/Library/Fonts/Supplemental/Arial.ttf 2>&1 | tail -1)
  R=$(head -1 "$S/crash/runs/$L/startscan.txt" 2>/dev/null)
  echo "$(date +%T) $L ${OUT#* } | ${R:-no scan result}"
  if grep -q FOUND "$S/crash/runs/$L/startscan.txt" 2>/dev/null; then cat "$S/crash/runs/$L/startscan.txt"; break; fi
  if echo "$OUT" | grep -q "newcrash=\[[^]]*ips"; then
    python3 /Users/mirkovelimirovic/Desktop/KinetiKern/Kinetikern2/tools/kk2_crash_report.py "$S/crash/runs/$L/crash_before.txt" "Glyphs 3" | head -6
  fi
done
echo "loop done"
