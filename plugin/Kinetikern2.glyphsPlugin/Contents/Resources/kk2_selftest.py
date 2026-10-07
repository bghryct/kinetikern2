# encoding: utf-8
"""
kk2_selftest — verify an installation inside Glyphs without clicking.

`build.sh --verify` starts a temporary second Glyphs 3 instance with the
test's parameters in the argument domain, so nothing is written to the user's
preferences and the user's own Glyphs is never touched:

    open -n -a "Glyphs 3" --args -ApplePersistenceIgnoreState YES \\
        -com.mirkovelimirovic.Kinetikern2.selfTestFont   /path/to/Font.ttf \\
        -com.mirkovelimirovic.Kinetikern2.selfTestOut    /path/to/folder \\
        -com.mirkovelimirovic.Kinetikern2.selfTestQuit   YES \\
        -com.mirkovelimirovic.Kinetikern2.selfTestWhole  YES \\
        -com.mirkovelimirovic.Kinetikern2.selfTestCancel YES

A few seconds after launch plugin.py calls run(). The test opens the font (a
.glyphs file or .glyphspackage is copied to a temporary folder first, a
.ttf/.otf is imported), chooses Filter ▸ Kinetikern2… the way a click does,
waits for the live preview and chooses the item again (the open window must
come to the front, not a second one).

    selfTestCancel   start a whole-font run, cancel it at about 30 % and check
                     that the window settles, then run the whole font again
    selfTestWhole    run the whole font, apply the result without the
                     confirmation dialog, read the font back, Revert Last Apply
                     and check that the master's kerning and the spacing
                     glyphs' groups and sidebearings are exactly as before
    selfTestMaxStall longest tolerated main-thread stall in ms (default 500)

Then it saves window.png and selftest.json into selfTestOut, closes the window
and the font without saving and, with selfTestQuit, quits Glyphs.

All waiting is done by polling from timers; nothing blocks the main thread. A
60 Hz heartbeat timer in the common run-loop modes measures the gaps between
its ticks, i.e. how long the main thread was busy at a time. The test's own
work (captures, read-backs) is taken out of the gaps; its synchronous calls
into the window (menu choice, start, Cancel, Apply, Revert, close) are timed
one by one. Everything is kept per stage. Stages in which the window works by
itself (preview, whole-font runs, Apply and Revert, which write in slices)
must stay under selfTestMaxStall; opening and closing the window are reported
but not judged.

The window is kept on screen for the whole test (a floating window would hide
while the test instance is in the background), so drawing it counts in the
stalls and window.png is a screen capture. Each stage's longest gap notes what
the window was doing (state, its latest timer tick); window_ticks_ms has the
window's longest tick per kind of work and gc Python's collections (a full one
over Glyphs' shared interpreter is a stall of its own, outside the window).

    selftest.json  {ok, steps: [{step, t, ...}], max_stall_ms, stalls, states,
                    window_ticks_ms, gc, summary, errors, warnings}
"""

from __future__ import division, print_function, unicode_literals

import gc
import json
import math
import os
import re
import shutil
import tempfile
import time
import traceback
import weakref
from contextlib import contextmanager

import Foundation
import objc
from AppKit import (NSApp, NSBitmapImageFileTypePNG, NSBitmapImageRep, NSObject, NSProgressIndicator,
                    NSTextField)
from Foundation import NSProcessInfo, NSRunLoop, NSRunLoopCommonModes, NSTimer
from PyObjCTools import AppHelper

from GlyphsApp import FILTER_MENU, LTR, Glyphs

import kk2_args
import kk2_bridge as kb
import kk2_snapshot as ks

PREFIX = "com.mirkovelimirovic.Kinetikern2."
FONT_KEY = PREFIX + "selfTestFont"
OUT_KEY = PREFIX + "selfTestOut"
QUIT_KEY = PREFIX + "selfTestQuit"
WHOLE_KEY = PREFIX + "selfTestWhole"
CANCEL_KEY = PREFIX + "selfTestCancel"
MAX_STALL_KEY = PREFIX + "selfTestMaxStall"

DEFAULT_OUT = os.path.expanduser("~/Desktop/Kinetikern2-selftest")
DEFAULT_MAX_STALL_MS = 500.0
DEADLINE_S = 540.0      # selftest.json is written before build.sh stops waiting (10 min after launch)
SETTLE_S = 1.5          # lets Glyphs draw the new font's window before anything is measured
CANCEL_AT = 0.3
KERNING_READBACK = 200
NOT_FOUND = 1e9         # Glyphs answers a missing kerning entry with NSNotFound (as a float)
MODAL_GRACE_S = 2.0

CLASSIC = ["AV", "AT", "LT", "To", "Te", "Ta", "Yo", "VA", "P.", "F.", "r.", "y.", "HH", "nn", "oo", "HO"]
SAMPLE_METRICS = "HOnoAVTL."

# Stages of the test. In the judged ones the window works by itself.
OPEN, PREVIEW, AGAIN = "open window", "preview", "menu again"
CANCELLED_RUN, WHOLE_RUN = "cancelled run", "whole-font run"
APPLY, REVERT, CLOSE = "apply", "revert", "close"
JUDGED = (PREVIEW, AGAIN, CANCELLED_RUN, WHOLE_RUN, APPLY, REVERT)
BUSY_STATES = ("reading", "preparing", "previewing", "solving", "applying")

PHASE_TEXT = re.compile(r"Phase\s*(\d+)\s*/\s*(\d+).*?\[\s*(\d+(?:\.\d+)?)\s*%\s*\]")
READING_TEXT = re.compile(r"Reading outlines.*?\[\s*(\d+(?:\.\d+)?)\s*%\s*\]")


def _class(name, factory):
    """One Objective-C class per process: Glyphs runs every plugin in one runtime."""
    try:
        return objc.lookUpClass(name)
    except objc.nosuchclass_error:
        return factory()


