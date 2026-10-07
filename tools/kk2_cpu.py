#!/usr/bin/env python3
"""kk2_cpu — CPU seconds per configuration (robust to a busy machine)."""
import os, resource, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kk2_fonts as kf, kk2_bridge as kb

def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime

def main():
    fonts = sys.argv[1:] or ["/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Georgia.ttf"]
    e = kb.Engine(kf.DYLIB)
    for path in fonts:
        lf = kf.LoadedFont(path, limit=400)
        c0 = cpu()
        ctx = kf.run_job(e.prepare(lf.packer(True), lf.upm))
        prep = cpu() - c0
        row = ["%-16s prep %5.2f cpu-s" % (os.path.basename(path), prep)]
        for label, kw in (("ref", dict(window=False)), ("window", dict(window=True))):
            c0 = cpu(); t0 = time.time()
            r = kf.run_job(e.solve(ctx, kb.make_params(classes=False, scope_scripts=False, threshold=10.24, **kw)))
            n = r.stats["member_pairs"]
            row.append("%s %6.2f cpu-s (%5.1f us/pair cpu, %.1f s wall)" % (label, cpu() - c0, 1e6 * (cpu() - c0) / n, time.time() - t0))
            r.close()
        print(" | ".join(row))
        ctx.close()

if __name__ == "__main__":
    main()
