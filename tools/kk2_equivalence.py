#!/usr/bin/env python3
"""
kk2_equivalence — Kinetikern2 against v1's brute force, pair by pair.

    "$GPY" Kinetikern2/tools/kk2_equivalence.py reference   # gate M1/M2: bit-identical to v1
    "$GPY" Kinetikern2/tools/kk2_equivalence.py window      # gate M4: window solver vs reference
    "$GPY" Kinetikern2/tools/kk2_equivalence.py classes     # gate M5: class kerning expanded vs glyph pairs

`reference` runs both engines on the same outlines (every glyph pair, v1's
solver, no threshold, no scope) and requires identical sidebearings (bit for
bit) and identical kerning (v1's f64 value rounded to f32 equals Kinetikern2's).
"""

from __future__ import print_function

import argparse
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kk2_fonts as kf  # noqa: E402
import kk2_bridge as kb  # noqa: E402

SUP = "/System/Library/Fonts/Supplemental/"
FONTS = [SUP + "Arial.ttf", SUP + "Times New Roman.ttf", SUP + "Verdana.ttf", SUP + "Georgia.ttf",
         SUP + "Trebuchet MS.ttf"]


def f32(x):
    import struct
    return struct.unpack("<f", struct.pack("<f", x))[0]


def v2_solve(engine, lf, params, kern_all=True, threads=0):
    t = time.time()
    ctx = kf.run_job(engine.prepare(lf.packer(kern_all), lf.upm, threads))
    prep = time.time() - t
    t = time.time()
    res = kf.run_job(engine.solve(ctx, params))
    return ctx, res, prep, time.time() - t