def _make_ticker_class():
    class KK2SelfTestTicker(NSObject):
        """Target of the heartbeat timer; calls back into Python."""

        @objc.python_method
        def setCallback(self, fn):
            self._kk2_callback = fn

        def tick_(self, timer):
            fn = getattr(self, "_kk2_callback", None)
            if fn is not None:
                fn()

    return KK2SelfTestTicker


KK2SelfTestTicker = _class("KK2SelfTestTicker", _make_ticker_class)


def _argument(key):
    """The key (PREFIX + name) without its prefix, for kk2_args: the test's
    parameters are read from the argument domain only, never from the
    preferences."""
    return key[len(PREFIX):]


def _text(value):
    """NSString or None → str or None (empty → None)."""
    return str(value) if value else None


def _clean(obj):
    """JSON-safe copy: non-finite floats become null, tuples lists."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return dict((str(k), _clean(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _freed(obj):
    """True when an engine object (Context, Result, Job) is gone or closed."""
    return obj is None or not getattr(obj, "ptr", None)


def _views(root):
    stack = [root] if root is not None else []
    while stack:
        view = stack.pop()
        yield view
        stack.extend(view.subviews() or ())


def _labels(nswindow):
    """The texts a window shows in its labels (non-editable text fields)."""
    if nswindow is None:
        return []
    out = []
    for view in _views(nswindow.contentView()):
        if isinstance(view, NSTextField) and not view.isEditable():
            text = str(view.stringValue() or "").strip()
            if text:
                out.append(text)
    return out


def find_menu_item(title):
    """(menu, index, top-level menu title) of the main-menu item called title."""
    main = NSApp().mainMenu()
    for t in range(main.numberOfItems()):
        top = main.itemAtIndex_(t)
        sub = top.submenu()
        if sub is None:
            continue
        for i in range(sub.numberOfItems()):
            if sub.itemAtIndex_(i).title() == title:
                return sub, i, top.title()
    return None


def kerning_table(font, master_id):
    """The master's LTR kerning as a plain {left key: {right key: value}}
    (rows Glyphs keeps empty are dropped)."""
    try:
        whole = font.kerningLTR
        table = whole[master_id] if whole is not None else None
    except (KeyError, IndexError):
        table = None
    out = {}
    if table is None:
        return out
    for left in table.keys():
        row = table[left]
        if not row:
            continue
        out[str(left)] = dict((str(right), float(row[right])) for right in row.keys())
    return out


def count_entries(table):
    return sum(len(row) for row in table.values())


def diff_kerning(before, after):
    """[left, right, before, after] for every entry that differs."""
    out = []
    for left in sorted(set(before) | set(after)):
        a, b = before.get(left, {}), after.get(left, {})
        for right in sorted(set(a) | set(b)):
            va, vb = a.get(right), b.get(right)
            if va is None or vb is None or abs(va - vb) > 1e-6:
                out.append([left, right, va, vb])
    return out


GLYPH_FIELDS = ("left group", "right group", "LSB", "RSB", "width")


def ink_metrics(layer):
    """(LSB, RSB, width) measured on the ink of the decomposed outline, the
    frame the engine and the proof panes use. Unlike layer.LSB / RSB, which
    Glyphs caches and does not refresh for a composite whose base moved, the
    outline is always current. Sidebearings are None for a glyph without ink."""
    width = float(layer.width)
    path = ks.layer_path(layer)
    if path is None or not path.elementCount():
        return None, None, width
    r = path.bounds()
    x = float(r.origin.x)
    return x, width - (x + float(r.size.width)), width


def glyph_metrics(layer):
    """ink_metrics(), or Glyphs' own LSB / RSB for a glyph without ink."""
    lsb, rsb, width = ink_metrics(layer)
    if lsb is None:
        lsb, rsb = getattr(layer, "LSB", None), getattr(layer, "RSB", None)
    return lsb, rsb, width


def diff_glyphs(before, after):
    """[name, what, before, after] for every group or sidebearing that differs."""
    out = []
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name), after.get(name)
        if a is None or b is None:
            out.append([name, "glyph", a is not None, b is not None])
            continue
        for k, field in enumerate(GLYPH_FIELDS):
            va, vb = a[k], b[k]
            if k < 2 or va is None or vb is None:
                same = va == vb
            else:
                same = abs(va - vb) <= 0.01
            if not same:
                out.append([name, field, va, vb])
    return out


class Heartbeat(object):
    """A 60 Hz timer in the common run-loop modes (it fires during menu
    tracking, slider drags and modal panels). The gap between two ticks, less
    the time the test spent in its own callbacks, is how long something else
    (the window's timers, Glyphs) kept the main thread busy. Gaps and timed
    calls are kept per stage."""

    INTERVAL = 1.0 / 60.0

    def __init__(self, on_tick, describe=None):
        self.on_tick = on_tick
        self.describe = describe  # () -> what the window was doing (noted with each stage's longest gap)
        self.stage_name = "start"
        self.stages = {}
        self.t0 = time.time()
        self.last = None
        self.own = 0.0
        self.timer = None
        self.ticker = KK2SelfTestTicker.alloc().init()
        self.ticker.setCallback(self._tick)

    def start(self):
        self.last = time.perf_counter()
        self.own = 0.0
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            self.INTERVAL, self.ticker, "tick:", None, True)
        self.timer.setTolerance_(0.0)
        NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def stage(self, name):
        self.stage_name = name

    def exclude(self, seconds):
        """Time the test spent in a callback of its own since the last tick."""
        self.own += seconds

    def charge(self, stage, seconds):
        """A synchronous call into the window, timed by the test: one gap of
        its own in the stage it belongs to."""
        self._record(stage, seconds * 1000.0)

    def _record(self, stage, ms):
        s = self.stages.get(stage)
        if s is None:
            s = self.stages[stage] = {"ticks": 0, "max_ms": 0.0, "at": None, "over_50ms": 0, "over_100ms": 0,
                                      "over_250ms": 0}
        s["ticks"] += 1
        if ms > s["max_ms"]:
            s["max_ms"] = round(ms, 1)
            s["at"] = round(time.time() - self.t0, 3)
            if self.describe is not None:
                try:
                    s["doing"] = self.describe()
                except Exception:
                    s["doing"] = None
        for limit in (50, 100, 250):
            if ms > limit:
                s["over_%dms" % limit] += 1

    def _tick(self):
        now = time.perf_counter()
        gap = (now - self.last - self.own) * 1000.0
        self.last = now
        self.own = 0.0
        self._record(self.stage_name, max(gap, 0.0))
        try:
            self.on_tick()
        except Exception:
            print(traceback.format_exc())

    def stop(self):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        self.ticker.setCallback(None)

    def worst(self, stages):
        """(longest gap in ms, its stage) over the given stages."""
        best = (0.0, None)
        for name in stages:
            s = self.stages.get(name)
            if s is not None and s["max_ms"] > best[0]:
                best = (s["max_ms"], name)
        return best


