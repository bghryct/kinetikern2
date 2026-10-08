# devScript: exercise one part of Kinetikern2 in a loop inside a real Glyphs,
# with a full collection and a heap scan (kk2scan) after every iteration.
#   -com.mirkovelimirovic.Kinetikern2.devFont <font>
#   -com.mirkovelimirovic.Kinetikern2.devMode snapshot|engine|window|apply|idle
#   -com.mirkovelimirovic.Kinetikern2.devSeconds <s>
import gc, os, sys, time, traceback, faulthandler, random
import objc
from AppKit import NSApp
from Foundation import NSObject, NSTimer, NSRunLoop, NSRunLoopCommonModes
from GlyphsApp import Glyphs
import kk2_args

sys.path.insert(0, "/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad/crash/scan")
import kk2scan
import kk2_bridge as kb
import kk2_snapshot as ks

OUT = kk2_args.text("selfTestOut")
FONT = kk2_args.text("devFont")
MODE = kk2_args.text("devMode") or "snapshot"
SECONDS = float(kk2_args.number("devSeconds", 240))
log = open(os.path.join(OUT, "stress.txt"), "w", buffering=1)
S = {"i": 0, "t0": time.time(), "font": None, "win": None, "phase": "start", "found": False, "scans": 0,
     "snap": None, "engine": None, "keep": [], "last": time.time(), "applies": 0, "reverts": 0, "renders": 0}

def finish(code=0):
    log.write("finish %d after %d iterations, %.0f s, scans %d, applies %d reverts %d renders %d\n"
              % (code, S["i"], time.time() - S["t0"], S["scans"], S["applies"], S["reverts"], S["renders"]))
    open(os.path.join(OUT, "done"), "w").write(str(code))
    os._exit(code)

def check(where):
    t = time.perf_counter()
    gc.collect()
    bad, n = kk2scan.scan(gc.get_objects())
    S["scans"] += 1
    if bad:
        with open(os.path.join(OUT, "scan_report.txt"), "w") as f:
            f.write("FOUND %s iteration %d mode %s phase %s\n" % (where, S["i"], MODE, S["phase"]))
            for (c, i, key, addr, reason, dump) in bad[:20]:
                f.write("container %s len %s index %d key %r -> 0x%x reason %d %s\n" % (
                    type(c).__name__, len(c) if hasattr(c, "__len__") else "?", i, key, addr, reason, dump))
                if isinstance(c, dict):
                    f.write("  keys %r\n" % (list(c.keys())[:40],))
                for r in gc.get_referrers(c)[:8]:
                    if isinstance(r, list) and len(r) > 1000:
                        continue
                    try:
                        f.write("  referrer %s %s\n" % (type(r).__name__, repr(r)[:200]))
                    except Exception as e:
                        f.write("  referrer %s (repr failed %r)\n" % (type(r).__name__, e))
            f.write("".join(traceback.format_stack()))
        finish(70)
    return 1000 * (time.perf_counter() - t)

def step():
    if time.time() - S["t0"] > SECONDS:
        finish(0)
    try:
        globals()["step_" + MODE]()
    except SystemExit:
        raise
    except Exception:
        log.write("error: %s\n" % traceback.format_exc())
        finish(71)

# --- modes ------------------------------------------------------------------
def master():
    f = S["font"]
    return f.masters[0]

def step_idle():
    S["i"] += 1
    ms = check("idle")
    log.write("%d idle check %.0f ms\n" % (S["i"], ms))

def step_snapshot():
    S["i"] += 1
    t = time.time()
    r = ks.SnapshotReader(S["font"], master())
    snap = r.read_all()
    S["keep"] = [snap]          # keep one, drop the previous (as the window does)
    ms = check("snapshot")
    log.write("%d snapshot %d glyphs %.2f s, check %.0f ms\n" % (S["i"], len(snap.names), time.time() - t, ms))

def step_engine():
    S["i"] += 1
    if S["snap"] is None:
        S["snap"] = ks.SnapshotReader(S["font"], master()).read_all()
        S["engine"] = kb.Engine(os.path.join(RESOURCES, kb.DYLIB_NAME))
    snap, eng = S["snap"], S["engine"]
    t = time.time()
    job = eng.prepare(snap.packer, snap.upm)
    job.wait()
    ctx = job.take(); job.free()
    mask = bytearray(len(snap.names))
    for k in random.sample(range(len(snap.names)), min(60, len(snap.names))):
        mask[k] = 1
    for _ in range(3):
        job = eng.solve(ctx, kb.make_params(threshold=0.5), bytes(mask))
        job.wait()
        res = job.take(); job.free()
        n = sum(1 for _e in res.iter_entries())
        m = [res.metrics[i].lsb for i in range(min(50, res.glyph_count))]
        v = res.values(list(range(20)), list(range(20, 40)))
        res.close()
    # a cancelled job and a job freed while running
    job = eng.solve(ctx, kb.make_params(threshold=0.5), None)
    time.sleep(0.05); job.cancel(); job.wait(); job.free()
    job = eng.solve(ctx, kb.make_params(threshold=0.5), None)
    job.free()
    ctx.close()
    ms = check("engine")
    log.write("%d engine %.2f s entries %d, check %.0f ms\n" % (S["i"], time.time() - t, n, ms))

