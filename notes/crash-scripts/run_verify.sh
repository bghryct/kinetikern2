#!/usr/bin/env bash
# usage: run_verify.sh <label> <font> [extra open args (e.g. --env X=Y) ...] [-- extra --args pairs]
# Reproduces build.sh --verify's open command (same argument-domain keys), plus
# environment variables and stdout/stderr capture. Starts exactly one temporary
# instance; waits for selftest.json or for that instance to exit.
set -uo pipefail
LABEL="$1"; FONT="$2"; shift 2
OPENARGS=(); EXTRA=()
while [ $# -gt 0 ]; do
  if [ "$1" = "--" ]; then shift; EXTRA=("$@"); break; fi
  OPENARGS+=("$1"); shift
done
BASE=/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad/crash/runs
KEY="com.mirkovelimirovic.Kinetikern2"
GLYPHS_APP="/Applications/Glyphs 3.app"
RESULTS="$BASE/$LABEL"
rm -rf "$RESULTS"; mkdir -p "$RESULTS"
REPORT="$RESULTS/selftest.json"
BUSY="$(pgrep -f "$KEY\.(selfTestFont|devScript)" || true)"
if [ -n "$BUSY" ]; then echo "BUSY: $BUSY"; exit 3; fi
pgrep -lf "Glyphs [34].app/Contents/MacOS" > "$RESULTS/pgrep_before.txt"
ls ~/Library/Logs/DiagnosticReports/ | grep "^Glyphs 3-" | sort > "$RESULTS/crash_before.txt"
PATTERN="$KEY\.selfTestOut $(printf '%s' "$RESULTS" | sed 's/[][\.*^$?+(){}|]/\\&/g')"
date +%s > "$RESULTS/start"
if [ -n "${DEVSCRIPT:-}" ]; then
  open -n -a "$GLYPHS_APP" ${OPENARGS[@]+"${OPENARGS[@]}"} --stdout "$RESULTS/stdout.txt" --stderr "$RESULTS/stderr.txt" --args -ApplePersistenceIgnoreState YES \
    "-$KEY.devScript" "$DEVSCRIPT" \
    "-$KEY.selfTestOut" "$RESULTS" \
    "-$KEY.devFont" "$FONT" ${EXTRA[@]+"${EXTRA[@]}"}
elif [ -n "${WRAPSCRIPT:-}" ]; then
  open -n -a "$GLYPHS_APP" ${OPENARGS[@]+"${OPENARGS[@]}"} --stdout "$RESULTS/stdout.txt" --stderr "$RESULTS/stderr.txt" --args -ApplePersistenceIgnoreState YES \
    "-$KEY.devScript" "$WRAPSCRIPT" \
    "-$KEY.selfTestFont" "$FONT" \
    "-$KEY.selfTestOut" "$RESULTS" \
    "-$KEY.selfTestQuit" YES \
    "-$KEY.selfTestWhole" YES \
    "-$KEY.selfTestCancel" YES ${EXTRA[@]+"${EXTRA[@]}"}
else
  open -n -a "$GLYPHS_APP" ${OPENARGS[@]+"${OPENARGS[@]}"} --stdout "$RESULTS/stdout.txt" --stderr "$RESULTS/stderr.txt" --args -ApplePersistenceIgnoreState YES \
    "-$KEY.selfTestFont" "$FONT" \
    "-$KEY.selfTestOut" "$RESULTS" \
    "-$KEY.selfTestQuit" YES \
    "-$KEY.selfTestWhole" YES \
    "-$KEY.selfTestCancel" YES ${EXTRA[@]+"${EXTRA[@]}"}
fi
START=$(date +%s); PID=""
TIMEOUT=${TIMEOUT:-900}
OUTCOME=""
while :; do
  ELAPSED=$(( $(date +%s) - START ))
  [ -n "$PID" ] || PID="$(pgrep -f "$PATTERN" | head -n 1 || true)"
  if [ -f "$REPORT" ] || [ -f "$RESULTS/done" ]; then OUTCOME="report"; break; fi
  if [ -n "$PID" ] && ! kill -0 "$PID" 2>/dev/null; then OUTCOME="exited"; break; fi
  if [ "$ELAPSED" -ge "$TIMEOUT" ]; then OUTCOME="timeout"; break; fi
  if [ -z "$PID" ] && [ "$ELAPSED" -ge 90 ]; then OUTCOME="nostart"; break; fi
  sleep 2
done
echo "$PID" > "$RESULTS/pid"
if [ "$OUTCOME" = "report" ] && [ -n "$PID" ]; then
  for _ in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  if kill -0 "$PID" 2>/dev/null; then echo "own instance $PID did not quit; TERM" ; kill -TERM "$PID"; sleep 3; fi
fi
if [ "$OUTCOME" = "timeout" ] && [ -n "$PID" ]; then kill -TERM "$PID" 2>/dev/null; sleep 5; kill -0 "$PID" 2>/dev/null && kill -KILL "$PID"; fi
sleep 3
if [ "$OUTCOME" = "exited" ]; then
  for _ in $(seq 1 30); do
    ls ~/Library/Logs/DiagnosticReports/ | grep "^Glyphs 3-" | sort > "$RESULTS/crash_after.txt"
    [ -n "$(comm -13 "$RESULTS/crash_before.txt" "$RESULTS/crash_after.txt")" ] && break
    sleep 2
  done
fi
END=$(date +%s)
pgrep -lf "Glyphs [34].app/Contents/MacOS" > "$RESULTS/pgrep_after.txt"
ls ~/Library/Logs/DiagnosticReports/ | grep "^Glyphs 3-" | sort > "$RESULTS/crash_after.txt"
NEWCRASH="$(comm -13 "$RESULTS/crash_before.txt" "$RESULTS/crash_after.txt" | tr '\n' ' ')"
OK="-"
if [ -f "$REPORT" ]; then OK="$(/usr/bin/python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print("PASS" if r.get("ok") else "FAIL:"+str(r.get("errors"))[:300])' "$REPORT")"; fi
echo "$LABEL outcome=$OUTCOME pid=$PID secs=$((END-START)) ok=$OK newcrash=[$NEWCRASH]" | tee "$RESULTS/outcome.txt"