class GCWatch(object):
    """Python's garbage collections while the test runs: count and longest
    pause per generation, with the stage and window work of the longest, so a
    stall can be told apart from the window's own work."""

    def __init__(self, heartbeat, describe):
        self.heartbeat = heartbeat
        self.describe = describe
        self.started = None
        self.gens = {}

    def start(self):
        gc.callbacks.append(self.callback)

    def stop(self):
        if self.callback in gc.callbacks:
            gc.callbacks.remove(self.callback)

    def callback(self, phase, info):
        if phase == "start":
            self.started = time.perf_counter()
            return
        if self.started is None:
            return
        ms = (time.perf_counter() - self.started) * 1000.0
        self.started = None
        g = self.gens.setdefault(str(info.get("generation")), {"count": 0, "max_ms": 0.0, "total_ms": 0.0})
        g["count"] += 1
        g["total_ms"] = round(g["total_ms"] + ms, 1)
        if ms > g["max_ms"]:
            g["max_ms"] = round(ms, 1)
            g["stage"] = self.heartbeat.stage_name
            try:
                g["doing"] = self.describe()
            except Exception:
                g["doing"] = None


class SelfTest(object):

    def __init__(self, resources, plugin):
        self.resources = resources
        self.plugin = plugin
        self.font_path = kk2_args.text(_argument(FONT_KEY))
        self.out = kk2_args.text(_argument(OUT_KEY)) or DEFAULT_OUT
        self.quit = kk2_args.flag(_argument(QUIT_KEY))
        self.whole = kk2_args.flag(_argument(WHOLE_KEY))
        self.cancel = kk2_args.flag(_argument(CANCEL_KEY))
        self.max_stall = kk2_args.number(_argument(MAX_STALL_KEY), DEFAULT_MAX_STALL_MS)
        self.t0 = time.time()
        self.report = {
            "ok": False, "font": self.font_path, "out": self.out,
            "glyphs_version": "%s (%s)" % (Glyphs.versionString, Glyphs.buildNumber),
            "options": {"whole": self.whole, "cancel": self.cancel, "quit": self.quit,
                        "max_stall_limit_ms": self.max_stall},
            "steps": [], "states": [], "summary": [], "errors": [], "warnings": []}
        self.heartbeat = Heartbeat(self.on_tick, self.window_doing)
        self.gc_watch = GCWatch(self.heartbeat, self.window_doing)
        self.depth = 0
        self.font = None
        self.temp_dir = None
        self.win = None
        self.window_t = None
        self.last_state = None
        self.modal_since = None
        self.activity = None
        self.finished = False
        self.preview_result = None
        self.preview_kerned = 0
        self.whole_result = None
        self.before_run = None
        self.plan = None
        self.before = None
        self.phase_times = {}

    # --- bookkeeping -------------------------------------------------------

    def now(self):
        return round(time.time() - self.t0, 3)

    def log(self, step, **info):
        info["step"] = step
        info["t"] = self.now()
        self.report["steps"].append(_clean(info))

    def note(self, line):
        """A line of the human summary build.sh prints."""
        self.report["summary"].append(line)

    def error(self, message):
        self.report["errors"].append(message)

    def warn(self, message):
        self.report["warnings"].append(message)

    @contextmanager
    def own_time(self):
        """Marks the test's own work: it is not a stall of the window."""
        self.depth += 1
        t = time.perf_counter()
        try:
            yield
        finally:
            self.depth -= 1
            if self.depth == 0:
                self.heartbeat.exclude(time.perf_counter() - t)

    def call_window(self, stage, fn, *args, **kwargs):
        """A synchronous call into the window, charged to stage."""
        t = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            self.heartbeat.charge(stage, time.perf_counter() - t)

    def later(self, delay, fn, *args):
        AppHelper.callLater(delay, self.guarded, fn, *args)

    def guarded(self, fn, *args):
        if self.finished:
            return
        with self.own_time():
            try:
                fn(*args)
            except Exception:
                self.fail()

    def wait(self, condition, then, timeout, what, interval=0.1, started=None):
        """Poll condition() from a timer until it holds, then call then()."""
        if self.finished:
            return
        started = started or time.time()
        with self.own_time():
            try:
                if condition():
                    then()
                    return
                if time.time() - started > timeout:
                    raise RuntimeError("timed out after %.0f s %s (window state %r; window shows: %s)"
                                       % (timeout, what, getattr(self.win, "state", None), self.window_text()))
                if time.time() - self.t0 > DEADLINE_S:
                    raise RuntimeError("the self-test ran out of time (%.0f s) %s (window state %r)"
                                       % (DEADLINE_S, what, getattr(self.win, "state", None)))
                AppHelper.callLater(interval, self.wait, condition, then, timeout, what, interval, started)
            except Exception:
                self.fail()

    def window_state(self, what):
        """The window's state; raises when it reports an error."""
        state = getattr(self.win, "state", None)
        if state == "error":
            raise RuntimeError("the window reports an error %s: %s" % (what, self.window_text()))
        return state

    def nswindow(self):
        w = getattr(self.win, "w", None)
        return w.getNSWindow() if w is not None else None

    def window_doing(self):
        """The window's state and its latest timer tick ("kind of work", ms):
        a long gap right after a short tick was spent outside the window
        (Glyphs drawing, the run loop)."""
        win = self.win
        if win is None:
            return None
        tick = getattr(win, "last_tick", None)
        return [getattr(win, "state", None), tick[0] if tick else None, round(tick[1], 1) if tick else None]

    def window_text(self):
        return " | ".join(_labels(self.nswindow()))[:600] or "-"

    def window_progress(self):
        """(phase, fraction, text) as the window shows them: from the phase
        text when there is one ("Phase 2/3: Evaluating pairs [80%]"; phase 0
        for "Reading outlines [42%]"), otherwise (None, fraction, "") from the
        progress bar; (None, None, "") when neither is found."""
        nswindow = self.nswindow()
        if nswindow is None:
            return None, None, ""
        bar = None
        for view in _views(nswindow.contentView()):
            if isinstance(view, NSTextField):
                text = str(view.stringValue() or "")
                m = PHASE_TEXT.search(text)
                if m:
                    return int(m.group(1)), float(m.group(3)) / 100.0, text.strip()
                m = READING_TEXT.search(text)
                if m:
                    return 0, float(m.group(1)) / 100.0, text.strip()
            elif bar is None and isinstance(view, NSProgressIndicator) and not view.isIndeterminate():
                bar = view
        if bar is not None:
            span = bar.maxValue() - bar.minValue()
            if span > 0:
                return None, (bar.doubleValue() - bar.minValue()) / span, ""
        return None, None, ""

    def track_progress(self, started):
        """Notes when each phase first shows up in the window."""
        phase, fraction, text = self.window_progress()
        if phase is not None:
            key = "reading" if phase == 0 else "phase %d" % phase
            self.phase_times.setdefault(key, round(time.time() - started, 2))
        return phase, fraction, text

    def on_tick(self):
        if self.win is not None:
            state = getattr(self.win, "state", None)
            if state != self.last_state:
                self.last_state = state
                self.report["states"].append([self.now(), state])
        self.watch_modal()

    def watch_modal(self):
        """An unattended run has nobody to answer an alert: record it and abort
        it, so that the test ends instead of hanging."""
        modal = NSApp().modalWindow()
        if modal is None:
            self.modal_since = None
            return
        if self.modal_since is None:
            self.modal_since = time.time()
        elif time.time() - self.modal_since > MODAL_GRACE_S:
            self.modal_since = None
            self.error("an unexpected modal window was aborted: %s" % (" | ".join(_labels(modal))[:600] or "-"))
            NSApp().abortModal()

    def open_windows(self):
        """The plugin's open windows as kk2_window keeps them: every window
        object in a module-level list or dict whose NSWindow is still there
        (a floating window hides while Glyphs is in the background, so
        visibility is no test)."""
        import kk2_window
        cls = type(self.win)
        found = []
        for value in list(vars(kk2_window).values()):
            if isinstance(value, dict):
                items = list(value.values())
            elif isinstance(value, (list, tuple, set)):
                items = list(value)
            else:
                continue
            for x in items:
                if isinstance(x, weakref.ref):
                    x = x()
                w = getattr(x, "w", None)
                if (isinstance(x, cls) and w is not None and w.getNSWindow() is not None
                        and not any(x is y for y in found)):
                    found.append(x)
        return found

    # --- the steps ---------------------------------------------------------

    def start(self):
        with self.own_time():
            try:
                if not self.font_path:
                    raise RuntimeError("no font: set %s" % FONT_KEY)
                os.makedirs(self.out, exist_ok=True)
                # App Nap would throttle the timers of a Glyphs in the
                # background and make every tick look like a stall.
                options = (getattr(Foundation, "NSActivityUserInitiatedAllowingIdleSystemSleep", 0x00EFFFFF) |
                           getattr(Foundation, "NSActivityLatencyCritical", 0xFF00000000))
                self.activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                    options, "Kinetikern2 self-test")
                engine = kb.Engine(os.path.join(self.resources, kb.DYLIB_NAME))
                self.report["engine"] = {"version": engine.version, "cpu_count": engine.cpu_count,
                                         "default_threads": engine.default_threads}
                self.open_font()
                self.later(SETTLE_S, self.open_window)
            except Exception:
                self.fail()

    def open_font(self):
        path = self.font_path
        if not os.path.exists(path):
            raise RuntimeError("font not found: %s" % path)
        opened = path
        if path.lower().rstrip("/").endswith((".glyphs", ".glyphspackage")):
            # never open the original: the test applies kerning to the document
            self.temp_dir = tempfile.mkdtemp(prefix="kk2-selftest-")
            opened = os.path.join(self.temp_dir, os.path.basename(path.rstrip("/")))
            if os.path.isdir(path):
                shutil.copytree(path, opened)
            else:
                shutil.copy2(path, opened)
        t = time.time()
        self.font = Glyphs.open(opened, showInterface=True)
        if self.font is None:
            raise RuntimeError("Glyphs could not open %s" % opened)
        self.log("opened", path=opened, seconds=round(time.time() - t, 2), glyphs=len(self.font.glyphs),
                 masters=len(self.font.masters), upm=self.font.upm, family=_text(self.font.familyName))

    def open_window(self):
        self.heartbeat.start()
        self.gc_watch.start()
        self.heartbeat.stage(OPEN)
        self.window_t = time.time()
        # the first choice imports the window's modules: timed on their own
        t = time.perf_counter()
        import kk2_window  # noqa: F401
        self.import_ms = round((time.perf_counter() - t) * 1000.0, 1)
        self.win = self.choose_menu_item("menu", OPEN)
        nswindow = self.nswindow()
        if nswindow is not None:
            # A floating window hides while its app is in the background, and
            # a test instance usually is: keep it on screen, so that drawing it
            # counts in the stalls and window.png is what the screen shows.
            nswindow.setHidesOnDeactivate_(False)
            nswindow.orderFrontRegardless()
        self.log("window", title=_text(nswindow.title()) if nswindow is not None else None,
                 state=getattr(self.win, "state", None), import_ms=self.import_ms,
                 open_ms=_clean(getattr(self.win, "open_ms", None)))
        self.heartbeat.stage(PREVIEW)
        self.wait(self.preview_done, self.preview_ready, 240.0, "waiting for the preview")

    def choose_menu_item(self, step, stage):
        """Choose Filter ▸ Kinetikern2… as a click does: validate the menu,
        then send the item's action to its target."""
        filter_item = Glyphs.menu[FILTER_MENU]
        found = find_menu_item(self.plugin.name)
        if found is None:
            raise RuntimeError("no %r item anywhere in the main menu" % self.plugin.name)
        menu, index, top_title = found
        menu.update()  # enables or disables the items, as opening the menu does
        item = menu.itemAtIndex_(index)
        info = dict(top_menu=_text(top_title), index=index, of=menu.numberOfItems(), enabled=bool(item.isEnabled()))
        if step == "menu":
            info["filter_menu"] = [_text(menu.itemAtIndex_(i).title()) for i in range(menu.numberOfItems())]
        if top_title != filter_item.title():
            self.log(step, **info)
            raise RuntimeError("the item is in the %r menu, not in %r" % (top_title, filter_item.title()))
        if not item.isEnabled():
            self.log(step, **info)
            raise RuntimeError("the menu item is disabled")
        # Forget the last answer, so that a choice whose action fails (the
        # plugin prints the traceback) cannot pass for one that worked.
        self.plugin._window = None
        t = time.perf_counter()
        self.call_window(stage, menu.performActionForItemAtIndex_, index)
        info["ms"] = round((time.perf_counter() - t) * 1000.0, 1)
        self.log(step, **info)
        win = getattr(self.plugin, "_window", None)
        if win is None or getattr(win, "w", None) is None:
            raise RuntimeError("choosing the menu item opened no window (see the Macro panel)")
        return win

    def preview_done(self):
        state = self.window_state("while preparing the preview")
        return state == "ready" and self.win.result is not None and self.win.snapshot is not None

    def preview_ready(self):
        win = self.win
        snap, res, ctx = win.snapshot, win.result, win.context
        self.preview_result = res
        seconds = time.time() - self.window_t
        info = self.result_info(res)
        self.preview_kerned = info["kerned_glyphs"]
        snap_info = self.snapshot_info(snap)
        self.log("preview", seconds=round(seconds, 2), snapshot=snap_info, gc_tracked=len(gc.get_objects()),
                 context_prep_ms=round(ctx.prep_ms, 1) if ctx is not None else None,
                 pairs=self.sample_pairs(snap, res), metrics=self.sample_metrics(snap, res),
                 labels=_labels(self.nswindow()), **info)
        self.note("preview ready after %.1f s: %d spacing glyphs (outlines read in %.0f ms), %d kerned in the sample, "
                  "%d entries" % (seconds, snap_info["spacing_glyphs"], snap_info["read_ms"] or 0,
                                  info["kerned_glyphs"], info["entries"]))
        self.later(0.2, self.menu_again)

    def menu_again(self):
        self.heartbeat.stage(AGAIN)
        again = self.choose_menu_item("menu again", AGAIN)
        windows = len(self.open_windows())
        self.log("second choice", same_window=again is self.win, windows=windows)
        if again is not self.win:
            raise RuntimeError("choosing the item again opened a second window")
        if windows != 1:
            raise RuntimeError("kk2_window lists %d open windows after the second choice, expected 1" % windows)
        if self.cancel:
            self.later(0.3, self.start_cancelled_run)
        elif self.whole:
            self.later(0.3, self.start_whole)
        else:
            self.finish()

    def start_cancelled_run(self):
        self.heartbeat.stage(CANCELLED_RUN)
        self.phase_times = {}
        started = time.time()
        self.call_window(CANCELLED_RUN, self.win.start_whole_font)
        self.log("cancel test: whole-font run started", state=getattr(self.win, "state", None))
        self.wait(lambda: self.cancel_point(started), lambda: self.cancel_now(started), 180.0,
                  "waiting for a whole-font run to reach %d %%" % (CANCEL_AT * 100), interval=0.05)

    def cancel_point(self, started):
        """True at about CANCEL_AT of Phase 2 (or when the run is over)."""
        if self.window_state("during the whole-font run") != "solving":
            return True
        phase, fraction, _ = self.track_progress(started)
        if phase is None and fraction is None:
            # no progress the test can read: give the run a few seconds
            return time.time() - started > 3.0
        if phase is None:
            return fraction >= CANCEL_AT
        return phase >= 3 or (phase == 2 and fraction >= CANCEL_AT)

    def cancel_now(self, started):
        win = self.win
        state = win.state
        if state != "solving":
            self.warn("the whole-font run ended (state %r) before it could be cancelled" % state)
            self.log("cancel test: run ended first", state=state, seconds=round(time.time() - started, 2))
            self.wait(lambda: self.window_state("after the whole-font run") == "ready", self.start_whole, 60.0,
                      "waiting for the window to settle")
            return
        phase, fraction, text = self.window_progress()
        at = dict(phase=phase, fraction=None if fraction is None else round(fraction, 3), text=text,
                  seconds=round(time.time() - started, 2), phases=dict(self.phase_times))
        t = time.perf_counter()
        self.call_window(CANCELLED_RUN, win.cancel_job)
        call_ms = (time.perf_counter() - t) * 1000.0
        self.wait(lambda: self.window_state("after Cancel") != "solving",
                  lambda: self.after_cancel(t, call_ms, at), 30.0, "waiting for Cancel to stop the run",
                  interval=0.01)

    def after_cancel(self, t, call_ms, at):
        stop_ms = (time.perf_counter() - t) * 1000.0
        win = self.win
        self.log("cancel test: cancelled", at=at, cancel_call_ms=round(call_ms, 1), stopped_ms=round(stop_ms, 1),
                 state=win.state, result_kept=win.result is self.preview_result, labels=_labels(self.nswindow()))
        where = at["text"] or ("%.0f %%" % (at["fraction"] * 100) if at["fraction"] is not None
                               else "%.1f s" % at["seconds"])
        self.note("cancelled the whole-font run at %s; the window stopped it in %.0f ms" % (where, stop_ms))
        # the restart: what was cancelled must run again from the start
        self.wait(lambda: self.window_state("after Cancel") == "ready", self.start_whole, 60.0,
                  "waiting for the window to settle after Cancel")

    def start_whole(self):
        self.heartbeat.stage(WHOLE_RUN)
        self.phase_times = {}
        self.before_run = self.win.result
        self.whole_t = time.time()
        self.call_window(WHOLE_RUN, self.win.start_whole_font)
        self.log("whole-font run started", state=getattr(self.win, "state", None))
        self.wait(self.whole_finished, self.whole_done, 420.0, "waiting for the whole-font run")

    def whole_finished(self):
        state = self.window_state("during the whole-font run")
        self.track_progress(self.whole_t)
        if state in BUSY_STATES:
            return False
        if self.win.result is self.before_run:
            if time.time() - self.whole_t > 2.0:
                raise RuntimeError("the whole-font run ended (state %r) without a new result; window shows: %s"
                                   % (state, self.window_text()))
            return False
        return True

    def whole_done(self):
        win = self.win
        seconds = time.time() - self.whole_t
        res = win.result
        self.before_run = None
        self.whole_result = res
        info = self.result_info(res)
        st = res.stats
        self.log("whole-font run", seconds=round(seconds, 2), phases=dict(self.phase_times),
                 labels=_labels(self.nswindow()), **info)
        self.note("whole-font run %.1f s on %d threads: %d kerned glyphs, %d pairs in scope, %d class pairs, "
                  "%d entries (%d before the budget)"
                  % (seconds, st.get("threads", 0), info["kerned_glyphs"], st.get("pairs_in_scope", 0),
                     st.get("class_pairs", 0), info["entries"], st.get("entries_before_budget", 0)))
        if info["entries"] == 0:
            self.warn("the whole-font run produced no kerning entries")
        if info["kerned_glyphs"] <= self.preview_kerned:
            self.warn("the whole-font result kerns no more glyphs (%d) than the preview of the sample text (%d)"
                      % (info["kerned_glyphs"], self.preview_kerned))
        if self.whole:
            self.later(0.3, self.apply_step)
        else:
            self.finish()

    def apply_step(self):
        self.heartbeat.stage(APPLY)
        import kk2_apply
        win = self.win
        snap = win.snapshot
        res = win.result
        if res is not self.whole_result:
            self.warn("the window's result changed after the whole-font run; the test plans with the current one")
        mid = snap.master_id
        t = time.time()
        self.before = self.font_state(snap.names, mid)
        capture_s = time.time() - t
        # The plan the window is about to carry out, for the read-back: the
        # same snapshot, the same result, the font as it is now.
        t = time.time()
        self.plan = kk2_apply.plan(snap, res, True)
        plan_s = time.time() - t
        self.log("before apply", capture_s=round(capture_s, 3), plan_s=round(plan_s, 3), gc_tracked=len(gc.get_objects()),
                 kerning_entries=count_entries(self.before["kerning"]), plan=getattr(self.plan, "counts", None))
        previous = win.last_apply
        self.apply_t = time.time()
        if not self.call_window(APPLY, win.apply_whole_font_result, confirm=False):
            raise RuntimeError("the window did not start applying the whole-font result")
        self.apply_call_s = time.time() - self.apply_t
        self.wait(lambda: win.last_apply is not previous and self.window_state("during Apply") != "applying",
                  self.after_apply, 300.0, "applying the whole-font result")

    def after_apply(self):
        win, font, plan = self.win, self.font, self.plan
        snap = win.snapshot
        mid = snap.master_id
        apply_s = time.time() - self.apply_t
        if win.revert_point is None:
            self.error("Apply kept no revert point")

        metric_bad, metric_sides = [], 0
        for name, sides in plan.metrics.items():
            glyph = font.glyphs[name]
            layer = glyph.layers[mid] if glyph is not None else None
            if layer is None:
                metric_bad.append([name, "missing"])
                continue
            for side, want, have in (("LSB", sides[0], layer.LSB), ("RSB", sides[1], layer.RSB)):
                if want is None:
                    continue
                metric_sides += 1
                if have is None or abs(have - want) > 0.5:
                    metric_bad.append([name, side, want, have])

        kerning = list(plan.kerning)
        stride = max(1, len(kerning) // KERNING_READBACK)
        sample = kerning[::stride][:KERNING_READBACK]
        kern_bad = []
        for left, right, value in sample:
            have = font.kerningForFontMasterID_leftKey_rightKey_direction_(mid, left, right, LTR)
            if have is None or abs(have) > NOT_FOUND:
                kern_bad.append([left, right, value, None])
            elif abs(have - value) > 0.5:
                kern_bad.append([left, right, value, have])

        group_bad, group_sides = [], 0
        for name, (left, right) in plan.groups_to_set.items():
            glyph = font.glyphs[name]
            for side, want, have in (("left", left, glyph.leftKerningGroup if glyph else None),
                                     ("right", right, glyph.rightKerningGroup if glyph else None)):
                if want is None:
                    continue
                group_sides += 1
                if _text(have) != want:
                    group_bad.append([name, side, want, _text(have)])

        spacing_checked, spacing_bad = self.spacing_differences(snap, win.result, mid)

        self.log("applied", seconds=round(apply_s, 3), call_s=round(self.apply_call_s, 3), summary=win.last_apply,
                 kerning_entries_after=count_entries(kerning_table(font, mid)),
                 metrics_checked=metric_sides, metrics_mismatches=len(metric_bad), metric_examples=metric_bad[:20],
                 kerning_checked=len(sample), kerning_mismatches=len(kern_bad), kerning_examples=kern_bad[:20],
                 groups_checked=group_sides, group_mismatches=len(group_bad), group_examples=group_bad[:20],
                 spacing_checked=spacing_checked, spacing_mismatches=len(spacing_bad),
                 spacing_examples=spacing_bad[:20], labels=_labels(self.nswindow()))
        self.note("Apply %.2f s: %d kerning entries, %d group sides, %d sidebearings; read back %d entries and all "
                  "groups and sidebearings: %d mismatches"
                  % (apply_s, len(kerning), group_sides, metric_sides, len(sample),
                     len(kern_bad) + len(metric_bad) + len(group_bad)))
        if metric_bad:
            self.error("%d sidebearings differ from the plan after Apply, e.g. %s" % (len(metric_bad), metric_bad[:5]))
        if kern_bad:
            self.error("%d of %d kerning entries read back differ from the plan, e.g. %s"
                       % (len(kern_bad), len(sample), kern_bad[:5]))
        if group_bad:
            self.error("%d kerning groups differ from the plan after Apply, e.g. %s" % (len(group_bad), group_bad[:5]))
        if spacing_bad:
            self.error("after Apply %d of %d glyphs are spaced more than a unit away from the result (metrics keys "
                       "and aligned composites included), e.g. [glyph, LSB, result, RSB, result] %s"
                       % (len(spacing_bad), spacing_checked, spacing_bad[:5]))
        self.later(0.3, self.revert_step)

    def revert_step(self):
        self.heartbeat.stage(REVERT)
        self.revert_t = time.time()
        if not self.call_window(REVERT, self.win.revert_last_apply):
            raise RuntimeError("the window did not start Revert Last Apply")
        self.wait(lambda: self.window_state("during Revert") != "applying", self.after_revert, 300.0,
                  "reverting the apply")

    def after_revert(self):
        snap = self.win.snapshot
        seconds = time.time() - self.revert_t
        after = self.font_state(snap.names, snap.master_id)
        kern_diff = diff_kerning(self.before["kerning"], after["kerning"])
        glyph_diff = diff_glyphs(self.before["glyphs"], after["glyphs"])
        entries = count_entries(after["kerning"])
        self.log("reverted", seconds=round(seconds, 3), counts=getattr(self.win, "last_revert", None),
                 kerning_entries=entries,
                 kerning_differences=len(kern_diff), kerning_examples=kern_diff[:20],
                 glyphs_checked=len(after["glyphs"]), glyph_differences=len(glyph_diff),
                 glyph_examples=glyph_diff[:20], labels=_labels(self.nswindow()))
        if kern_diff or glyph_diff:
            self.note("Revert %.2f s: %d kerning entries and %d glyph values differ from before Apply"
                      % (seconds, len(kern_diff), len(glyph_diff)))
        else:
            self.note("Revert %.2f s: kerning (%d entries), groups and sidebearings of %d glyphs are as before Apply"
                      % (seconds, entries, len(after["glyphs"])))
        if kern_diff:
            self.error("after Revert %d kerning entries differ from before Apply, e.g. %s"
                       % (len(kern_diff), kern_diff[:5]))
        if glyph_diff:
            self.error("after Revert %d glyph values differ from before Apply, e.g. %s"
                       % (len(glyph_diff), glyph_diff[:5]))
        self.before = None
        self.finish()

    # --- reading ------------------------------------------------------------

    def font_state(self, names, master_id):
        """The master's kerning plus groups and (ink) sidebearings of the glyphs."""
        glyphs = {}
        for name in names:
            glyph = self.font.glyphs[name]
            layer = glyph.layers[master_id] if glyph is not None else None
            if layer is None:
                continue
            glyphs[name] = (_text(glyph.leftKerningGroup), _text(glyph.rightKerningGroup)) + glyph_metrics(layer)
        return {"kerning": kerning_table(self.font, master_id), "glyphs": glyphs}

    def spacing_differences(self, snap, res, master_id):
        """(glyphs checked, [[name, LSB, result LSB, RSB, result RSB]]) for
        the glyphs whose ink sidebearings in the font are more than a unit
        (whole-unit moves plus rounding) away from the result: what the left
        pane shows after Apply against what the right pane showed."""
        checked, bad = 0, []
        for i, name in enumerate(snap.names):
            m = res.metrics[i]
            if not m.valid:
                continue
            glyph = self.font.glyphs[name]
            layer = glyph.layers[master_id] if glyph is not None else None
            if layer is None:
                continue
            lsb, rsb, _width = ink_metrics(layer)
            if lsb is None:
                continue
            checked += 1
            if max(abs(lsb - m.lsb), abs(rsb - m.rsb)) > 1.01:
                bad.append([name, round(lsb, 1), round(m.lsb, 1), round(rsb, 1), round(m.rsb, 1)])
        return checked, bad

    def snapshot_info(self, snap):
        rules = {"follow": 0, "fixed": 0}
        kern = rtl = based = 0
        for spec in snap.specs:
            kern += bool(spec.flags & kb.GLYPH_KERN)
            rtl += bool(spec.flags & kb.GLYPH_RTL)
            based += spec.base != kb.NONE
            for rule in (spec.lsb_rule, spec.rsb_rule):
                if rule in (kb.RULE_FOLLOW_SAME, kb.RULE_FOLLOW_OPPOSITE):
                    rules["follow"] += 1
                elif rule == kb.RULE_FIXED:
                    rules["fixed"] += 1
        return {"master": _text(snap.master_name), "upm": snap.upm, "spacing_glyphs": len(snap.names),
                "kern_flag": kern, "rtl": rtl, "on_base": based, "fixed_advance": len(snap.fixed),
                "left_groups": len(snap.left_group_names), "right_groups": len(snap.right_group_names),
                "metric_rules": rules, "read_ms": snap.read_ms, "skipped": snap.glyph_count_skipped}

    def result_info(self, res):
        mask = bytes(res.kern_mask)
        return {"entries": res.entry_count, "classes": res.classes, "right_classes": res.right_class_count,
                "left_classes": res.left_class_count, "kerned_glyphs": len(mask) - mask.count(0),
                "stats": dict(res.stats)}

    def sample_pairs(self, snap, res):
        out = {}
        for pair in CLASSIC:
            a, b = snap.char_map.get(pair[0]), snap.char_map.get(pair[1])
            i, j = snap.index.get(a), snap.index.get(b)
            if i is None or j is None:
                continue
            v = res.value(i, j)
            if v == v:
                out[pair] = round(v, 1)
        return out

    def sample_metrics(self, snap, res):
        out = {}
        for ch in SAMPLE_METRICS:
            i = snap.index.get(snap.char_map.get(ch))
            if i is None or i >= res.glyph_count:
                continue
            m = res.metrics[i]
            if m.valid:
                out[ch] = [round(m.lsb, 1), round(m.rsb, 1), round(m.advance, 1)]
        return out

    def capture_png(self, filename):
        """Save the window as the screen shows it (a process may capture its
        own windows without the Screen Recording permission). A window that is
        not on screen (a floating window hides while Glyphs is in the
        background) is drawn offscreen instead, which leaves pop-up buttons and
        push buttons blank. Returns which of the two it saved."""
        window = self.nswindow()
        rep, source = None, "screen"
        if window.isVisible():
            import Quartz
            window.displayIfNeeded()
            image = Quartz.CGWindowListCreateImage(Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
                                                   window.windowNumber(), Quartz.kCGWindowImageBoundsIgnoreFraming)
            if image is not None and Quartz.CGImageGetWidth(image) > 1:
                rep = NSBitmapImageRep.alloc().initWithCGImage_(image)
        if rep is None:
            view, source = window.contentView(), "offscreen"
            rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
            view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
        data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
        data.writeToFile_atomically_(os.path.join(self.out, filename), True)
        return source

    # --- the end ------------------------------------------------------------

    def fail(self):
        self.error(traceback.format_exc())
        self.finish()

    def finish(self):
        if self.finished:
            return
        self.finished = True
        self.heartbeat.stage(CLOSE)
        if self.win is not None:
            self.report["window_ticks_ms"] = dict(
                (k, round(v, 1)) for k, v in sorted(getattr(self.win, "tick_ms", {}).items(), key=lambda kv: -kv[1]))
        try:
            if self.nswindow() is not None:
                self.log("window image", file="window.png", source=self.capture_png("window.png"))
        except Exception:
            self.error(traceback.format_exc())
        try:
            self.close_window()
        except Exception:
            self.error(traceback.format_exc())
        try:
            if self.font is not None:
                self.font.close(ignoreChanges=True)
                self.log("font closed")
        except Exception:
            self.error(traceback.format_exc())
        self.font = None
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.heartbeat.stop()
        self.gc_watch.stop()
        self.report["gc"] = self.gc_watch.gens
        if self.activity is not None:
            NSProcessInfo.processInfo().endActivity_(self.activity)
            self.activity = None
        self.write_report()
        if self.quit:
            AppHelper.callLater(1.0, NSApp().terminate_, None)

    def close_window(self):
        """Close the window as its close button does; it must let go of its
        engine objects and leave kk2_window's list."""
        win = self.win
        # the test's own references must not keep engine memory alive
        self.preview_result = self.whole_result = self.before_run = None
        self.plan = None
        if win is None or self.nswindow() is None:
            return
        self.call_window(CLOSE, win.w.close)
        windows = len(self.open_windows())
        result_freed = _freed(getattr(win, "result", None))
        context_freed = _freed(getattr(win, "context", None))
        self.log("closed", windows=windows, result_freed=result_freed, context_freed=context_freed)
        if windows:
            self.error("kk2_window still lists %d open windows after the window closed" % windows)
        if not (result_freed and context_freed):
            self.error("the closed window still holds its engine result or context")

    def write_report(self):
        try:
            worst, stage = self.heartbeat.worst(JUDGED)
            self.report["stalls"] = self.heartbeat.stages
            self.report["max_stall_ms"] = worst
            self.report["max_stall_stage"] = stage
            if stage is not None:
                self.note("longest main-thread stall while the window worked: %.0f ms (%s); limit %.0f ms"
                          % (worst, stage, self.max_stall))
            for name in (OPEN, APPLY, REVERT):
                s = self.heartbeat.stages.get(name)
                if s is not None:
                    self.note("main thread held by %s: up to %.0f ms" % (name, s["max_ms"]))
            if worst > self.max_stall:
                self.error("the main thread was busy for %.0f ms at once during the %s (limit %.0f ms)"
                           % (worst, stage, self.max_stall))
            self.report["seconds"] = self.now()
            self.report["ok"] = not self.report["errors"]
            os.makedirs(self.out, exist_ok=True)
            path = os.path.join(self.out, "selftest.json")
            # written under another name first: build.sh polls for selftest.json
            with open(path + ".part", "w") as f:
                json.dump(_clean(self.report), f, indent=2, default=str)
            os.replace(path + ".part", path)
            print("Kinetikern2 self-test %s: %s" % ("passed" if self.report["ok"] else "FAILED", path))
        except Exception:
            print(traceback.format_exc())


def run(resources, plugin):
    SelfTest(resources, plugin).start()
