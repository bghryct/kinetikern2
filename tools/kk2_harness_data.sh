#!/bin/bash
# The observations the designer harness is learned from beyond Spacing QA's
# library scan (which checks each family at Regular): the reference variable
# families (text faces rated well spaced) checked at their other weights,
# 100-900, from the fonts Spacing QA has cached. Then learn the harness:
#
#   tools/kk2_harness_data.sh ../SpacingQA/data families.csv weights/
#   python3 tools/kk2_harness_learn.py --reports ../SpacingQA/data/reports \
#       --weights weights/ --families families.csv \
#       --out plugin/Kinetikern2.glyphsPlugin/Contents/Resources/kk2_harness.json
#
# families.csv: google/fonts' tags/all/families.csv (the human
# /Quality/Spacing tags). About 1,200 checks; 10 minutes on 6 M1 cores.
set -euo pipefail
DATA=${1:?Spacing QA data directory}
TAGS=${2:?google/fonts tags/all/families.csv}
OUT=${3:?output directory}
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
SPACINGQA="$HERE/../SpacingQA/target/release/spacingqa"
[ -x "$SPACINGQA" ] || { echo "build Spacing QA first (cargo build --release in SpacingQA)" >&2; exit 1; }
mkdir -p "$OUT"
JOBS="$OUT/jobs.txt"
python3 - "$DATA" "$TAGS" "$JOBS" <<'EOF'
import csv, json, os, sys
data, tags, jobs_path = sys.argv[1:4]
human = {r[0]: float(r[3]) for r in csv.reader(open(tags, encoding="utf-8"))
         if len(r) == 4 and r[2] == "/Quality/Spacing" and not r[1]}


def fnv(s):
    h = 0xcbf29ce484222325
    for b in s.encode():
        h ^= b
        h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


jobs, fams = [], 0
for slug, r in json.load(open(os.path.join(data, "index.json"))).items():
    f, s = r["font"], r.get("summary")
    if not s or f.get("italic") or f.get("monospaced") or f.get("category") not in ("Sans Serif", "Serif"):
        continue
    if f.get("primary_script") not in (None, "", "Latn") or (human.get(f["family"]) or 0) < 70:
        continue
    w = {a["tag"]: a for a in f.get("axes", [])}.get("wght")
    if not w or w["max"] - w["min"] < 200:
        continue
    url = (f.get("source") or {}).get("url")
    path = os.path.join(data, "fonts", fnv(url) + ".ttf") if url else None
    if not path or not os.path.exists(path):
        continue
    fams += 1
    for x in sorted({max(w["min"], 100.0), 200.0, 300.0, 500.0, 600.0, 700.0, 800.0, min(w["max"], 900.0)}):
        if w["min"] <= x <= w["max"] and x != 400:
            jobs.append(f"{slug}\t{path}\t{x:g}")
open(jobs_path, "w").write("\n".join(jobs) + "\n")
print(f"{fams} families, {len(jobs)} checks")
EOF
# one check per family and weight; the family slug names the file, so the
# learner can find the family's category in the library scan
cut -f1-3 "$JOBS" | xargs -P 6 -L 1 sh -c '"$0" check "$2" --weight "$3" --no-instances --json "'"$OUT"'/$1@$3.json" \
  > /dev/null 2>&1 || echo "failed: $1 at $3"' "$SPACINGQA"
echo "$(ls "$OUT"/*@*.json | wc -l | tr -d ' ') observations in $OUT"
