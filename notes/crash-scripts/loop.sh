#!/usr/bin/env bash
# loop.sh <prefix> <count> <mode: plain|gc> [open env args...]
PREFIX="$1"; COUNT="$2"; MODE="$3"; shift 3
D=/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad/crash
LATO=/Users/mirkovelimirovic/Documents/Github/fonts/ofl/lato/Lato-Regular.ttf
ARIAL=/System/Library/Fonts/Supplemental/Arial.ttf
for i in $(seq 1 "$COUNT"); do
  if [ $((i % 2)) = 1 ]; then F="$LATO"; N=lato; else F="$ARIAL"; N=arial; fi
  [ -n "${FONTONLY:-}" ] && { F="$FONTONLY"; N=only; }
  if [ "$MODE" = gc ]; then
    WRAPSCRIPT="$D/gcstress.py" "$D/run_verify.sh" "${PREFIX}_${i}_$N" "$F" "$@"
  elif [ "$MODE" = wrap ]; then
    WRAPSCRIPT="$WRAP" "$D/run_verify.sh" "${PREFIX}_${i}_$N" "$F" "$@"
  else
    "$D/run_verify.sh" "${PREFIX}_${i}_$N" "$F" "$@"
  fi
  if grep -q "newcrash=\[[^]]" "$D/runs/${PREFIX}_${i}_$N/outcome.txt" || ! grep -q "outcome=report" "$D/runs/${PREFIX}_${i}_$N/outcome.txt"; then
    echo "STOP at $i"; break
  fi
done
