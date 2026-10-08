# devScript: run kk2_selftest with frequent full garbage collections so that a
# dangling pointer in any container is hit soon after it appears. Logs the
# stage before each collection (flushed), so a crash can be placed in time.
import gc, os, sys, time, faulthandler
import objc
from Foundation import NSObject, NSTimer, NSRunLoop, NSRunLoopCommonModes
import kk2_args
import kk2_selftest

out = kk2_args.text("selfTestOut")
interval = float(kk2_args.number("gcInterval", 0.25))
log = open(os.path.join(out, "gclog.txt"), "w", buffering=1)
state = {"n": 0, "test": None}

_orig_init = kk2_selftest.SelfTest.__init__
def _init(self, *a, **k):
    _orig_init(self, *a, **k)
    state["test"] = self
kk2_selftest.SelfTest.__init__ = _init

def describe():
    t = state["test"]
    if t is None:
        return "-"
    try:
        hb = t.heartbeat.stage_name
        win = t.win
        tick = getattr(win, "last_tick", None) if win is not None else None
        return "%s | %s | %s" % (hb, getattr(win, "state", None), tick)
    except Exception as e:
        return "describe failed %r" % e

def collect():
    state["n"] += 1
    t0 = time.perf_counter()
    log.write("%.3f #%d before %s\n" % (time.time(), state["n"], describe()))
    gc.collect()
    log.write("%.3f #%d after %.1f ms\n" % (time.time(), state["n"], 1000 * (time.perf_counter() - t0)))

try:
    Ticker = objc.lookUpClass("KK2GCStressTicker")
except objc.nosuchclass_error:
    class KK2GCStressTicker(NSObject):
        def tick_(self, timer):
            fn = getattr(self, "fn", None)
            if fn is not None:
                fn()
    Ticker = KK2GCStressTicker

ticker = Ticker.alloc().init()
ticker.fn = collect
timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(interval, ticker, "tick:", None, True)
NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
sys.modules["__kk2_gcstress_keep"] = (ticker, timer, log)
log.write("gcstress interval %.3f allocator faulthandler=%s\n" % (interval, faulthandler.is_enabled()))
kk2_selftest.run(RESOURCES, PLUGIN)
