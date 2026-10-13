#!/usr/bin/env bash
# usage: [HOLD=a,b] [HAMMER=0] aliasloop.sh N PREFIX
# Launches a temporary Glyphs with the startup watcher (CONTINUE mode) and aliascheck.py as the dev script.
S=/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad
N=${1:-1}; P=${2:-A}
cd /Users/mirkovelimirovic/Desktop/KinetiKern/Kinetikern2/notes/crash-scripts
for i in $(seq 1 "$N"); do
  L=$(printf "%s%02d" "$P" "$i")
  OUT=$(TIMEOUT=180 DEVSCRIPT=$S/crash/aliascheck.py bash run_verify.sh "$L" /System/Library/Fonts/Supplemental/Arial.ttf \
        --env "PYTHONPATH=$S/crash/site" --env "KK2_SITE_OUT=$S/crash/runs/$L" --env "KK2_SITE_HAMMER=${HAMMER:-0}" \
        --env "KK2_SITE_CONTINUE=1" --env "KK2_SITE_HOLD=${HOLD:-}" --env "KK2_SITE_CHECK_AT=${CHECK_AT:-15}" 2>&1 | tail -1)
  echo "$(date +%T) $L ${OUT#* }"
done
echo "loop done"
