#!/usr/bin/env python3
"""
kk2_benchmark — Kinetikern2 on v1's 73-font benchmark (designer spacing as
the reference), next to v1 itself.

    "$GPY" Kinetikern2/tools/kk2_benchmark.py                       # all sets, all modes
    "$GPY" Kinetikern2/tools/kk2_benchmark.py --set system --modes v1,pairs

Loading and scoring are v1's tools/benchmark.py (imported, not copied): the
same cached font cases (outlines, designer sidebearings and CoreText kerning),
score() and summarize(). Kinetikern2 gets the same glyph inputs v1 gets
(contours, rhythm groups, fixed advance for tabular figures), every glyph
kerned, script from its character, and the plugin's defaults (PLUGIN_DEFAULTS
at slider 0: spring 1, repulsion = base ratio, coupling = intensity / 100).

Modes:
    v1         v1's engine (benchmark.py's Runner)
    pairs      glyph pairs, window solver, script scope, threshold 0.5 (v1's rounding)
    pairs-t5   glyph pairs, window solver, threshold 5 units per 1000 em
    classes    class kerning, threshold 5 units per 1000 em (no designer groups in
               the cache: classes come from shape matches only)
    reference  glyph pairs, v1's solver, no scope (diagnostics: bit-identical to v1)

Gate: `pairs` within noise of `v1` on every set (medians): pair gaps ±0.3,
designer-kerned pairs ±0.5, kerning r ±0.01, sidebearings bit-identical.
Everything goes to Kinetikern2/results/benchmark.json.
"""

from __future__ import print_function

import argparse
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kk2_fonts as kf  # noqa: E402
import kk2_bridge as kb  # noqa: E402

sys.path.insert(0, os.path.join(kf.ROOT, "tools"))
import benchmark as bm  # noqa: E402  (v1: loading, scoring, plugin defaults)

from fontTools import unicodedata as fud  # noqa: E402

CACHE = os.path.join(kf.ROOT, "results", "benchmark", "cache")
V1_FINAL = os.path.join(kf.ROOT, "results", "benchmark", "tuned-final.json")
OUT = os.path.join(kf.KK2, "results", "benchmark.json")
SETS = ("google", "google-reserve", "system")
SET_LABELS = {"google": "Google 1-30 (tuning)", "google-reserve": "Google 31-43 (validation)",
              "system": "System (held-out test)"}
MODES = ("v1", "pairs", "pairs-t5", "classes", "reference")
DEFAULT_MODES = ("v1", "pairs", "pairs-t5", "classes")
# (classes, window, scope_scripts, threshold in units per 1000 em; None = v1's rounding)
KK2_MODES = {"pairs": (False, True, True, None), "pairs-t5": (False, True, True, 5.0),
             "classes": (True, True, True, 5.0), "reference": (False, False, False, None)}
# gate: largest |median(pairs) - median(v1)| that counts as noise
GATE = {"gap_mae": 0.3, "kerned_mae": 0.5, "kern_r": 0.01}


def params_for(case, mode, threads=0):
    """KK2Params for one font: v1's plugin defaults, UPM-relative values in units."""
    p = dict(bm.PLUGIN_DEFAULTS)
    spring, repulsion, coupling = bm.physics(0.0, p["intensity"], p["base"])
    classes, window, scope, threshold = KK2_MODES[mode]
    units = 0.5 if threshold is None else threshold * case.upm / 1000.0
    return kb.make_params(spring=spring, repulsion=repulsion, coupling=coupling, classes=classes, window=window,
                          scope_scripts=scope, threshold=units, threads=threads, **bm.options_for(case, p))


class Kk2Runner(object):
    """Kinetikern2 with benchmark.Runner's solve() outputs; one prepared
    Context per font, shared by every mode."""

    def __init__(self, engine, cases, threads=0):
        self.engine = engine
        self.threads = threads
        self.ctx = {}
        self.prep_seconds = {}
        for case in cases:
            t = time.time()
            self.ctx[case.label] = kf.run_job(engine.prepare(self.packer(case), case.upm, threads))
            self.prep_seconds[case.label] = time.time() - t

    @staticmethod
    def packer(case):
        p = kb.InputPacker()
        for ch, n in zip(case.chars, case.names):
            flags = kb.GLYPH_KERN
            if case.tabular and n in case.figures:
                flags |= kb.GLYPH_FIXED_ADVANCE
            script = fud.script(ch)
            if script in kb.RTL_SCRIPTS:
                flags |= kb.GLYPH_RTL
            p.add(kb.GlyphSpec(n, case.contours[n], case.advance[n], case.groups[n], flags, kb.script_code(script)))
        return p

    def solve(self, case, mode):
        """(lsb, rsb, advance, kerning, stats): kerning as text sees it, every
        ordered pair looked up through the result (class entries expanded)."""
        t = time.time()
        res = kf.run_job(self.engine.solve(self.ctx[case.label], params_for(case, mode, self.threads)))
        seconds = time.time() - t
        names = case.names
        n = len(names)
        lefts = [i for i in range(n) for _ in range(n)]
        rights = list(range(n)) * n
        kerning = {}
        for l, r, v in zip(lefts, rights, res.values(lefts, rights)):
            if v == v and v != 0.0:
                kerning[(names[l], names[r])] = float(v)
        m = res.metrics
        lsb = dict((name, m[i].lsb) for i, name in enumerate(names))
        rsb = dict((name, m[i].rsb) for i, name in enumerate(names))
        adv = dict((name, m[i].advance) for i, name in enumerate(names))
        stats = dict((k, v) for k, v in res.stats.items() if not isinstance(v, list))
        stats.update(seconds=seconds, entries=res.entry_count, right_classes=res.right_class_count,
                     left_classes=res.left_class_count)
        res.close()
        return lsb, rsb, adv, kerning, stats

    def close(self):
        for c in self.ctx.values():
            c.close()
        self.ctx = {}


