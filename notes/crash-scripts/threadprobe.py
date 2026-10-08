# devScript: which Python code runs on threads other than the main thread?
# A sampler thread records every distinct stack of every other thread
# (sys._current_frames every 2 ms) while the script opens and closes a font.
import os, sys, time, threading, traceback
from AppKit import NSApp
from PyObjCTools import AppHelper
from GlyphsApp import Glyphs
import kk2_args

OUT = kk2_args.text("selfTestOut")
FONT = kk2_args.text("devFont")
log = open(os.path.join(OUT, "threadprobe.txt"), "w", buffering=1)
MAIN = threading.get_ident()
seen = set()
stop = [False]

def sampler():
    me = threading.get_ident()
    while not stop[0]:
        for tid, frame in sys._current_frames().items():
            if tid in (MAIN, me):
                continue
            stack = traceback.extract_stack(frame)
            key = tuple((f.filename, f.lineno, f.name) for f in stack)
            if key in seen:
                continue
            seen.add(key)
            name = [t.name for t in threading.enumerate() if t.ident == tid]
            log.write("%.3f thread %s %s\n%s\n" % (time.time(), tid, name, "".join(traceback.format_list(stack))))
        time.sleep(0.002)

threading.Thread(target=sampler, name="kk2-sampler", daemon=True).start()
state = {}

def step1():
    log.write("%.3f opening %s\n" % (time.time(), FONT))
    state["font"] = Glyphs.open(FONT, showInterface=True)
    AppHelper.callLater(15.0, step2)

def step2():
    log.write("%.3f closing\n" % time.time())
    state["font"].close(ignoreChanges=True)
    AppHelper.callLater(15.0, step3)

def step3():
    stop[0] = True
    log.write("%.3f done; threads now: %s\n" % (time.time(), [(t.name, t.ident, t.daemon) for t in threading.enumerate()]))
    open(os.path.join(OUT, "done"), "w").write("0")
    AppHelper.callLater(0.5, NSApp().terminate_, None)

AppHelper.callLater(1.0, step1)
