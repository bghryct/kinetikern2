# devScript: open and close a font over and over (no Kinetikern2 at all),
# with a full collection + heap scan after each step.
#   devMode fpc    leave Font Proofer Companion running (its debounce timers
#                  read Glyphs.fonts on a background thread after each open/close)
#   devMode nofpc  stop Font Proofer Companion first
import gc, os, sys, time, random, threading, traceback, faulthandler
import objc
from AppKit import NSApp
from Foundation import NSObject, NSTimer, NSRunLoop, NSRunLoopCommonModes
from GlyphsApp import Glyphs
import kk2_args
sys.path.insert(0, "/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad/crash/scan")
import kk2scan

OUT = kk2_args.text("selfTestOut")
FONT = kk2_args.text("devFont")
MODE = kk2_args.text("devMode") or "fpc"
SECONDS = float(kk2_args.number("devSeconds", 300))
log = open(os.path.join(OUT, "openclose.txt"), "w", buffering=1)
S = {"t0": time.time(), "i": 0, "font": None, "next": 0.0, "threads": set()}

def finish(code):
    log.write("finish %d after %d cycles %.0f s; python threads seen %d\n" % (code, S["i"], time.time() - S["t0"], len(S["threads"])))
    open(os.path.join(OUT, "done"), "w").write(str(code))
    os._exit(code)

lo = id(type(None)) & 0xFFFFFFFF
bad_slide = bool(lo & (1 << 30)) and bool(lo & (1 << 25))
log.write("mode %s, NoneType at %#x, PyObjC nil-argument bug armed: %s\n" % (MODE, id(type(None)), bad_slide))
if bad_slide:
    finish(3)   # this launch would crash in Waterfall's setWindowController_(None) at a close

def stop_fpc():
    n = 0
    for o in gc.get_objects():
        t = type(o)
        if t.__name__ == "FontProoferCompanion" and t.__module__.startswith("fpcompanion"):
            try:
                o.stop(); n += 1
            except Exception as e:
                log.write("stop failed %r\n" % e)
    for t in threading.enumerate():
        if isinstance(t, threading.Timer):
            t.cancel()
    log.write("stopped %d Font Proofer Companion instance(s)\n" % n)

def check(where):
    gc.collect()
    bad, n = kk2scan.scan(gc.get_objects())
    for tid in sys._current_frames():
        S["threads"].add(tid)
    for t in threading.enumerate():
        if isinstance(t, threading.Timer) and t.ident not in S.setdefault("timers", set()):
            S["timers"].add(t.ident)
            fn = t.function
            log.write("timer thread %s: function %s.%s interval %s args %r\n" % (
                t.name, getattr(fn, "__module__", "?"), getattr(fn, "__qualname__", repr(fn)), t.interval, t.args[:3]))
    if bad:
        with open(os.path.join(OUT, "scan_report.txt"), "w") as f:
            f.write("FOUND %s cycle %d mode %s\n" % (where, S["i"], MODE))
            for (c, i, key, addr, reason, dump) in bad[:20]:
                f.write("container %s len %s index %d key %r -> 0x%x reason %d %s\n" % (
                    type(c).__name__, len(c) if hasattr(c, "__len__") else "?", i, key, addr, reason, dump))
                if isinstance(c, dict):
                    f.write("  keys %r\n  sys.modules: %s\n" % (list(c.keys())[:40], c is sys.modules))
                for r in gc.get_referrers(c)[:8]:
                    if isinstance(r, list) and len(r) > 1000:
                        continue
                    try:
                        f.write("  referrer %s %s\n" % (type(r).__name__, repr(r)[:200]))
                    except Exception as e:
                        f.write("  referrer %s (repr failed %r)\n" % (type(r).__name__, e))
        finish(70)

def step():
    now = time.time()
    if now - S["t0"] > SECONDS:
        finish(0)
    if now < S["next"]:
        return
    try:
        if S["font"] is None:
            S["i"] += 1
            S["font"] = Glyphs.open(FONT, showInterface=True)
            check("after open")
        else:
            S["font"].close(ignoreChanges=True)
            S["font"] = None
            check("after close")
        S["next"] = time.time() + random.uniform(0.2, 1.5)
        if S["i"] % 10 == 0 and S["font"] is None:
            log.write("%d cycles, %.0f s\n" % (S["i"], time.time() - S["t0"]))
    except Exception:
        log.write(traceback.format_exc())
        finish(71)

try:
    Ticker = objc.lookUpClass("KK2OpenCloseTicker")
except objc.nosuchclass_error:
    class KK2OpenCloseTicker(NSObject):
        def tick_(self, timer):
            fn = getattr(self, "fn", None)
            if fn is not None:
                fn()
    Ticker = KK2OpenCloseTicker

if MODE == "nofpc":
    stop_fpc()
check("start")
ticker = Ticker.alloc().init()
ticker.fn = step
timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.05, ticker, "tick:", None, True)
NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
sys.modules["__kk2_openclose_keep"] = (ticker, timer, log)
