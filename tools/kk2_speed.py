#!/usr/bin/env python3
"""
kk2_speed — whole-font timings of Kinetikern2 (and, with --v1, of v1).

    "$GPY" Kinetikern2/tools/kk2_speed.py                         # full Arial, plugin defaults
    "$GPY" Kinetikern2/tools/kk2_speed.py --font X.ttf --budget 0 --threshold 5
    "$GPY" Kinetikern2/tools/kk2_speed.py --v1                    # also time v1 on the same glyphs

Prints the phases as the plugin's progress bar would see them, the work the
engine did and skipped, the entries it produced, and peak memory.
"""

from __future__ import print_function

import argparse
import os
import resource
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kk2_fonts as kf  # noqa: E402
import kk2_bridge as kb  # noqa: E402


def cpu_seconds():
    """CPU time of this process, engine threads included (robust to a busy machine)."""
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def poll_until_done(job, label):
    t0 = time.time()
    phases = {}
    while True:
        state, phase, nphases, frac, elapsed = job.poll()
        if phase:
            phases.setdefault(phase, time.time() - t0)
        if state != kb.STATE_RUNNING:
            break
        time.sleep(0.01)
    if state != kb.STATE_DONE:
        raise SystemExit("%s ended in state %d: %s" % (label, state, job.error()))
    out = job.take()
    job.free()
    return out, time.time() - t0, phases


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--font", default="/System/Library/Fonts/Supplemental/Arial.ttf")
    ap.add_argument("--threshold", type=float, default=5.0, help="units per 1000 em")
    ap.add_argument("--budget", type=int, default=30000)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--pairs", action="store_true", help="glyph-pair mode instead of classes")
    ap.add_argument("--no-scope", action="store_true")
    ap.add_argument("--radius", type=float, default=-1.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--v1", action="store_true")
    args = ap.parse_args()

    t = time.time()
    lf = kf.LoadedFont(args.font, limit=args.limit or None)
    load_s = time.time() - t
    engine = kb.Engine(kf.DYLIB)
    print("%s: %d spacing glyphs, %d UPM, %d threads of %d cores (font read %.2f s)"
          % (os.path.basename(args.font), len(lf.names), lf.upm, args.threads or engine.default_threads,
             engine.cpu_count, load_s))
    t = time.time()
    packer = lf.packer()
    pack_s = time.time() - t
    c0 = cpu_seconds()
    ctx, prep_s, _ = poll_until_done(engine.prepare(packer, lf.upm, args.threads), "prepare")
    prep_cpu = cpu_seconds() - c0
    print("  Phase 1/3 analyzing SDFs   %6.2f s (packing %.2f s)" % (prep_s, pack_s))
    params = kb.make_params(classes=not args.pairs, window=True, scope_scripts=not args.no_scope,
                            threshold=args.threshold * lf.upm / 1000.0, budget=args.budget, threads=args.threads,
                            radius_ratio=args.radius)
    c0 = cpu_seconds()
    res, solve_s, phases = poll_until_done(engine.solve(ctx, params), "solve")
    solve_cpu = cpu_seconds() - c0
    st = res.stats
    p3 = phases.get(3, solve_s)
    print("  Phase 2/3 evaluating pairs %6.2f s" % (p3 - phases.get(2, 0.0)))
    print("  Phase 3/3 grouping/pruning %6.2f s" % (solve_s - p3))
    print("  total %.2f s (pass 1 %.0f ms, pass 2 %.0f ms, prune %.0f ms)"
          % (prep_s + solve_s, st["pass1_ms"], st["pass2_ms"], st["prune_ms"]))
    print("  cpu %.1f s (prepare %.1f, solve %.1f = %.1f us per pair in scope)"
          % (prep_cpu + solve_cpu, prep_cpu, solve_cpu, 1e6 * solve_cpu / max(1, st["pairs_in_scope"])))
    print("  pairs in scope %d (of %d²=%d); kerned glyphs %d; probes %d"
          % (st["pairs_in_scope"], len(lf.names), len(lf.names) ** 2, st["kern_glyphs"], st["probe_glyphs"]))
    if res.classes:
        print("  classes: right %d, left %d → %d class pairs solved; members %d: inherited %d, verified %d "
              "(%d within), solved %d" % (res.right_class_count, res.left_class_count, st["class_pairs"],
                                           st["member_pairs"], st["inherited"], st["verified"],
                                           st["verified_within"], st["solved"] - st["class_pairs"]))
    print("  force evaluations %d (%.2f per solved pair); settled by window %d, by bounds alone %d; fallbacks %d"
          % (st["force_evaluations"], st["force_evaluations"] / max(1.0, st["solved"]), st["window_hits"],
             st["bound_hits"], st["fallbacks"]))
    print("  entries %d (class pairs %d, exceptions %d); before budget %d, dropped %d"
          % (res.entry_count, st["class_entries"], st["exception_entries"], st["entries_before_budget"],
             st["dropped_by_budget"]))
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024.0 * 1024.0)
    print("  peak memory %.0f MB" % rss)
    if args.v1:
        sys.path.insert(0, kf.V1_RESOURCES)
        import kinetikern_bridge as v1
        e1 = v1.KineticEngine(kf.V1_DYLIB)
        geo = lf.v1_geometries()
        t = time.time()
        c1 = e1.context(geo, lf.upm)
        t_prep = time.time() - t
        t = time.time()
        r1 = c1.solve(1.0, 3.86, 1.0)
        print("  v1: prepare %.2f s, solve %.2f s, %d non-zero glyph pairs"
              % (t_prep, time.time() - t, len(r1.kerning)))
    res.close()
    ctx.close()


if __name__ == "__main__":
    main()
