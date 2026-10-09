#!/usr/bin/env bash
# Builds libkinetikern2.dylib and puts it into the Kinetikern2 Glyphs plugin bundle.
#
#   ./build.sh                  native build (arm64 on Apple Silicon)
#   ./build.sh --test           run the engine's tests first (cargo test --release)
#   ./build.sh --universal      arm64 + x86_64 (needs: rustup target add x86_64-apple-darwin)
#   ./build.sh --install        also link the bundle into Glyphs 3's Plugins folder
#   ./build.sh --verify [font]  then run the in-Glyphs self-test (kk2_selftest) in a
#                               temporary second Glyphs 3 instance; default font:
#                               /System/Library/Fonts/Supplemental/Arial.ttf
#   ./build.sh --verify --groups  the self-test also runs its spacing-groups stage
#                               (frozen capitals, looser figures, the Pairs window)
#   ./build.sh --verify FONT --connected  and its connected-script stage (FONT must
#                               be a connected script, e.g. an OFL script from Google
#                               Fonts): joins found, join pairs unkerned and overlapping,
#                               a period's distance kept, Apply/Revert, off again
#
# The library is replaced atomically (built into a staging file, signed, then renamed
# over the old one), so a Glyphs or a tool that has the old one loaded keeps it.
# --verify never touches a running Glyphs: it starts its own instance (open -n)
# with the test's parameters in the argument domain, waits up to 10 minutes for
# selftest.json, prints a summary and exits non-zero unless the test passed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
ENGINE="$ROOT/engine"
BUNDLE="$ROOT/plugin/Kinetikern2.glyphsPlugin"
RESOURCES="$BUNDLE/Contents/Resources"
DYLIB="libkinetikern2.dylib"
OUT="$RESOURCES/$DYLIB"
PLUGINS="$HOME/Library/Application Support/Glyphs 3/Plugins"
LINK="$PLUGINS/Kinetikern2.glyphsPlugin"
GLYPHS_APP="${GLYPHS_APP:-/Applications/Glyphs 3.app}"
GPY="$HOME/Library/Application Support/Glyphs 3/Repositories/GlyphsPythonPlugin/Python.framework/Versions/3.11/bin/python3"
KEY="com.mirkovelimirovic.Kinetikern2"
VERIFY_TIMEOUT=600   # seconds for selftest.json to appear
QUIT_TIMEOUT=60      # seconds for the temporary Glyphs to quit afterwards

UNIVERSAL=0
INSTALL=0
TEST=0
VERIFY=0
SPACING_GROUPS=0
CONNECTED=0
FONT="/System/Library/Fonts/Supplemental/Arial.ttf"
while [ $# -gt 0 ]; do
  case "$1" in
    --universal) UNIVERSAL=1 ;;
    --install) INSTALL=1 ;;
    --test) TEST=1 ;;
    --groups) SPACING_GROUPS=1 ;;
    --connected) CONNECTED=1 ;;
    --verify)
      VERIFY=1
      # an optional font path follows (anything not starting with "-")
      if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then FONT="$2"; shift; fi ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

# Glyphs' own Python reads the results; any python3 will do without it.
PY="$GPY"
[ -x "$PY" ] || PY="$(command -v python3 || true)"

if [ "$VERIFY" = 1 ]; then
  # Fail before the build, not ten minutes into the test.
  [ -e "$FONT" ] || { echo "font not found: $FONT" >&2; exit 2; }
  # Glyphs resolves nothing relative, and it hangs opening a file whose path
  # runs through a relative symlink (/tmp -> private/tmp): pass the physical path
  FONT="$(cd "$(dirname "$FONT")" && pwd -P)/$(basename "$FONT")"
  [ -d "$GLYPHS_APP" ] || { echo "Glyphs 3 not found at $GLYPHS_APP (set GLYPHS_APP)" >&2; exit 2; }
  [ -n "$PY" ] || { echo "--verify needs python3 to read the results" >&2; exit 2; }
