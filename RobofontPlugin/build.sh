#!/usr/bin/env bash
# Builds the Kinetikern2 extension for RoboFont: Kinetikern2.roboFontExt.
#
#   ./build.sh                  build the engine (universal: arm64 + x86_64) and the extension
#   ./build.sh --native         the engine for this Mac's architecture only (quicker; on Apple
#                               silicon arm64 only, which RoboFont 4.4, an Intel app under
#                               Rosetta, cannot load: use the universal build for it)
#   ./build.sh --no-engine      keep the engine library already in the extension
#   ./build.sh --engine DIR     the engine's source (default: ../engine)
#   ./build.sh --test           the headless tests first (python3 with fontParts, defcon, fontTools)
#   ./build.sh --install        also link the extension into RoboFont's plugins folder
#   ./build.sh --verify [font]  then run the in-RoboFont self-test (kk2_selftest) in a
#                               temporary second RoboFont instance; default font:
#                               /System/Library/Fonts/Supplemental/Arial.ttf (any .ttf, .otf
#                               or .ufo; a .ufo is copied first)
#   ./build.sh --verify --groups  the self-test also runs its spacing-groups stage
#   ./build.sh --verify FONT --connected  and its connected-script stage (FONT must be a
#                               connected script, e.g. an OFL script from Google Fonts)
#   ./build.sh --verify FONT --slant DEG --lean YES|NO  a font that declares no italic angle
#                               but leans: the slant its stems show must be DEG (within
#                               1°, Spacing QA's), and Spacing QA's rule must measure it
#                               along that slant (YES) or upright (NO); the whole-font
#                               run, Apply and Revert then run that way
#   ./build.sh --verify --profile  profiles the main thread from Apply to the end of Revert
#                               (profile.txt next to the report)
#
# The engine library is replaced atomically (built into a staging file, signed, then
# renamed over the old one), so a RoboFont that has the old one loaded keeps it.
# --verify never touches a running RoboFont: it starts its own instance (open -n) with
# the test's parameters in the argument domain, waits up to 10 minutes for
# selftest.json, prints a summary and exits non-zero unless the test passed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
ENGINE="$ROOT/../engine"
SOURCE="$ROOT/source/lib"
BUNDLE="$ROOT/Kinetikern2.roboFontExt"
LIB="$BUNDLE/lib"
DYLIB="libkinetikern2.dylib"
OUT="$LIB/$DYLIB"
PLUGINS="$HOME/Library/Application Support/RoboFont/plugins"
LINK="$PLUGINS/Kinetikern2.roboFontExt"
ROBOFONT_APP="${ROBOFONT_APP:-/Applications/RoboFont.app}"
KEY="com.mirkovelimirovic.Kinetikern2"
VERSION="2.0.0"
VERIFY_TIMEOUT=600   # seconds for selftest.json to appear
QUIT_TIMEOUT=60      # seconds for the temporary RoboFont to quit afterwards

NATIVE=0
BUILD_ENGINE=1
INSTALL=0
TEST=0
VERIFY=0
SPACING_GROUPS=0
CONNECTED=0
SLANT=""
LEAN=""
PROFILE=0
FONT="/System/Library/Fonts/Supplemental/Arial.ttf"
while [ $# -gt 0 ]; do
  case "$1" in
    --native) NATIVE=1 ;;
    --no-engine) BUILD_ENGINE=0 ;;
    --engine) ENGINE="$2"; shift ;;
    --install) INSTALL=1 ;;
    --test) TEST=1 ;;
    --groups) SPACING_GROUPS=1 ;;
    --connected) CONNECTED=1 ;;
    --slant) SLANT="$2"; shift ;;
    --lean) LEAN="$2"; shift ;;
    --profile) PROFILE=1 ;;
    --verify)
      VERIFY=1
      # an optional font path follows (anything not starting with "-")
      if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then FONT="$2"; shift; fi ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

PY="$(command -v python3 || true)"
if [ "$VERIFY" = 1 ]; then
  # Fail before the build, not ten minutes into the test.
  [ -e "$FONT" ] || { echo "font not found: $FONT" >&2; exit 2; }
  FONT="$(cd "$(dirname "$FONT")" && pwd -P)/$(basename "$FONT")"
  [ -d "$ROBOFONT_APP" ] || { echo "RoboFont not found at $ROBOFONT_APP (set ROBOFONT_APP)" >&2; exit 2; }
  [ -n "$PY" ] || { echo "--verify needs python3 to read the results" >&2; exit 2; }
fi