SAMPLES = ["Hamburgefonstiv HOHOHOHO nonononono\nAVATAR TYPE WAVE LT Tolerance Yo Ta Te Vo P. F, L’",
           "The quick brown fox jumps over the lazy dog. /T/o/T/a/T/e /V/A/V/o",
           "ÀÉÎÕÜ àéîõü ÆŒ æœ ß fi fl 0123456789 (H) [n] {o} «A» ‹V›",
           "Kinetikern2 proof /a/b/c/d/e/f/g/h/i/j/k/l/m/n/o/p/q/r/s/t/u/v/w/x/y/z"]

def step_window():
    win = S["win"]
    if win is None:
        import kk2_window
        S["win"] = kk2_window.open_window(RESOURCES)
        S["win"].w.getNSWindow().setHidesOnDeactivate_(False)
        S["win"].w.getNSWindow().orderFrontRegardless()
        return
    if win.state not in ("ready",) or win.panes_due:
        return
    S["i"] += 1
    r = random.random()
    if r < 0.15:
        win.reloadOutlines(None)
        what = "reload"
    elif r < 0.5:
        win.editor.set(random.choice(SAMPLES) * random.randint(1, 4))
        win.textChanged(None)
        what = "text"
    elif r < 0.7:
        win.w.size.set(random.randrange(len(win.w.size.getItems())))
        win.sizeChanged(None)
        what = "size"
    else:
        win.w.tightness.set(random.uniform(-1, 1))
        win.physicsChanged(None)
        what = "physics"
    S["renders"] += 1
    ms = check("window " + what)
    log.write("%d window %s check %.0f ms\n" % (S["i"], what, ms))

def step_apply():
    win = S["win"]
    if win is None:
        import kk2_window
        S["win"] = kk2_window.open_window(RESOURCES)
        S["win"].w.getNSWindow().setHidesOnDeactivate_(False)
        S["win"].w.getNSWindow().orderFrontRegardless()
        S["phase"] = "wait"
        return
    if win.state != "ready" or win.panes_due or win._stepper is not None:
        return
    S["i"] += 1
    if S["phase"] in ("wait", "reverted"):
        if win._font_written:
            win.reloadOutlines(None)  # the window re-reads after a write before the next apply
            S["phase"] = "reverted-reload"
            return
        ok = win._apply(win.result, confirm=False)
        S["phase"] = "applying" if ok else S["phase"]
        S["applies"] += bool(ok)
        what = "apply %s" % ok
    elif S["phase"] == "reverted-reload":
        ok = win._apply(win.result, confirm=False)
        S["phase"] = "applying" if ok else S["phase"]
        S["applies"] += bool(ok)
        what = "apply %s" % ok
    else:
        ok = win.revert_last_apply()
        S["phase"] = "reverted" if ok else S["phase"]
        S["reverts"] += bool(ok)
        what = "revert %s" % ok
    ms = check(what)
    log.write("%d %s check %.0f ms\n" % (S["i"], what, ms))

# --- driver -------------------------------------------------------------------
try:
    Ticker = objc.lookUpClass("KK2StressTicker")
except objc.nosuchclass_error:
    class KK2StressTicker(NSObject):
        def tick_(self, timer):
            fn = getattr(self, "fn", None)
            if fn is not None:
                fn()
    Ticker = KK2StressTicker

def start():
    S["font"] = Glyphs.open(FONT, showInterface=True)
    log.write("mode %s font %s glyphs %d faulthandler %s\n" % (MODE, FONT, len(S["font"].glyphs), faulthandler.is_enabled()))
    ms = check("after open")
    log.write("initial check %.0f ms\n" % ms)
    ticker = Ticker.alloc().init()
    ticker.fn = step
    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.05, ticker, "tick:", None, True)
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    sys.modules["__kk2_stress_keep"] = (ticker, timer, log)

try:
    start()
except Exception:
    log.write("start failed: %s\n" % traceback.format_exc())
    finish(72)