fi

cd "$ENGINE"
if [ "$TEST" = 1 ]; then
  cargo test --release
fi

# A staging file of this run's own (two builds at once must not write the same
# file) next to the library, so the final rename stays on one volume; it is
# gone however the script ends.
NEW="$(mktemp "$RESOURCES/.$DYLIB.XXXXXX")"
trap 'rm -f "$NEW"' EXIT
if [ "$UNIVERSAL" = 1 ]; then
  # Cross-compiling needs rustup's toolchain with both standard libraries.
  if ! command -v rustup >/dev/null 2>&1; then
    echo "--universal needs rustup (https://rustup.rs)" >&2; exit 1
  fi
  for t in aarch64-apple-darwin x86_64-apple-darwin; do
    if ! rustup target list --installed | grep -qx "$t"; then
      echo "missing Rust target $t — run: rustup target add $t" >&2; exit 1
    fi
  done
  MACOSX_DEPLOYMENT_TARGET=11.0 rustup run stable cargo build --release --target aarch64-apple-darwin
  MACOSX_DEPLOYMENT_TARGET=10.13 rustup run stable cargo build --release --target x86_64-apple-darwin
  lipo -create \
    "target/aarch64-apple-darwin/release/$DYLIB" \
    "target/x86_64-apple-darwin/release/$DYLIB" \
    -output "$NEW"
else
  MACOSX_DEPLOYMENT_TARGET=11.0 cargo build --release
  cp "target/release/$DYLIB" "$NEW"
fi

# ctypes loads the library by path; a neutral install name keeps build paths out
# of the binary. Changing it invalidates the linker signature, so sign again (ad
# hoc is enough: Glyphs disables library validation for plugins). All of this
# happens to the staging file; only the finished file takes the library's name, in
# one rename: a process that has the old library mapped keeps the old inode, and
# nothing ever loads a half-written file.
chmod 755 "$NEW"  # mktemp creates it private
install_name_tool -id "@rpath/$DYLIB" "$NEW"
if ! SIGNED="$(codesign --force --sign - "$NEW" 2>&1)"; then
  echo "$SIGNED" >&2; exit 1
fi
if [ -f "$OUT" ] && cmp -s "$NEW" "$OUT"; then
  rm -f "$NEW"
  echo "unchanged $(lipo -archs "$OUT") → $OUT"
else
  mv -f "$NEW" "$OUT"
  echo "built $(lipo -archs "$OUT") → $OUT"
fi

if [ "$INSTALL" = 1 ]; then
  if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    echo "$LINK exists and is not a link (an installed copy?); leaving it alone." >&2
    echo "Move it to the Trash and run --install again." >&2
    exit 1
  fi
  mkdir -p "$PLUGINS"
  ln -sfn "$BUNDLE" "$LINK"
  echo "linked into $PLUGINS — restart Glyphs 3 to load it"
fi

[ "$VERIFY" = 1 ] || exit 0

# ---------------------------------------------------------------- --verify
# The temporary instance loads its plugins from the Plugins folder: it must load
# this bundle, not an older copy.
if [ ! -L "$LINK" ] || [ "$(cd "$LINK" && pwd -P)" != "$(cd "$BUNDLE" && pwd -P)" ]; then
  echo "Glyphs 3 does not load this bundle: $LINK is not a link to $BUNDLE." >&2
  echo "Run with --install (an installed copy has to go to the Trash first)." >&2
  exit 1
fi

# A syntax error would only show as ten minutes without selftest.json.
"$PY" - "$RESOURCES" <<'EOF'
import ast, os, sys
res, bad = sys.argv[1], 0
for name in sorted(os.listdir(res)):
    if name.endswith(".py"):
        path = os.path.join(res, name)
        try:
            with open(path, encoding="utf-8") as f:
                ast.parse(f.read(), path)
        except SyntaxError as e:
            print("syntax error: %s:%s: %s" % (path, e.lineno, e.msg), file=sys.stderr)
            bad += 1