# ------------------------------------------------------------------ the engine
mkdir -p "$LIB"
if [ "$BUILD_ENGINE" = 1 ]; then
  [ -f "$ENGINE/Cargo.toml" ] || { echo "no engine at $ENGINE (use --engine DIR)" >&2; exit 2; }
  ENGINE="$(cd "$ENGINE" && pwd)"
  # A staging file of this run's own next to the library, so the final rename
  # stays on one volume; it is gone however the script ends.
  NEW="$(mktemp "$LIB/.$DYLIB.XXXXXX")"
  trap 'rm -f "$NEW"' EXIT
  (
    cd "$ENGINE"
    if [ "$NATIVE" = 1 ]; then
      MACOSX_DEPLOYMENT_TARGET=11.0 cargo build --release
      cp "target/release/$DYLIB" "$NEW"
    else
      # Cross-compiling needs rustup's toolchain with both standard libraries.
      command -v rustup >/dev/null 2>&1 || { echo "a universal build needs rustup (https://rustup.rs)" >&2; exit 1; }
      for t in aarch64-apple-darwin x86_64-apple-darwin; do
        if ! rustup target list --installed | grep -qx "$t"; then
          echo "missing Rust target $t — run: rustup target add $t" >&2; exit 1
        fi
      done
      # rustup's own compiler: another rustc first on PATH (Homebrew's) has
      # only its own platform's standard library
      RUSTC_RUSTUP="$(rustup which rustc)"
      CARGO_RUSTUP="$(rustup which cargo)"
      MACOSX_DEPLOYMENT_TARGET=11.0 RUSTC="$RUSTC_RUSTUP" "$CARGO_RUSTUP" build --release --target aarch64-apple-darwin
      MACOSX_DEPLOYMENT_TARGET=10.13 RUSTC="$RUSTC_RUSTUP" "$CARGO_RUSTUP" build --release --target x86_64-apple-darwin
      lipo -create \
        "target/aarch64-apple-darwin/release/$DYLIB" \
        "target/x86_64-apple-darwin/release/$DYLIB" \
        -output "$NEW"
    fi
  )
  # ctypes loads the library by path; a neutral install name keeps build paths out
  # of the binary. Changing it invalidates the linker signature, so sign again (ad
  # hoc is enough: RoboFont disables library validation for extensions).
  chmod 755 "$NEW"  # mktemp creates it private
  install_name_tool -id "@rpath/$DYLIB" "$NEW"
  if ! SIGNED="$(codesign --force --sign - "$NEW" 2>&1)"; then
    echo "$SIGNED" >&2; exit 1
  fi
  if [ -f "$OUT" ] && cmp -s "$NEW" "$OUT"; then
    rm -f "$NEW"
    echo "engine unchanged $(lipo -archs "$OUT") → $OUT"
  else
    mv -f "$NEW" "$OUT"
    echo "engine built $(lipo -archs "$OUT") → $OUT"
  fi
else
  [ -f "$OUT" ] || { echo "no engine library in the extension yet: build without --no-engine" >&2; exit 2; }
fi

