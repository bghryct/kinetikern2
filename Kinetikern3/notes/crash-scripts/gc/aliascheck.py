# Dev script (DEVSCRIPT mode), run once Glyphs has started and loaded its plugins:
# which object each plugin module key ("__main__...") in sys.modules points to.
# Entries the C scanner (kk2scan) finds freed or typeless are reported, never
# touched from Python. Keys that share one object, and the reference counts of
# the modules sitecustomize created and holds (KK2_SITE_HOLD), go to
# aliascheck.txt; then Glyphs quits.
import os, sys, time, traceback
from AppKit import NSApp
import kk2_args

S = "/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad"
sys.path.insert(0, S + "/crash/scan")
import kk2scan

OUT = kk2_args.text("selfTestOut")
VICTIM = "__main__com_weiweihuanghuang_ShowDistanceAndAngleInCorner"
lines = []
open(os.path.join(OUT, "started"), "w").close()
try:
    bad, n = kk2scan.scan([sys.modules])
    badkeys = {b[2] for b in bad}
    for (_c, i, key, addr, reason, dump) in bad:
        lines.append("BAD entry %d key %r -> 0x%x reason %d dump %s" % (i, key, addr, reason, dump))
    lines.append("%d entries, %d bad" % (n, len(bad)))

    by_id = {}
    for k in [k for k in list(sys.modules) if k.startswith("__main__") and k not in badkeys]:
        v = sys.modules[k]
        by_id.setdefault(id(v), []).append(k)
        if k == VICTIM:
            lines.append("victim key -> 0x%x type %s __name__ %r refs %d"
                         % (id(v), type(v).__name__, getattr(v, "__name__", None), sys.getrefcount(v) - 2))
        del v
    shared = {a: ks for a, ks in by_id.items() if len(ks) > 1}
    lines.append("plugin module keys: %d, objects: %d" % (sum(len(ks) for ks in by_id.values()), len(by_id)))
    for a, ks in shared.items():
        o = sys.modules[ks[0]]
        lines.append("SHARED 0x%x (%s, __name__ %r): %r" % (a, type(o).__name__, getattr(o, "__name__", None), ks))
        del o
    refs = {}
    for k, ks in by_id.items():
        o = sys.modules[ks[0]]
        refs.setdefault(sys.getrefcount(o) - 2, []).append(ks[0])
        del o
    for r in sorted(refs):
        lines.append("refs %d: %d modules, e.g. %r" % (r, len(refs[r]), refs[r][:3]))

    try:
        import sitecustomize
        held = getattr(sitecustomize, "HELD", {})
    except Exception as e:
        held = {}
        lines.append("no sitecustomize: %r" % e)
    for name in sorted(held):
        m = held[name]
        lines.append("HELD %s: refs %d (sys.modules + HELD = 2 before any loader), still sys.modules[name]: %s, has __file__: %s"
                     % (name, sys.getrefcount(m) - 2, sys.modules.get(name) is m, "__file__" in m.__dict__))
        del m

except Exception:
    lines.append("ERROR\n" + traceback.format_exc())
with open(os.path.join(OUT, "aliascheck.txt"), "w") as f:
    f.write("\n".join(lines) + "\n")
open(os.path.join(OUT, "done"), "w").close()
NSApp.terminate_(None)