sys.exit(1 if bad else 0)
EOF

# One temporary instance at a time: a test (or tool) instance carries one of
# these keys on its command line; the user's own Glyphs never does.
BUSY="$(pgrep -f "$KEY\.(selfTestFont|devScript)" || true)"
if [ -n "$BUSY" ]; then
  echo "a temporary Glyphs 3 with a Kinetikern2 test is still running (pid $(echo $BUSY)); try again when it quit" >&2
  exit 1
fi

TMP="${TMPDIR:-/tmp}"
RESULTS="$(mktemp -d "${TMP%/}/kk2-verify.XXXXXX")"
REPORT="$RESULTS/selftest.json"
# the results folder is unique, so it identifies this run's instance
PATTERN="$KEY\.selfTestOut $(printf '%s' "$RESULTS" | sed 's/[][\.*^$?+(){}|]/\\&/g')"
EXTRA=()
[ "$SPACING_GROUPS" = 1 ] && EXTRA+=("-$KEY.selfTestGroups" YES)
[ "$CONNECTED" = 1 ] && EXTRA+=("-$KEY.selfTestConnected" YES)
echo "self-test: $(basename "$FONT") in a temporary Glyphs 3 (results in $RESULTS)"
open -n -a "$GLYPHS_APP" --args -ApplePersistenceIgnoreState YES \
  "-$KEY.selfTestFont" "$FONT" \
  "-$KEY.selfTestOut" "$RESULTS" \
  "-$KEY.selfTestQuit" YES \
  "-$KEY.selfTestWhole" YES \
  "-$KEY.selfTestCancel" YES \
  ${EXTRA[@]+"${EXTRA[@]}"}

START=$(date +%s)
PID=""
while [ ! -f "$REPORT" ]; do
  ELAPSED=$(( $(date +%s) - START ))
  [ -n "$PID" ] || PID="$(pgrep -f "$PATTERN" | head -n 1 || true)"
  if [ -n "$PID" ] && ! kill -0 "$PID" 2>/dev/null; then
    echo "the temporary Glyphs 3 quit without writing selftest.json (a crash? see Console.app)" >&2
    exit 1
  fi
  if [ "$ELAPSED" -ge "$VERIFY_TIMEOUT" ]; then
    echo "no selftest.json after $VERIFY_TIMEOUT s" >&2
    if [ -n "$PID" ]; then
      echo "terminating the temporary Glyphs 3 (pid $PID)" >&2
      kill -TERM "$PID" 2>/dev/null || true
    fi
    exit 1
  fi
  if [ -z "$PID" ] && [ "$ELAPSED" -ge 60 ]; then
    echo "the temporary Glyphs 3 did not start" >&2
    exit 1
  fi
  sleep 2
done

# the test quits its instance (selfTestQuit) right after writing the report
if [ -n "$PID" ]; then
  for _ in $(seq 1 "$QUIT_TIMEOUT"); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$PID" 2>/dev/null; then
    echo "the temporary Glyphs 3 (pid $PID) did not quit; terminating it" >&2
    kill -TERM "$PID" 2>/dev/null || true
  fi
fi

for image in window groups pairs connected; do
  [ -f "$RESULTS/$image.png" ] && echo "$image image: $RESULTS/$image.png"
done
echo "report: $REPORT"
"$PY" - "$REPORT" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    r = json.load(f)
engine = r.get("engine") or {}
print("Kinetikern2 self-test %s — %s, Glyphs %s, engine %s"
      % ("PASSED" if r.get("ok") else "FAILED", r.get("font"), r.get("glyphs_version"), engine.get("version", "?")))
for line in r.get("summary", []):
    print("  " + line)
for w in r.get("warnings", []):
    print("warning: " + w)
for e in r.get("errors", []):
    print("error: " + e.rstrip())
sys.exit(0 if r.get("ok") else 1)
EOF