def v1_solve(runner, case):
    p = bm.PLUGIN_DEFAULTS
    t = time.time()
    lsb, rsb, adv, kern, res = runner.solve(case, bm.options_for(case, p), 0.0, p["intensity"], p["base"])
    stats = dict((k, v) for k, v in res.stats.items() if not isinstance(v, list))
    stats.update(seconds=time.time() - t, entries=len(kern))
    return lsb, rsb, adv, dict(kern), stats


def compare(case, a, b):
    """Where two solves of one font differ (font units → units per 1000 em)."""
    unit = 1000.0 / case.upm
    lsb_a, rsb_a, _, kern_a, _ = a
    lsb_b, rsb_b, _, kern_b, _ = b
    sb_diff = [n for n in case.names if lsb_a[n] != lsb_b[n] or rsb_a[n] != rsb_b[n]]
    sb_max = max([abs(lsb_a[n] - lsb_b[n]) for n in case.names] + [abs(rsb_a[n] - rsb_b[n]) for n in case.names])
    diffs = []
    for key in set(kern_a) | set(kern_b):
        d = abs(kern_a.get(key, 0.0) - kern_b.get(key, 0.0)) * unit
        if d > 0.0:
            diffs.append((d, key, kern_a.get(key, 0.0) * unit, kern_b.get(key, 0.0) * unit))
    diffs.sort(reverse=True)
    pairs = len(case.names) ** 2
    return {"sidebearings_identical": not sb_diff, "sidebearing_glyphs_different": len(sb_diff),
            "sidebearing_max_diff": sb_max * unit, "pairs": pairs,
            "kerned_a": len(kern_a), "kerned_b": len(kern_b),
            "pairs_diff_over_0.5": sum(1 for d in diffs if d[0] >= 0.5),
            "pairs_diff_over_1": sum(1 for d in diffs if d[0] >= 1.0),
            "pairs_diff_over_5": sum(1 for d in diffs if d[0] >= 5.0),
            "max_diff": diffs[0][0] if diffs else 0.0,
            "worst": [{"pair": list(k), "a": va, "b": vb} for d, k, va, vb in diffs[:5]]}


def finite(x):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else x