# --------------------------------------------------------------- the extension
# lib: the extension's modules and the harness table (the library stays)
find "$LIB" -maxdepth 1 \( -name '*.py' -o -name '*.json' \) -delete
cp "$SOURCE"/*.py "$SOURCE"/kk2_harness.json "$LIB/"
rm -rf "$LIB/__pycache__"
[ -f "$ROOT/../LICENSE" ] && cp "$ROOT/../LICENSE" "$BUNDLE/license"
"$PY" - "$BUNDLE/info.plist" "$VERSION" <<'EOF'
import plistlib, sys, time
path, version = sys.argv[1], sys.argv[2]
info = {
    "name": "Kinetikern2",
    "developer": "Mirko Velimirovic",
    "developerURL": "https://github.com/bghryct/kinetikern2",
    "version": version,
    "html": False,
    "launchAtStartUp": True,
    "mainScript": "kk2_startup.py",
    "uninstallScript": "",
    "addToMenu": [{"path": "kk2_menu.py", "preferredName": "Kinetikern2…", "shortKey": ""}],
    "requiresVersionMajor": "4",
    "requiresVersionMinor": "0",
    "timeStamp": time.time(),
}
with open(path, "wb") as f:
    plistlib.dump(info, f)
EOF
echo "extension → $BUNDLE"

if [ "$TEST" = 1 ]; then
  "$PY" "$ROOT/tests/test_headless.py"
fi

if [ "$INSTALL" = 1 ]; then
  if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    echo "$LINK exists and is not a link (an installed copy?); leaving it alone." >&2
    echo "Remove it in RoboFont (RoboFont ▸ Preferences ▸ Extensions) or move it to the Trash, then run --install again." >&2
    exit 1
  fi
  mkdir -p "$PLUGINS"
  ln -sfn "$BUNDLE" "$LINK"
  echo "linked into $PLUGINS — restart RoboFont to load it"
fi

[ "$VERIFY" = 1 ] || exit 0

# ---------------------------------------------------------------- --verify
# The temporary instance loads its extensions from the plugins folder: it must load
# this one, not an older copy.
if [ ! -L "$LINK" ] || [ "$(cd "$LINK" && pwd -P)" != "$(cd "$BUNDLE" && pwd -P)" ]; then
  echo "RoboFont does not load this extension: $LINK is not a link to $BUNDLE." >&2
  echo "Run with --install (an installed copy has to go first)." >&2
  exit 1
fi

# A syntax error would only show as ten minutes without selftest.json.
"$PY" - "$LIB" <<'EOF'
import ast, os, sys
lib, bad = sys.argv[1], 0
for name in sorted(os.listdir(lib)):
    if name.endswith(".py"):
        path = os.path.join(lib, name)
        try:
            with open(path, encoding="utf-8") as f:
                ast.parse(f.read(), path, feature_version=(3, 9))
        except SyntaxError as e:
            print("syntax error: %s:%s: %s" % (path, e.lineno, e.msg), file=sys.stderr)
            bad += 1
sys.exit(1 if bad else 0)
EOF

# One temporary instance at a time: a test (or tool) instance carries one of
# these keys on its command line; the user's own RoboFont never does.
BUSY="$(pgrep -f "$KEY\.(selfTestFont|devScript)" || true)"
if [ -n "$BUSY" ]; then
  echo "a temporary RoboFont with a Kinetikern2 test is still running (pid $(echo $BUSY)); try again when it quit" >&2
  exit 1
fi

TMP="${TMPDIR:-/tmp}"
RESULTS="$(mktemp -d "${TMP%/}/kk2rf-verify.XXXXXX")"
REPORT="$RESULTS/selftest.json"
# the results folder is unique, so it identifies this run's instance
PATTERN="$KEY\.selfTestOut $(printf '%s' "$RESULTS" | sed 's/[][\.*^$?+(){}|]/\\&/g')"
EXTRA=()
[ "$SPACING_GROUPS" = 1 ] && EXTRA+=("-$KEY.selfTestGroups" YES)
[ "$CONNECTED" = 1 ] && EXTRA+=("-$KEY.selfTestConnected" YES)
[ -n "$SLANT" ] && EXTRA+=("-$KEY.selfTestSlant" "$SLANT")
[ -n "$LEAN" ] && EXTRA+=("-$KEY.selfTestLean" "$LEAN")
[ "$PROFILE" = 1 ] && EXTRA+=("-$KEY.selfTestProfile" YES)
echo "self-test: $(basename "$FONT") in a temporary RoboFont (results in $RESULTS)"
open -n -a "$ROBOFONT_APP" --args -ApplePersistenceIgnoreState YES \
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
    echo "the temporary RoboFont quit without writing selftest.json (a crash? see Console.app)" >&2
    exit 1
  fi
  if [ "$ELAPSED" -ge "$VERIFY_TIMEOUT" ]; then
    echo "no selftest.json after $VERIFY_TIMEOUT s" >&2
    if [ -n "$PID" ]; then
      echo "terminating the temporary RoboFont (pid $PID)" >&2
      kill -TERM "$PID" 2>/dev/null || true
    fi
    exit 1
  fi
  if [ -z "$PID" ] && [ "$ELAPSED" -ge 60 ]; then
    echo "the temporary RoboFont did not start" >&2
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
    echo "the temporary RoboFont (pid $PID) did not quit; terminating it" >&2
    kill -TERM "$PID" 2>/dev/null || true
  fi
fi

for image in window harness harness-letters groups pairs connected; do
  [ -f "$RESULTS/$image.png" ] && echo "$image image: $RESULTS/$image.png"
done
echo "report: $REPORT"
"$PY" - "$REPORT" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    r = json.load(f)
engine = r.get("engine") or {}
print("Kinetikern2 self-test %s — %s, RoboFont %s, engine %s"
      % ("PASSED" if r.get("ok") else "FAILED", r.get("font"), r.get("robofont_version"), engine.get("version", "?")))
for line in r.get("summary", []):
    print("  " + line)
for w in r.get("warnings", []):
    print("warning: " + w)
for e in r.get("errors", []):
    print("error: " + e.rstrip())
sys.exit(0 if r.get("ok") else 1)
EOF
