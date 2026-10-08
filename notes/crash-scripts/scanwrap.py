# devScript: kk2_selftest with a heap-consistency scanner. At the start of
# every garbage collection the generations about to be collected are scanned
# for dict/list/tuple slots that point to dead memory; a timer scans the whole
# heap every SCAN_EVERY seconds; every KK2Window tick is followed by a scan of
# the young generations. The first finding is written to scan_report.txt
# (container, key, owner, allocation traceback, Python stacks) and the
# instance exits. Other Python threads are logged to threads.txt.
import gc, os, sys, time, traceback, faulthandler, threading
import objc
from Foundation import NSObject, NSTimer, NSRunLoop, NSRunLoopCommonModes
import kk2_args

sys.path.insert(0, "/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad/crash/scan")
import kk2scan
import kk2_selftest

OUT = kk2_args.text("selfTestOut")
SCAN_EVERY = float(kk2_args.number("scanEvery", 5.0))
log = open(os.path.join(OUT, "scanlog.txt"), "w", buffering=1)
tlog = open(os.path.join(OUT, "threads.txt"), "w", buffering=1)
S = {"test": None, "busy": False, "n": 0, "full": 0, "found": False, "seen_threads": set(), "ticks": []}
MAIN = threading.get_ident()

_orig_init = kk2_selftest.SelfTest.__init__
def _init(self, *a, **k):
    _orig_init(self, *a, **k)
    S["test"] = self
kk2_selftest.SelfTest.__init__ = _init

def stage():
    t = S["test"]
    if t is None:
        return "-"
    try:
        win = t.win
        return "%s | %s | %s" % (t.heartbeat.stage_name, getattr(win, "state", None),
                                 getattr(win, "last_tick", None) if win is not None else None)
    except Exception as e:
        return "? %r" % e

def short(o, n=160):
    try:
        r = repr(o)
    except Exception as e:
        r = "<repr failed %r>" % e
    return r[:n]

def found(bad, where):
    if S["found"]:
        return
    S["found"] = True
    path = os.path.join(OUT, "scan_report.txt")
    with open(path, "w") as f:
        f.write("FOUND during %s at %.3f, stage %s\n" % (where, time.time(), stage()))
        f.write("last window ticks: %r\n" % (S["ticks"][-30:],))
        for (c, i, key, addr, reason, dump) in bad[:20]:
            f.write("\ncontainer %s at 0x%x len %d: index %d key %r -> 0x%x reason %d dump %s\n"
                    % (type(c).__name__, id(c), len(c) if hasattr(c, "__len__") else -1, i, key, addr, reason, dump))
            if isinstance(c, dict):
                ks = list(c.keys())
                f.write("  keys (%d): first %s\n  around: %s\n" % (len(ks), short(ks[:30], 1500), short(ks[max(0, i - 5):i + 5], 800)))
                f.write("  is sys.modules: %s\n" % (c is sys.modules))
            try:
                import tracemalloc
                tb = tracemalloc.get_object_traceback(c)
                if tb is not None:
                    f.write("  container allocated at:\n    " + "\n    ".join(tb.format()) + "\n")
            except Exception as e:
                f.write("  tracemalloc: %r\n" % e)
            try:
                refs = gc.get_referrers(c)
                for r in refs[:12]:
                    if r is bad or isinstance(r, list) and len(r) > 1000:
                        continue
                    f.write("  referrer %s: %s\n" % (type(r).__name__, short(r)))
                    if type(r).__name__ not in ("list", "tuple", "dict", "frame"):
                        try:
                            import tracemalloc
                            tb = tracemalloc.get_object_traceback(r)
                            if tb is not None:
                                f.write("    owner allocated at:\n      " + "\n      ".join(tb.format()) + "\n")
                        except Exception:
                            pass
            except Exception as e:
                f.write("  referrers failed %r\n" % e)
        f.write("\nPython stack:\n" + "".join(traceback.format_stack()))
        f.flush()
        faulthandler.dump_traceback(file=f, all_threads=True)
    log.write("FOUND, report written\n")
    os._exit(70)

def do_scan(gens, where):
    if S["busy"] or S["found"]:
        return
    S["busy"] = True
    try:
        objs = []
        for g in gens:
            objs.extend(gc.get_objects(generation=g))
        bad, n = kk2scan.scan(objs)
        del objs
        if bad:
            found(bad, where)
    finally:
        S["busy"] = False

def gc_cb(phase, info):
    if phase == "start":
        S["n"] += 1
        if info["generation"] >= 1:
            do_scan(range(info["generation"] + 1), "gc start gen%d #%d" % (info["generation"], S["n"]))

def threads_probe():
    for tid, frame in sys._current_frames().items():
        if tid == MAIN or tid in S["seen_threads"]:
            continue
        S["seen_threads"].add(tid)
        tlog.write("%.3f thread %s (%s) stage %s\n%s\n" % (
            time.time(), tid, [t.name for t in threading.enumerate() if t.ident == tid], stage(),
            "".join(traceback.format_stack(frame))))

def timer_tick():
    t0 = time.perf_counter()
    S["full"] += 1
    do_scan(range(3), "timer full scan #%d" % S["full"])
    threads_probe()
    log.write("%.3f full scan #%d %.0f ms, gcs %d, %s\n" % (time.time(), S["full"], 1000 * (time.perf_counter() - t0), S["n"], stage()))

def patch_window():
    import kk2_window
    orig = kk2_window.KK2Window._tick
    def _tick(self):
        try:
            return orig(self)
        finally:
            S["ticks"].append(getattr(self, "last_tick", None))
            if len(S["ticks"]) > 200:
                del S["ticks"][:100]
    kk2_window.KK2Window._tick = _tick

try:
    Ticker = objc.lookUpClass("KK2ScanTicker")
except objc.nosuchclass_error:
    class KK2ScanTicker(NSObject):
        def tick_(self, timer):
            fn = getattr(self, "fn", None)
            if fn is not None:
                fn()
    Ticker = KK2ScanTicker

ticker = Ticker.alloc().init()
ticker.fn = timer_tick
timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(SCAN_EVERY, ticker, "tick:", None, True)
NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
sys.modules["__kk2_scan_keep"] = (ticker, timer, log, tlog)
gc.callbacks.insert(0, gc_cb)
patch_window()
import tracemalloc
log.write("scanwrap: every %.2f s, tracemalloc %s, faulthandler %s\n" % (SCAN_EVERY, tracemalloc.is_tracing(), faulthandler.is_enabled()))
do_scan(range(3), "initial")
log.write("initial scan clean\n")
kk2_selftest.run(RESOURCES, PLUGIN)