def reference(args):
    sys.path.insert(0, kf.V1_RESOURCES)
    import kinetikern_bridge as v1
    e1 = v1.KineticEngine(kf.V1_DYLIB)
    e2 = kb.Engine(kf.DYLIB)
    ok_all = True
    for path in args.fonts:
        for chars, limit, label in ((kf.CHARS, None, "core"), (None, args.limit, "first %d" % args.limit)):
            lf = kf.LoadedFont(path, chars=chars, limit=limit)
            geo = lf.v1_geometries()
            t = time.time()
            ctx1 = e1.context(geo, lf.upm)
            r1 = ctx1.solve(1.0, 3.86, 1.0)
            t1 = time.time() - t
            ctx2, r2, prep2, solve2 = v2_solve(e2, lf, kb.make_params(classes=False, window=False,
                                                                       scope_scripts=False, threshold=0.5))
            # sidebearings
            mism_m = 0
            for i, name in enumerate(lf.names):
                g = r1.glyphs[name]
                m = r2.metrics[i]
                if not g.valid:
                    continue
                if (g.lsb != m.lsb and not (math.isnan(g.lsb) and math.isnan(m.lsb))) or g.rsb != m.rsb:
                    mism_m += 1
            # kerning
            k2 = {}
            for kind, l, r, v, imp in r2.iter_entries():
                k2[(lf.names[l], lf.names[r])] = v
            k1 = dict(((a, b), v) for (a, b), v in r1.kerning.items())
            keys = set(k1) | set(k2)
            mism_k = [(key, k1.get(key), k2.get(key)) for key in keys if f32(k1.get(key, 0.0)) != k2.get(key, 0.0)]
            ok = mism_m == 0 and not mism_k
            ok_all &= ok
            st = r2.stats
            print("%-22s %-9s %4d glyphs %7d pairs | v1 %6.2f s | kk2 prep %5.2f s solve %6.2f s | metrics diff %d | "
                  "kerning %6d vs %6d, diff %d | active rays %.0f%% | %s"
                  % (os.path.basename(path), label, len(lf.names), st["member_pairs"], t1, prep2, solve2, mism_m,
                     len(k1), len(k2), len(mism_k), 100.0 * st["active_rays"] / max(1, st["merged_rays"]),
                     "IDENTICAL" if ok else "DIFFERENT"))
            for key, a, b in mism_k[:5]:
                print("      %s: v1 %r kk2 %r" % (key, a, b))
            ctx2.close()
            r2.close()
    print("\nreference equivalence:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


def compare_values(base, other):
    """Max |diff| and counts between two {pair: value} maps (missing = 0)."""
    keys = set(base) | set(other)
    diffs = [abs(base.get(k, 0.0) - other.get(k, 0.0)) for k in keys]
    return keys, diffs


def window(args):
    e2 = kb.Engine(kf.DYLIB)
    worst = 0.0
    for path in args.fonts:
        lf = kf.LoadedFont(path, chars=None, limit=args.limit)
        common = dict(classes=False, scope_scripts=False, threshold=args.threshold)
        ctx, ref, _, t_ref = v2_solve(e2, lf, kb.make_params(window=False, **common))
        win = kf.run_job(e2.solve(ctx, kb.make_params(window=True, **common)))
        a = dict(((l, r), v) for k, l, r, v, i in ref.iter_entries())
        b = dict(((l, r), v) for k, l, r, v, i in win.iter_entries())
        keys, diffs = compare_values(a, b)
        n = win.stats["member_pairs"]
        over = sum(1 for d in diffs if d >= args.threshold)
        worst = max(worst, max(diffs) if diffs else 0.0)
        print("%-22s %5d glyphs %8d pairs | evals/pair ref %.2f window %.2f | time %.2f → %.2f s | entries %d vs %d | "
              "|diff| ≥ T: %d (%.3f%%), max %.1f | window-settled %.0f%% | fallbacks %d"
              % (os.path.basename(path), len(lf.names), n, ref.stats["force_evaluations"] / max(n, 1),
                 win.stats["force_evaluations"] / max(n, 1), t_ref, win.stats["pass2_ms"] / 1000.0, len(a), len(b),
                 over, 100.0 * over / max(n, 1), max(diffs) if diffs else 0, 100.0 * win.stats["window_hits"] / max(n, 1),
                 win.stats["fallbacks"]))
        ctx.close()
    return 0


def classes(args):
    e2 = kb.Engine(kf.DYLIB)
    for path in args.fonts:
        lf = kf.LoadedFont(path, chars=None, limit=args.limit)
        common = dict(window=True, scope_scripts=True, threshold=args.threshold)
        ctx, pairs, _, t_pairs = v2_solve(e2, lf, kb.make_params(classes=False, **common), kern_all=False)
        t = time.time()
        cls = kf.run_job(e2.solve(ctx, kb.make_params(classes=True, radius_ratio=args.radius, **common)))
        t_cls = time.time() - t
        a = dict(((l, r), v) for k, l, r, v, i in pairs.iter_entries())
        # expand the class result over the same pairs
        n = len(lf.names)
        lefts, rights = [], []
        for l in range(n):
            if not cls.kern_mask[l]:
                continue
            for r in range(n):
                if cls.kern_mask[r]:
                    lefts.append(l)
                    rights.append(r)
        vals = cls.values(lefts, rights)
        # only pairs in scope count (the class result is defined there): a
        # class value also reaches member pairs of different scripts, which
        # glyph-pair mode never solves and text never shows
        scripts = [s.script for s in lf.specs()]
        b = {}
        for l, r, v in zip(lefts, rights, vals):
            if v == v and v != 0.0 and (not scripts[l] or not scripts[r] or scripts[l] == scripts[r]):
                b[(l, r)] = v
        keys = set(a) | set(b)
        diffs = sorted((abs(a.get(k, 0.0) - b.get(k, 0.0)), k) for k in keys)
        total = pairs.stats["member_pairs"]
        within1 = total - sum(1 for d, k in diffs if d > 1.0)
        over = [x for x in diffs if x[0] >= args.threshold]
        st = cls.stats
        print("%-22s %5d glyphs | %8d pairs in scope | pairs %.2f s → classes %.2f s | entries %d glyph pairs → %d "
              "(%d class pairs, %d exceptions) | classes R %d L %d | rep solves %d, inherited %d, verified %d "
              "(%d within), solved %d | within 1 unit %.3f%% | ≥ T: %d, max %.1f"
              % (os.path.basename(path), n, total, t_pairs, t_cls, len(a), cls.entry_count, st["class_entries"],
                 st["exception_entries"], cls.right_class_count, cls.left_class_count, st["class_pairs"],
                 st["inherited"], st["verified"], st["verified_within"], st["solved"] - st["class_pairs"],
                 100.0 * within1 / max(total, 1), len(over), diffs[-1][0] if diffs else 0))
        for d, (l, r) in over[-6:]:
            rl = cls.right_class_rep[cls.glyph_right_class[l]]
            rr = cls.left_class_rep[cls.glyph_left_class[r]]
            print("      %s %s: pairs %.1f classes %.1f (class pair %s %s: pairs %.1f)"
                  % (lf.names[l], lf.names[r], a.get((l, r), 0.0), b.get((l, r), 0.0), lf.names[rl], lf.names[rr],
                     a.get((rl, rr), 0.0)))
        ctx.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["reference", "window", "classes"])
    ap.add_argument("--fonts", nargs="*", default=FONTS)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--threshold", type=float, default=5.0 * 2048 / 1000.0)
    ap.add_argument("--radius", type=float, default=1.0)
    args = ap.parse_args()
    return {"reference": reference, "window": window, "classes": classes}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