def clean(obj):
    """JSON without NaN (null instead)."""
    if isinstance(obj, dict):
        return dict((str(k), clean(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, float):
        return finite(obj)
    return obj


def run_set(name, modes, engine, threads):
    cases = bm.load_set(name, CACHE, verbose=False)
    print("\n%s: %d fonts" % (SET_LABELS[name], len(cases)))
    rows = dict((m, []) for m in modes)
    solved = dict((m, {}) for m in modes)
    timing = dict((m, {}) for m in modes)
    t = time.time()
    v1 = bm.Runner(cases) if "v1" in modes else None
    v1_prep = time.time() - t
    kk2 = Kk2Runner(engine, cases, threads) if any(m != "v1" for m in modes) else None
    for case in cases:
        for m in modes:
            out = v1_solve(v1, case) if m == "v1" else kk2.solve(case, m)
            solved[m][case.label] = out
            rows[m].append((case, bm.score(case, out[0], out[1], out[2], out[3])))
            timing[m][case.label] = out[4]["seconds"]
    summary = {}
    for m in modes:
        summary[m] = bm.summarize("%-9s %s" % (m, SET_LABELS[name]), rows[m])
    per_font = {}
    for case in cases:
        entry = {"upm": case.upm, "kerned": case.kerned, "designer_pairs": len(case.kerning),
                 "glyphs": len(case.names), "scores": {}, "stats": {}}
        for m in modes:
            entry["scores"][m] = next(s for c, s in rows[m] if c is case)
            entry["stats"][m] = solved[m][case.label][4]
        if kk2 is not None:
            entry["prep_seconds"] = kk2.prep_seconds[case.label]
        per_font[case.label] = entry
    comparisons = {}
    if "v1" in modes:
        for m in modes:
            if m != "v1":
                comparisons[m] = dict((c.label, compare(c, solved["v1"][c.label], solved[m][c.label])) for c in cases)
    if kk2 is not None:
        kk2.close()
    del v1
    return {"fonts": [c.label for c in cases], "summary": summary, "per_font": per_font,
            "comparison_vs_v1": comparisons,
            "seconds": dict((m, sum(timing[m].values())) for m in modes),
            "v1_prep_seconds": v1_prep if "v1" in modes else None,
            "kk2_prep_seconds": sum(kk2.prep_seconds.values()) if kk2 is not None else None}


def gate(results):
    """`pairs` against `v1`, set by set."""
    out = {"pass": True, "sets": {}}
    for name, r in results.items():
        s = r["summary"]
        if "v1" not in s or "pairs" not in s:
            continue
        row = {"pass": True}
        for k, tol in GATE.items():
            d = s["pairs"][k]["median"] - s["v1"][k]["median"]
            ok = abs(d) <= tol
            row[k] = {"v1": s["v1"][k]["median"], "pairs": s["pairs"][k]["median"], "delta": d,
                      "mean_delta": s["pairs"][k]["mean"] - s["v1"][k]["mean"], "tolerance": tol, "pass": ok}
            row["pass"] &= ok
        cmp = r["comparison_vs_v1"]["pairs"]
        different = sorted(label for label, c in cmp.items() if not c["sidebearings_identical"])
        row["sidebearings"] = {"identical_fonts": len(cmp) - len(different), "different_fonts": different,
                               "max_diff": max(c["sidebearing_max_diff"] for c in cmp.values()),
                               "pass": not different}
        row["pass"] &= not different
        row["kerning"] = {"pairs": sum(c["pairs"] for c in cmp.values()),
                          "diff_over_0.5": sum(c["pairs_diff_over_0.5"] for c in cmp.values()),
                          "diff_over_1": sum(c["pairs_diff_over_1"] for c in cmp.values()),
                          "diff_over_5": sum(c["pairs_diff_over_5"] for c in cmp.values()),
                          "max_diff": max(c["max_diff"] for c in cmp.values())}
        out["sets"][name] = row
        out["pass"] &= row["pass"]
    return out


def print_gate(g):
    print("\nGate: pairs (window solver, threshold 0.5) vs v1, medians")
    for name, row in g["sets"].items():
        print("  %-26s %s" % (SET_LABELS[name], "PASS" if row["pass"] else "FAIL"))
        for k in GATE:
            x = row[k]
            print("     %-11s v1 %7.3f  kk2 %7.3f  delta %+.3f (mean %+.3f, tolerance ±%.2f) %s"
                  % (k, x["v1"], x["pairs"], x["delta"], x["mean_delta"], x["tolerance"], "ok" if x["pass"] else "FAIL"))
        sb = row["sidebearings"]
        print("     sidebearings identical in %d fonts, different in %d %s (max %.3g units/1000 em)"
              % (sb["identical_fonts"], len(sb["different_fonts"]), sb["different_fonts"] or "", sb["max_diff"]))
        k = row["kerning"]
        print("     kerning: %d pairs, |diff| >= 0.5: %d, >= 1: %d, >= 5: %d, max %.2f units/1000 em"
              % (k["pairs"], k["diff_over_0.5"], k["diff_over_1"], k["diff_over_5"], k["max_diff"]))
    print("gate:", "PASS" if g["pass"] else "FAIL")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", default=",".join(SETS))
    ap.add_argument("--modes", default=",".join(DEFAULT_MODES))
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    modes = [m for m in args.modes.split(",") if m]
    for m in modes:
        if m not in MODES:
            ap.error("unknown mode %s (one of %s)" % (m, ", ".join(MODES)))
    engine = kb.Engine(kf.DYLIB)
    print("Kinetikern2 engine %s (%s), %d cores" % (engine.version, engine.path, engine.cpu_count))
    results = {}
    for name in args.set.split(","):
        results[name] = run_set(name, modes, engine, args.threads)
    g = gate(results)
    if g["sets"]:
        print_gate(g)
    v1_final = None
    if os.path.exists(V1_FINAL):
        with open(V1_FINAL) as f:
            v1_final = json.load(f).get("results")
    report = {"engine": engine.version, "dylib": engine.path, "params": bm.PLUGIN_DEFAULTS,
              "modes": dict((m, "v1 engine" if m == "v1" else dict(zip(("classes", "window", "scope_scripts",
                                                                          "threshold_per_1000"), KK2_MODES[m])))
                            for m in modes),
              "gate_tolerances": GATE, "gate": g, "sets": results,
              "v1_tuned_final": dict((SET_LABELS[n], v1_final[SET_LABELS[n]]["tuned"]) for n in results
                                     if v1_final and SET_LABELS[n] in v1_final)}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    if os.path.exists(args.out) and args.set != ",".join(SETS):
        # a partial run updates its sets in the saved report
        with open(args.out) as f:
            old = json.load(f)
        old_sets = old.get("sets", {})
        old_sets.update(clean(results))
        report["sets"] = old_sets
        report["gate"] = gate(old_sets)
    with open(args.out, "w") as f:
        json.dump(clean(report), f, indent=1)
    print("\nnumbers → %s" % args.out)
    return 0 if not g["sets"] or g["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
