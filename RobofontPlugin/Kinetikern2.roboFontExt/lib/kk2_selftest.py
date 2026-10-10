# encoding: utf-8
"""
kk2_selftest — verify an installation inside RoboFont without clicking.

`build.sh --verify` starts a temporary second RoboFont instance with the
test's parameters in the argument domain, so nothing is written to the user's
preferences and the user's own RoboFont is never touched:

    open -n -a RoboFont --args -ApplePersistenceIgnoreState YES \\
        -com.mirkovelimirovic.Kinetikern2.selfTestFont   /path/to/Font.ttf \\
        -com.mirkovelimirovic.Kinetikern2.selfTestOut    /path/to/folder \\
        -com.mirkovelimirovic.Kinetikern2.selfTestQuit   YES \\
        -com.mirkovelimirovic.Kinetikern2.selfTestWhole  YES \\
        -com.mirkovelimirovic.Kinetikern2.selfTestCancel YES

A few seconds after launch kk2_startup calls run(). The test opens the font
(a .ufo is copied to a temporary folder first, a .ttf/.otf is imported),
chooses Extensions ▸ Kinetikern2… the way a click does, waits for the live
preview and chooses the item again (the open window must come to the front,
not a second one).

    selfTestCancel   start a whole-font run, cancel it at about 30 % and check
                     that the window settles, then run the whole font again
    selfTestWhole    run the whole font, apply the result without the
                     confirmation dialog, read the font back, Revert Last Apply
                     and check that the master's kerning and the spacing
                     glyphs' groups and sidebearings are exactly as before
    selfTestMaxStall longest tolerated main-thread stall in ms (default 500)
    selfTestConnected the connected-script stage (the font must be a connected
                     script): Keep joins keeps every join (the join checker),
                     kept sides and the font's kerning between two kept sides,
                     a period's distance, Apply and then the font read back
                     with every join between the sample's letters still
                     touching (ink contact), Revert exact, Space joined letters
                     counted, off again
    selfTestGroups   spacing groups — freeze the capitals, space the figures
                     looser with half the kerning force, By Category, open the
                     Spacing Groups and Pairs windows, run the whole font,
                     apply, check that the frozen glyphs, their groups and the
                     kerning between them did not change, measure the pairs,
                     revert and check that the font is as before
    selfTestProfile  profile the main thread from Apply to the end of Revert
                     (profile.txt next to the report)

After the Revert the designer-harness stage runs whenever the engine has the
harness (its window, the conventions, Apply and Revert); the connected-script
and groups stages follow when asked for, in that order. Then it saves
window.png (and the images of the stages that ran) and selftest.json into
selfTestOut, closes the window and the font without saving and, with
selfTestQuit, quits the test instance.

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
over RoboFont's shared interpreter is a stall of its own, outside the window).
A thread watches the main thread while the heartbeat is late: long_gaps keeps,
for every gap over 250 ms, the Python stacks the main thread was in.

    selftest.json  {ok, steps: [{step, t, ...}], max_stall_ms, stalls, states,
                    window_ticks_ms, gc, long_gaps, summary, errors, warnings}
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

import kk2_apply  # noqa: F401  (imported while the extension's folder is on sys.path)
import kk2_args
import kk2_bridge as kb
import kk2_groups  # noqa: F401
import kk2_host as host
import kk2_snapshot as ks
import kk2_window  # noqa: F401

PREFIX = "com.mirkovelimirovic.Kinetikern2."
FONT_KEY = PREFIX + "selfTestFont"
OUT_KEY = PREFIX + "selfTestOut"
QUIT_KEY = PREFIX + "selfTestQuit"
WHOLE_KEY = PREFIX + "selfTestWhole"
CANCEL_KEY = PREFIX + "selfTestCancel"
MAX_STALL_KEY = PREFIX + "selfTestMaxStall"
GROUPS_KEY = PREFIX + "selfTestGroups"
CONNECTED_KEY = PREFIX + "selfTestConnected"
PROFILE_KEY = PREFIX + "selfTestProfile"

DEFAULT_OUT = os.path.expanduser("~/Desktop/Kinetikern2-selftest")
DEFAULT_MAX_STALL_MS = 500.0
DEADLINE_S = 540.0      # selftest.json is written before build.sh stops waiting (10 min after launch)
SETTLE_S = 1.5          # lets RoboFont draw the new font's window before anything is measured
CANCEL_AT = 0.3
KERNING_READBACK = 200
SHAPE_TOLERANCE = 0.01  # units: a composite's outline moved as a whole
MODAL_GRACE_S = 2.0
MENU_TITLE = "Kinetikern2…"

CLASSIC = ["AV", "AT", "LT", "To", "Te", "Ta", "Yo", "VA", "P.", "F.", "r.", "y.", "HH", "nn", "oo", "HO"]
SAMPLE_METRICS = "HOnoAVTL."

# Stages of the test. In the judged ones the window works by itself.
OPEN, PREVIEW, AGAIN = "open window", "preview", "menu again"
CANCELLED_RUN, WHOLE_RUN = "cancelled run", "whole-font run"
APPLY, REVERT, CLOSE = "apply", "revert", "close"
GROUPS = "spacing groups"
GROUP_WINDOWS = "opening the groups windows"  # a user action, timed but not judged
HARNESS = "designer harness"
CONNECTED = "connected script"
SLIDERS = "sliders"
JUDGED = (PREVIEW, SLIDERS, AGAIN, CANCELLED_RUN, WHOLE_RUN, APPLY, REVERT, GROUPS, HARNESS, CONNECTED)
BUSY_STATES = ("reading", "preparing", "previewing", "solving", "applying")

PHASE_TEXT = re.compile(r"Phase\s*(\d+)\s*/\s*(\d+).*?\[\s*(\d+(?:\.\d+)?)\s*%\s*\]")
READING_TEXT = re.compile(r"Reading outlines.*?\[\s*(\d+(?:\.\d+)?)\s*%\s*\]")


def _class(name, factory):
    """One Objective-C class per process: RoboFont runs every extension in one runtime."""
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
    """(menu, index, top-level menu title) of the item called title anywhere
    in the main menu (RoboFont 4.4 puts this extension's item directly in
    the Extensions menu; other versions may use a submenu, so submenus are
    searched too)."""
    main = NSApp().mainMenu()
    for t in range(main.numberOfItems()):
        top = main.itemAtIndex_(t)
        stack = [top.submenu()] if top.submenu() is not None else []
        while stack:
            sub = stack.pop()
            for i in range(sub.numberOfItems()):
                item = sub.itemAtIndex_(i)
                if item.title() == title:
                    return sub, i, top.title()
                if item.submenu() is not None:
                    stack.append(item.submenu())
    return None


def kerning_table(font, master_id=None):
    """The font's kerning as a plain {left key: {right key: value}}."""
    out = {}
    for (left, right), value in host.naked(font).kerning.items():
        out.setdefault(str(left), {})[str(right)] = float(value)
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


def _layer(font):
    return host.naked(font).layers.defaultLayer


def ink_metrics(font, name):
    """(LSB, RSB, width) of a glyph measured on the ink of its decomposed
    outline, the frame the engine and the proof panes use (sidebearings None
    for a glyph without ink); None for a glyph the font does not have."""
    layer = _layer(font)
    if name not in layer:
        return None
    return ks.ink_metrics(layer[name], layer)


def glyph_groups(font):
    """{glyph: (left group, right group)} of the font's kerning groups."""
    left, right, _doubles = ks.kerning_group_maps(host.naked(font).groups)
    return lambda name: (left.get(name), right.get(name))


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


def outline_state(glyph):
    """A glyph's geometry exactly: every point, anchor and component
    transform to the last bit (the .glif RoboFont saves writes them so), and
    the advance."""
    return (repr(glyph.width),  # 600 and 600.0 are written differently
            tuple(tuple((p.x, p.y, p.segmentType) for p in contour) for contour in glyph),
            tuple((a.name, a.x, a.y) for a in glyph.anchors),
            tuple((c.baseGlyph, tuple(c.transformation)) for c in glyph.components))


def diff_outlines(before, after):
    """[name, "outline"] for every glyph whose geometry is not exactly as it
    was (a Revert puts every coordinate back to the last bit)."""
    a, b = before.get("outlines", {}), after.get("outlines", {})
    return [[name, "outline"] for name in sorted(set(a) | set(b)) if a.get(name) != b.get(name)]


def composite_outlines(font):
    """{composite: the points of its decomposed outline} for every glyph with
    components in the font's default layer."""
    layer = _layer(font)
    out = {}
    for name in layer.keys():
        glyph = layer[name]
        if not glyph.components:
            continue
        try:
            out[name] = [p for contour in ks.draw_contours(glyph, layer) for p in contour]
        except RecursionError:
            continue
    return out


def shape_changes(before, after):
    """[name, dx, off by] for the composites whose outline did not move as a
    whole: every point by the same dx along x and none along y. Apply may move
    a composite but must keep it as drawn (an accent stays on its letter)."""
    bad = []
    for name, a in sorted(before.items()):
        b = after.get(name)
        if b is None or len(a) != len(b):
            bad.append([name, "points", len(a), len(b) if b is not None else None])
            continue
        if not a:
            continue
        dx = b[0][0] - a[0][0]
        off = max(max(abs(q[0] - p[0] - dx), abs(q[1] - p[1])) for p, q in zip(a, b))
        if off > SHAPE_TOLERANCE:
            bad.append([name, round(dx, 2), round(off, 2)])
    return bad


def diff_components(before, after):
    """[composite, "components", before, after] for every glyph whose
    component transforms differ (Apply holds composites rigid by moving their
    components; a Revert must put every one back)."""
    a, b = before.get("components", {}), after.get("components", {})
    out = []
    for name in sorted(set(a) | set(b)):
        ta, tb = a.get(name), b.get(name)
        if ta is None or tb is None or len(ta) != len(tb) or any(
                abs(x - y) > 1e-6 for p, q in zip(ta, tb) for x, y in zip(p, q)):
            out.append([name, "components", ta, tb])
    return out


class Heartbeat(object):
    """A 60 Hz timer in the common run-loop modes (it fires during menu
    tracking, slider drags and modal panels). The gap between two ticks, less
    the time the test spent in its own callbacks, is how long something else
    (the window's timers, RoboFont) kept the main thread busy. Gaps and timed
    calls are kept per stage."""

    INTERVAL = 1.0 / 60.0
    STACKS_OVER_MS = 250.0  # a gap this long keeps what StallWatch saw the main thread do

    def __init__(self, on_tick, describe=None):
        self.on_tick = on_tick
        self.describe = describe  # () -> what the window was doing (noted with each stage's longest gap)
        self.stage_name = "start"
        self.stages = {}
        self.t0 = time.time()
        self.last = None
        self.own = 0.0
        self.timer = None
        self.main_thread = None
        self.samples = []  # StallWatch: (ms into the gap, stack) for the gap now running
        self.long_gaps = []  # [{stage, ms, at, stacks}] of the gaps over STACKS_OVER_MS
        self.ticker = KK2SelfTestTicker.alloc().init()
        self.ticker.setCallback(self._tick)

    def note_sample(self, since, ms, stack):
        """From StallWatch's thread: the main thread's stack `ms` into the gap
        that began at `since` (dropped if that gap has ended meanwhile)."""
        if since == self.last:
            self.samples.append((round(ms), stack))

    def _long_gap(self, stage, ms, call):
        """Keeps a few of the stacks StallWatch saw during a long gap, spread
        over it."""
        samples, self.samples = self.samples, []
        if ms <= self.STACKS_OVER_MS or len(self.long_gaps) >= 40:
            return
        keep = samples if len(samples) <= 4 else [samples[0], samples[len(samples) // 3],
                                                   samples[2 * len(samples) // 3], samples[-1]]
        self.long_gaps.append({"stage": stage, "ms": round(ms, 1), "at": round(time.time() - self.t0, 3),
                               "call": call, "stacks": keep})

    def start(self):
        import threading
        self.main_thread = threading.get_ident()
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
        self._long_gap(stage, seconds * 1000.0, True)

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
        self._long_gap(self.stage_name, gap, False)
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


def _stack(frame, limit=12):
    """A thread's Python stack, innermost first: "file:line function"."""
    out = []
    while frame is not None and len(out) < limit:
        code = frame.f_code
        out.append("%s:%d %s" % (os.path.basename(code.co_filename), frame.f_lineno, code.co_name))
        frame = frame.f_back
    return out


class StallWatch(object):
    """A thread that looks at the main thread while the heartbeat is late:
    every EVERY_S once a gap has lasted AFTER_S, the Python stack the main
    thread is in (it runs Python 5 ms at a time at most, so this thread gets
    its turn). A long gap's stacks say what held the main thread: the
    window's code, RoboFont's or another extension's Python, or native work
    (drawing, layout) when the stack is only the run loop's."""

    AFTER_S = 0.2
    EVERY_S = 0.05

    def __init__(self, heartbeat):
        self.heartbeat = heartbeat
        self.running = False

    def start(self):
        import threading
        self.running = True
        thread = threading.Thread(target=self._run, name="kk2-stall-watch")
        thread.daemon = True
        thread.start()

    def stop(self):
        self.running = False

    def _run(self):
        import sys
        hb = self.heartbeat
        while self.running:
            time.sleep(self.EVERY_S)
            since = hb.last
            if since is None or hb.main_thread is None:
                continue
            gap = time.perf_counter() - since
            if gap < self.AFTER_S:
                continue
            frame = sys._current_frames().get(hb.main_thread)
            hb.note_sample(since, 1000.0 * gap, _stack(frame))


def _profile_at(rays, y):
    """Ink x of a side at height y, interpolated between its rays (None where it has no ink)."""
    pts = [(ry, rx) for ry, rx, _t in rays if rx == rx]
    for (y0, x0), (y1, x1) in zip(pts, pts[1:]):
        if y0 <= y <= y1 and y1 > y0:
            return x0 + (y - y0) * (x1 - x0) / (y1 - y0)
    return None


def ink_gap(context, snap, m, a, b, kern):
    """The narrowest white between the inks of glyph a and glyph b set after
    it with the result's metrics `m` and `kern` (font units; negative: they
    overlap), at the heights where both have ink; None if they share none."""
    ia, ib = snap.infos.get(snap.names[a]), snap.infos.get(snap.names[b])
    if ia is None or ib is None:
        return None
    right = context.rays(a, 1)
    left = context.rays(b, 0)
    # each outline is moved by its new left sidebearing; b starts at a's advance plus the kerning
    da = m[a].lsb - ia.bounds[0]
    db = m[a].advance + kern + m[b].lsb - ib.bounds[0]
    best = None
    for y, xl, _t in left:
        if xl != xl:
            continue
        xr = _profile_at(right, y)
        if xr is None:
            continue
        g = (db + xl) - (da + xr)
        best = g if best is None else min(best, g)
    return best


# The main window's controls a user clicks: each must be what a click on it
# reaches (a view laid over it, such as a label stretched to the window's
# edges, takes the clicks without a sign).
CLICKABLE = ("tightness", "intensity", "threshold", "thresholdField", "harness", "harnessStrength", "harnessButton",
             "master", "size", "threads", "maxPairs", "connected", "joinMode", "reload", "scope", "replace",
             "groupsButton", "pairsButton", "joinsButton", "revert", "apply", "alongSlant", "cancel")


def covered_controls(win):
    """[(control, the view a click at its centre reaches)] for the controls
    of CLICKABLE that a click does not reach."""
    nswindow = win.w.getNSWindow()
    content = nswindow.contentView()
    out = []
    for name in CLICKABLE:
        control = getattr(win.w, name, None)
        view = getattr(control, "_nsObject", None) if control is not None else None
        if view is None or view.isHiddenOrHasHiddenAncestor():
            continue
        b = view.bounds()
        centre = view.convertPoint_toView_((b.origin.x + b.size.width / 2.0, b.origin.y + b.size.height / 2.0), None)
        sup = content.superview()
        point = sup.convertPoint_fromView_(centre, None) if sup is not None else centre
        hit = content.hitTest_(point)
        if hit is None or not (hit is view or hit.isDescendantOf_(view)):
            out.append((name, str(hit.className()) if hit is not None else None))
    return out


class SelfTest(object):

    def __init__(self, resources):
        self.resources = resources
        self.font_path = kk2_args.text(_argument(FONT_KEY))
        self.out = kk2_args.text(_argument(OUT_KEY)) or DEFAULT_OUT
        self.quit = kk2_args.flag(_argument(QUIT_KEY))
        self.whole = kk2_args.flag(_argument(WHOLE_KEY))
        self.cancel = kk2_args.flag(_argument(CANCEL_KEY))
        self.groups_test = kk2_args.flag(_argument(GROUPS_KEY))
        self.connected_test = kk2_args.flag(_argument(CONNECTED_KEY))
        self.profile = kk2_args.flag(_argument(PROFILE_KEY))
        self.profiler = None
        self.max_stall = kk2_args.number(_argument(MAX_STALL_KEY), DEFAULT_MAX_STALL_MS)
        self.t0 = time.time()
        self.report = {
            "ok": False, "font": self.font_path, "out": self.out,
            "robofont_version": _robofont_version(),
            "options": {"whole": self.whole, "cancel": self.cancel, "quit": self.quit, "groups": self.groups_test,
                        "connected": self.connected_test, "max_stall_limit_ms": self.max_stall},
            "steps": [], "states": [], "summary": [], "errors": [], "warnings": []}
        self.heartbeat = Heartbeat(self.on_tick, self.window_doing)
        self.gc_watch = GCWatch(self.heartbeat, self.window_doing)
        self.stall_watch = StallWatch(self.heartbeat)
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
        self.before_shapes = None
        self.phase_times = {}

    # --- bookkeeping -------------------------------------------------------

    def now(self):
        return round(time.time() - self.t0, 3)

    def log(self, step, **info):
        info["step"] = step
        info["t"] = self.now()
        self.report["steps"].append(_clean(info))
        # as it goes, into progress.log next to the report: a test that hangs
        # still says where (a file: printing to RoboFont's Output window
        # redraws windows and runs the event loop inside the test's own work)
        try:
            with open(os.path.join(self.out, "progress.log"), "a") as f:
                f.write("%.1f s  %s\n" % (info["t"], step))
        except Exception:
            pass

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
            detail = getattr(self.win, "last_error", None)
            raise RuntimeError("the window reports an error %s: %s%s" % (
                what, self.window_text(), ("\n" + detail) if detail else ""))
        return state

    def nswindow(self):
        w = getattr(self.win, "w", None)
        return w.getNSWindow() if w is not None else None

    def window_doing(self):
        """The window's state and its latest timer tick ("kind of work", ms):
        a long gap right after a short tick was spent outside the window
        (RoboFont drawing, the run loop)."""
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
        (a floating window hides while RoboFont is in the background, so
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
                # App Nap would throttle the timers of a RoboFont in the
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
        from mojo.roboFont import OpenFont
        path = os.path.realpath(self.font_path)
        if not os.path.exists(path):
            raise RuntimeError("font not found: %s" % path)
        opened = path
        if path.lower().rstrip("/").endswith(".ufo") or path.lower().rstrip("/").endswith(".ufoz"):
            # never open the original: the test applies kerning to the document
            self.temp_dir = tempfile.mkdtemp(prefix="kk2-selftest-")
            opened = os.path.join(self.temp_dir, os.path.basename(path.rstrip("/")))
            if os.path.isdir(path):
                shutil.copytree(path, opened)
            else:
                shutil.copy2(path, opened)
        t = time.time()
        self.font = OpenFont(opened, showInterface=True)
        if self.font is None:
            raise RuntimeError("RoboFont could not open %s" % opened)
        dfont = host.naked(self.font)
        self.log("opened", path=opened, seconds=round(time.time() - t, 2), glyphs=len(dfont.layers.defaultLayer),
                 upm=dfont.info.unitsPerEm, family=host.family_name(self.font), style=host.style_name(self.font))

    def open_window(self):
        # RoboFont draws a font window's glyph cells the first time it shows
        # them (a fraction of a second for a few hundred glyphs under
        # Rosetta): done here, before anything is measured, so that it does
        # not count as the window's
        for nswindow in NSApp().windows():
            if nswindow.isVisible():
                nswindow.display()
        self.heartbeat.start()
        self.gc_watch.start()
        self.stall_watch.start()
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
        """Choose Extensions ▸ Kinetikern2… as a click does: validate the
        menu, then send the item's action to its target."""
        import kk2_window
        found = find_menu_item(MENU_TITLE)
        if found is None:
            raise RuntimeError("no %r item anywhere in the main menu" % MENU_TITLE)
        menu, index, top_title = found
        menu.update()  # enables or disables the items, as opening the menu does
        item = menu.itemAtIndex_(index)
        info = dict(top_menu=_text(top_title), index=index, of=menu.numberOfItems(), enabled=bool(item.isEnabled()))
        if step == "menu":
            info["menu"] = [_text(menu.itemAtIndex_(i).title()) for i in range(menu.numberOfItems())]
        if not item.isEnabled():
            self.log(step, **info)
            raise RuntimeError("the menu item is disabled")
        # the window of the test font before the choice: a choice whose action
        # fails (RoboFont prints the traceback) cannot pass for one that worked
        before = kk2_window._window_of(self.font)
        t = time.perf_counter()
        self.call_window(stage, menu.performActionForItemAtIndex_, index)
        info["ms"] = round((time.perf_counter() - t) * 1000.0, 1)
        self.log(step, **info)
        win = kk2_window._window_of(self.font)
        if win is None or getattr(win, "w", None) is None:
            raise RuntimeError("choosing the menu item opened no window (see %s)" % host.OUTPUT_NAME)
        if before is not None and win is not before:
            raise RuntimeError("choosing the menu item again replaced the open window")
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
        covered = covered_controls(win)
        self.log("controls a click reaches", covered=covered)
        if covered:
            self.error("%d controls of the window do not get a click on them, e.g. %s" % (len(covered), covered[:6]))
        else:
            self.note("every control of the window gets a click on it (%d checked)" % len(CLICKABLE))
        self.later(0.2, self.sliders_step)

    # --- sliders ------------------------------------------------------------

    def sliders_step(self):
        """The sliders, moved as a user moves them: each change brings a new
        preview, with other spacing or kerning somewhere in it where the
        setting changes it (the Looseness, the intensity, the designer
        harness; the threshold may leave the sample's kerns as they are),
        then every setting back as it was. While the window matches the
        Looseness to kept joins or frozen glyphs, the slider moves the rest of
        the font from the matched Looseness (an engine without
        FEATURE_FIT_OFFSET takes the Looseness from them alone)."""
        win = self.win
        w = win.w
        self.heartbeat.stage(SLIDERS)
        saved = (w.tightness.get(), w.intensity.get(), w.threshold.get(), w.thresholdField.get(), bool(w.harness.get()))
        self.slider_saved = saved
        # each slider moved from where the settings put it (a user's settings
        # may already sit at an end)
        loose = saved[0] - 0.4 if saved[0] - 0.4 >= w.tightness.getNSSlider().minValue() else saved[0] + 0.4
        strong = 100.0 if abs(saved[1] - 100.0) > 1.0 else 160.0
        higher = min(w.threshold.getNSSlider().maxValue(), saved[2] + 6.0)

        def threshold(v):
            w.threshold.set(v)
            w.thresholdField.set("%.1f" % v)

        self.slider_steps = [
            ("the Looseness from %+.2f to %+.2f" % (saved[0], loose), lambda: w.tightness.set(loose),
             win.physicsChanged, w.tightness),
            ("the intensity from %.0f to %.0f %%" % (saved[1], strong), lambda: w.intensity.set(strong),
             win.physicsChanged, w.intensity),
            ("the threshold from %.1f to %.1f" % (saved[2], higher), lambda: threshold(higher), win.thresholdChanged,
             w.threshold),
            ("the designer harness %s" % ("off" if saved[4] else "on"), lambda: w.harness.set(not saved[4]),
             win.harnessChanged, w.harness),
            ("the Looseness back to %+.2f" % saved[0], lambda: w.tightness.set(saved[0]), win.physicsChanged,
             w.tightness),
        ]
        self.slider_index = 0
        self.slider_times = []
        self.slider_fitted = False
        self.slider_next()

    def slider_next(self):
        win = self.win
        if self.slider_index >= len(self.slider_steps):
            self.sliders_restore()
            return
        what, setter, handler, sender = self.slider_steps[self.slider_index]
        old = win.result
        before = (self.sample_metrics(win.snapshot, old), self.sample_pairs(win.snapshot, old),
                  self.result_signature(old))
        self.slider_fitted = getattr(win, "_fitted", None) is not None
        setter()
        self.call_window(SLIDERS, handler, sender)
        t = time.time()
        self.wait(lambda: self._new_preview(old, "waiting for the preview after moving %s" % what),
                  lambda: self.slider_after(what, before, t), 90.0, "after moving %s" % what)

    def slider_after(self, what, before, t):
        win = self.win
        after = (self.sample_metrics(win.snapshot, win.result), self.sample_pairs(win.snapshot, win.result),
                 self.result_signature(win.result))
        seconds = time.time() - t
        self.slider_times.append(seconds)
        # an engine without FEATURE_FIT_OFFSET takes the Looseness of a solve
        # that matches it to kept joins or frozen glyphs from them alone
        matched = (self.slider_fitted and "Looseness" in what and
                   not win.engine.features & kb.FEATURE_FIT_OFFSET)
        self.log("slider", what=what, seconds=round(seconds, 2), metrics_changed=after[0] != before[0],
                 kerning_changed=after[1] != before[1], preview_changed=after[2] != before[2],
                 looseness_matched=matched)
        if after[2] == before[2] and "threshold" not in what and not matched:
            # (a higher threshold may leave every kern of the sample as it is)
            self.error("sliders: moving %s brought a preview with the same spacing and kerning" % what)
        self.slider_index += 1
        self.later(0.1, self.slider_next)

    def result_signature(self, res):
        """Every glyph's sidebearings and every kerning entry of a result, to
        a tenth of a unit: what a setting may change anywhere in a preview."""
        if res is None or not res.ptr:
            return None
        metrics = tuple((round(m.lsb, 1), round(m.rsb, 1)) if m.valid else None for m in res.metrics)
        entries = tuple(sorted((k, l, r, round(v, 1)) for k, l, r, v, _imp in res.iter_entries()))
        return metrics, entries

    def sliders_restore(self):
        win = self.win
        w = win.w
        tightness, intensity, threshold, field, harness = self.slider_saved
        w.tightness.set(tightness)
        w.intensity.set(intensity)
        w.threshold.set(threshold)
        w.thresholdField.set(field)
        w.harness.set(harness)
        old = win.result
        self.call_window(SLIDERS, win.harnessChanged, w.harness)
        self.call_window(SLIDERS, win.thresholdChanged, w.threshold)
        self.call_window(SLIDERS, win.physicsChanged, w.tightness)
        self.wait(lambda: self._new_preview(old, "waiting for the preview with the settings back"),
                  self.sliders_done, 90.0, "after putting the sliders back")

    def sliders_done(self):
        if not self.slider_fitted:
            how = ""
        elif self.win.engine.features & kb.FEATURE_FIT_OFFSET:
            how = "; the Looseness moved the rest of the font from the one matched to the kept joins or frozen glyphs"
        else:
            how = "; the Looseness is matched to the kept joins or frozen glyphs, so its slider's value is not used"
        self.note("sliders: %d moves, each answered by a new preview (%.1f–%.1f s)%s"
                  % (len(self.slider_times), min(self.slider_times), max(self.slider_times), how))
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
        if self.profile:
            import cProfile
            self.profiler = cProfile.Profile()
            self.profiler.enable()  # the main thread from here to the end of the Revert
        import kk2_apply
        win = self.win
        snap = win.snapshot
        res = win.result
        if res is not self.whole_result:
            self.warn("the window's result changed after the whole-font run; the test plans with the current one")
        mid = snap.master_id
        t = time.time()
        self.before = self.font_state(snap.names, mid)
        self.before_shapes = composite_outlines(self.font)
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

        # every side Apply wrote is within half a unit of the plan, on the ink
        # like the plan's targets
        metric_bad, metric_sides = [], 0
        targets = plan.metrics_ink
        for name, sides in targets.items():
            m = ink_metrics(font, name)
            if m is None:
                metric_bad.append([name, "missing"])
                continue
            lsb, rsb, _w = m
            for side, want, have in (("LSB", sides[0], lsb), ("RSB", sides[1], rsb)):
                if want is None:
                    continue
                metric_sides += 1
                if have is None or abs(have - want) > 0.5 + 1e-6:
                    metric_bad.append([name, side, round(want, 2), have])

        kerning = list(plan.kerning)
        stride = max(1, len(kerning) // KERNING_READBACK)
        sample = kerning[::stride][:KERNING_READBACK]
        kern_bad = []
        table = host.naked(font).kerning
        for left, right, value in sample:
            have = table[(left, right)] if (left, right) in table else None
            if have is None:
                kern_bad.append([left, right, value, None])
            elif abs(float(have) - value) > 0.5:
                kern_bad.append([left, right, value, have])

        group_bad, group_sides = [], 0
        groups_of = glyph_groups(font)
        for name, (left, right) in plan.groups_to_set.items():
            have_left, have_right = groups_of(name)
            for side, want, have in (("left", left, have_left), ("right", right, have_right)):
                if want is None:
                    continue
                group_sides += 1
                if _text(have) != want:
                    group_bad.append([name, side, want, _text(have)])

        spacing_checked, spacing_bad = self.spacing_differences(snap, win.result, mid)
        shape_bad = shape_changes(self.before_shapes, composite_outlines(font))

        self.log("applied", seconds=round(apply_s, 3), call_s=round(self.apply_call_s, 3), summary=win.last_apply,
                 kerning_entries_after=count_entries(kerning_table(font, mid)),
                 metrics_checked=metric_sides, metrics_mismatches=len(metric_bad), metric_examples=metric_bad[:20],
                 kerning_checked=len(sample), kerning_mismatches=len(kern_bad), kerning_examples=kern_bad[:20],
                 groups_checked=group_sides, group_mismatches=len(group_bad), group_examples=group_bad[:20],
                 spacing_checked=spacing_checked, spacing_mismatches=len(spacing_bad),
                 spacing_examples=spacing_bad[:20], composites_checked=len(self.before_shapes),
                 composites_reshaped=len(shape_bad), composite_examples=shape_bad[:20],
                 labels=_labels(self.nswindow()))
        self.note("Apply %.2f s: %d kerning entries, %d group sides, %d sidebearings; read back %d entries and all "
                  "groups and sidebearings: %d mismatches; %d of %d composites moved as drawn"
                  % (apply_s, len(kerning), group_sides, metric_sides, len(sample),
                     len(kern_bad) + len(metric_bad) + len(group_bad), len(self.before_shapes) - len(shape_bad),
                     len(self.before_shapes)))
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
        if shape_bad:
            self.error("after Apply %d of %d composites are no longer as drawn (a part moved against the rest), "
                       "e.g. [glyph, dx, off by] %s" % (len(shape_bad), len(self.before_shapes), shape_bad[:5]))
        self.before_shapes = None
        self.later(0.3, self.revert_step)

    def revert_step(self):
        self.heartbeat.stage(REVERT)
        self.revert_t = time.time()
        if not self.call_window(REVERT, self.win.revert_last_apply):
            raise RuntimeError("the window did not start Revert Last Apply")
        self.wait(lambda: self.window_state("during Revert") != "applying", self.after_revert, 300.0,
                  "reverting the apply")

    def save_profile(self):
        """With selfTestProfile: what ran on the main thread from Apply to the
        end of Revert (the window, RoboFont, other extensions), into
        profile.txt."""
        if self.profiler is None:
            return
        import io
        import pstats
        self.profiler.disable()
        text = io.StringIO()
        stats = pstats.Stats(self.profiler, stream=text)
        stats.sort_stats("tottime").print_stats(45)
        stats.sort_stats("cumulative").print_stats(60)
        self.profiler = None
        path = os.path.join(self.out, "profile.txt")
        with open(path, "w") as f:
            f.write(text.getvalue())
        self.log("profile", file=path)

    def after_revert(self):
        snap = self.win.snapshot
        seconds = time.time() - self.revert_t
        self.save_profile()
        after = self.font_state(snap.names, snap.master_id)
        kern_diff = diff_kerning(self.before["kerning"], after["kerning"])
        glyph_diff = diff_glyphs(self.before["glyphs"], after["glyphs"]) + diff_components(self.before, after) + \
            diff_outlines(self.before, after)
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
            self.note("Revert %.2f s: kerning (%d entries), groups, sidebearings and every outline coordinate of "
                      "%d glyphs are exactly as before Apply" % (seconds, entries, len(after["glyphs"])))
        if kern_diff:
            self.error("after Revert %d kerning entries differ from before Apply, e.g. %s"
                       % (len(kern_diff), kern_diff[:5]))
        if glyph_diff:
            self.error("after Revert %d glyph values differ from before Apply, e.g. %s"
                       % (len(glyph_diff), glyph_diff[:5]))
        self.before = None
        if self.win.harness_available():
            self.later(0.3, self.harness_step)
        else:
            self.note("designer harness: not in this engine build, not tested")
            self.after_harness()

    def after_harness(self):
        if self.connected_test:
            self.later(0.3, self.connected_step)
        else:
            self.after_connected()

    def after_connected(self):
        if self.groups_test:
            self.later(0.3, self.groups_step)
        else:
            self.finish()

    # --- connected scripts ----------------------------------------------------

    def _new_preview(self, old, what):
        """A preview other than `old` is shown and the window is idle."""
        win = self.win
        return (self.window_state(what) == "ready" and win._result_kind == "preview" and win.result is not None
                and win.result is not old and win.context is not None)

    def connected_step(self):
        """Connected script on, as a user does it: Phase 1 again with the joins
        found in the font's own spacing, then the preview."""
        win = self.win
        self.heartbeat.stage(CONNECTED)
        if not win.engine.features & kb.FEATURE_JOINS:
            self.error("connected script: this engine build has no join mode (kk2_detect_joins)")
            self.after_connected()
            return
        if win.w.connected.get():
            win.w.connected.set(False)
            self.call_window(CONNECTED, win.connectedChanged, None)
        self.wait(lambda: self._new_preview(None, "waiting for the preview without joins") and win.joins is None,
                  self.connected_off_ready, 180.0, "the preview without joins")

    def connected_off_ready(self):
        win = self.win
        self.joins_off_metrics = self._metrics()
        self.joins_off_result = win.result
        win.w.connected.set(True)
        self.call_window(CONNECTED, win.connectedChanged, None)
        self.wait(lambda: self._new_preview(self.joins_off_result, "waiting for the connected-script preview"),
                  self.connected_on_ready, 180.0, "the preview with the joins")

    def connected_on_ready(self):
        """Keep joins (the default): the preview keeps every join, the kept
        sides keep the font's sidebearings and join pairs its kerning; a
        period after a joining letter keeps its distance."""
        win = self.win
        snap = win.snapshot
        res = win.result
        per = 1000.0 / snap.upm
        n, joins = win.join_count, win.joins
        self.log("connected script on", joins=n, note=win.join_note, detect_ms=round(win.join_ms, 1),
                 mode=win._join_mode(), button=win.w.joinsButton.getTitle())
        if joins is None:
            self.error("connected script: no joins found (%s): a connected script is needed for this stage" % win.join_note)
            self.connected_off()
            return
        lower = [i for i, sp in enumerate(snap.specs) if sp.group == kb.GROUP_LOWERCASE]
        # the sides that join: the detector's bands, else (letters that touch,
        # a hand that joins in part) the checker's sides that join in the font
        side_bits = win.engine.join_sides(win.context) if win.engine.features & kb.FEATURE_JOIN_CHECK else []

        def right_joins(i):
            return bool(joins[i][1]) or (i < len(side_bits) and bool(side_bits[i] & kb.JOINSIDE_RIGHT_JOINS))

        joining_lower = [i for i in lower if right_joins(i)]
        self.note("connected script: %s (found in %.0f ms on the main thread); %d of %d lowercase letters join on the right"
                  % (win.join_note, win.join_ms, len(joining_lower), len(lower)))
        sample = sorted(win._sample_indices())
        m = res.metrics
        em = res.engine_metrics

        def kern(a, b):
            v = res.value(a, b)
            return 0.0 if v != v else float(v)

        if not win.engine.features & kb.FEATURE_JOIN_CHECK:
            self.error("connected script: this engine build has no join checker (kk2_join_check)")
        elif not win._keeps_joins():
            self.error("connected script: Keep joins is not the mode in use (mode %r)" % win._join_mode())
        else:
            # 1. the checker on the preview: every join between letters holds
            st, sides = win.join_check if win.join_check is not None else (None, [])
            if st is None:
                self.error("connected script: the preview has no join check")
            else:
                self.log("join check (Keep joins)", stats=st, sides=[(snap.names[g], side, k, round(d * per, 1)) for g, side, k, d in sides[:8]])
                self.note("connected script (Keep joins): %d of %d joins between letters kept, %d broken, %d kept sides; the Looseness matched to them (%+.2f)"
                          % (st["kept"], st["joins"], st["broken"], st["kept_sides"], win._fitted or 0.0))
                if not st["joins"]:
                    self.error("connected script: the checker found no joins between letters")
                if st["broken"]:
                    self.error("connected script: Keep joins breaks %d joins, e.g. %s"
                               % (st["broken"], [(snap.names[g], side) for g, side, _k, _d in sides[:5]]))
            # 2. kept sides keep the font's sidebearings (the engine's frame)
            bits = win.engine.join_sides(win.context)
            moved = []
            for i in sample:
                b = bits[i] if i < len(bits) else 0
                spec = snap.specs[i]
                if not em[i].valid:
                    continue
                if b & kb.JOINSIDE_LEFT_KEPT and abs(em[i].lsb - spec.cur_lsb) > 0.01:
                    moved.append((snap.names[i], "left", round(em[i].lsb - spec.cur_lsb, 2)))
                if b & kb.JOINSIDE_RIGHT_KEPT and abs(em[i].rsb - spec.cur_rsb) > 0.01:
                    moved.append((snap.names[i], "right", round(em[i].rsb - spec.cur_rsb, 2)))
            kept_sample = [i for i in sample if i < len(bits) and bits[i] & (kb.JOINSIDE_LEFT_KEPT | kb.JOINSIDE_RIGHT_KEPT)]
            self.log("kept sides in the sample", glyphs=len(kept_sample), moved=moved[:10])
            if moved:
                self.error("connected script: %d kept sides moved, e.g. %s" % (len(moved), moved[:5]))
            # 3. a pair of two kept sides keeps the font's kerning
            table = win._kerning.table(snap.master_id)
            pairs = [(a, b) for a in sample for b in sample
                     if bits[a] & kb.JOINSIDE_RIGHT_KEPT and bits[b] & kb.JOINSIDE_LEFT_KEPT and m[a].valid and m[b].valid]
            changed = []
            for a, b in pairs:
                want = win._kerning.value(table, snap.names[a], snap.names[b])
                if abs(kern(a, b) - want) > 0.5:
                    changed.append((snap.names[a], snap.names[b], round(kern(a, b), 1), round(want, 1)))
            self.note("connected script: %d sample pairs of two kept sides, %d with other kerning than the font's (should be 0)"
                      % (len(pairs), len(changed)))
            if changed:
                self.error("connected script: %d kept join pairs lost the font's kerning, e.g. %s" % (len(changed), changed[:5]))
            if not pairs:
                self.error("connected script: the sample text has no pair of kept joining sides")
        # a period after a joining letter keeps its distance from the exit
        # stroke: the white between the two inks at every height they share
        # (the engine's own ink profiles: a letter's bounding box can reach
        # over the period, as a d's loop does)
        period = snap.index.get("period")
        if period is not None and period in sample and m[period].valid:
            gaps = []
            for a in sample:
                if not right_joins(a) or not m[a].valid:
                    continue
                g = ink_gap(win.context, snap, m, a, period, kern(a, period))
                if g is not None:
                    gaps.append((snap.names[a], round(g * per, 1)))
            worst = min(gaps, key=lambda x: x[1]) if gaps else None
            self.log("letters before a period", pairs=gaps[:12])
            if worst is not None:
                self.note("connected script: a period after a joining letter keeps its distance (closest %s %+.0f per 1000 em between the inks, %d pairs)"
                          % (worst[0], worst[1], len(gaps)))
                if worst[1] < -1:
                    self.error("connected script: %s and a period overlap by %.0f units per 1000 em" % (worst[0], -worst[1]))
        # the Joins window, opened from its button as a user does it: each
        # view lists its rows, and it closes
        try:
            self.call_window(CONNECTED, win.openJoins, win.w.joinsButton)
            jw = win.joins_window
            if jw is None or not jw.w.getNSWindow().isVisible():
                self.error("connected script: the Joins window did not open")
            else:
                counts = []
                for view in range(3):
                    jw.w.viewPicker.set(view)
                    jw.viewChanged(jw.w.viewPicker)
                    counts.append(len(jw.w.list.get()))
                self.note("connected script: the Joins window opens: %d rows of findings, %d under the preview, "
                          "%d of drawing advice" % tuple(counts))
                jw.close()
                if win.joins_window is not None:
                    self.error("connected script: the Joins window did not tell the window it closed")
        except Exception:
            self.error("connected script: the Joins window failed: %s" % traceback.format_exc().strip().splitlines()[-1])
        self.capture_png("connected.png")
        # the sample's letter pairs that join as drawn (ink contact), to check
        # again on the font after Apply
        kinds = win.join_kinds or ()
        letters = [i for i in sample if i < len(kinds) and kinds[i] != kb.JOINKIND_OTHER]
        self.joined_before = set()
        if win.engine.features & kb.FEATURE_JOIN_CHECK and letters:
            # the joins Keep joins keeps: a letter and a basic a–z letter,
            # either way round (two capitals whose swashes touch are spaced)
            pairs = [(a, b) for a in letters for b in letters
                     if kb.JOINKIND_LOWER in (kinds[a], kinds[b])]
            drawn = win.engine.join_pairs(win.context, pairs)
            self.joined_before = set((snap.names[d["left"]], snap.names[d["right"]]) for d in drawn if d["joins"])
        # Apply the preview with the joins, then Revert
        self.before = self.font_state(snap.names, snap.master_id)
        self.connected_written = []
        for name in sorted(win.plan_names(res) or ()):
            i = snap.index.get(name)
            if i is not None and m[i].valid:
                self.connected_written.append((name, m[i].lsb, m[i].rsb))
        previous = win.last_apply
        if not self.call_window(CONNECTED, win._apply, res, False):
            raise RuntimeError("the window did not apply the connected-script preview")
        self.wait(lambda: win.last_apply is not previous and self.window_state("during the connected Apply") != "applying",
                  self.connected_applied, 120.0, "applying with the joins")

    def connected_applied(self):
        win = self.win
        snap = win.snapshot
        bad = []
        for name, lsb_want, rsb_want in self.connected_written:
            lsb, rsb, _w = ink_metrics(self.font, name) or (None, None, None)
            if lsb is None:
                continue
            if max(abs(lsb - lsb_want), abs(rsb - rsb_want)) > 1.01:
                bad.append([name, round(lsb, 1), round(lsb_want, 1), round(rsb, 1), round(rsb_want, 1)])
        self.log("connected applied", glyphs=len(self.connected_written), differences=bad[:10], summary=win.last_apply)
        if bad:
            self.error("after the connected-script Apply %d glyphs differ from the preview, e.g. %s" % (len(bad), bad[:5]))
        else:
            self.note("connected script Apply: %d glyphs written as the preview showed them" % len(self.connected_written))
        self.wait(lambda: self.window_state("after the connected Apply") == "ready", self.connected_reread, 120.0,
                  "the window after the connected-script Apply")

    def connected_reread(self):
        """Read the applied master again: its joins, by ink contact."""
        win = self.win
        old = win.result
        self.call_window(CONNECTED, win.reloadOutlines, None)
        self.wait(lambda: self._new_preview(old, "waiting for the preview of the applied font") and win.joins is not None,
                  self.connected_reread_ready, 300.0, "the applied font read again")

    def connected_reread_ready(self):
        win = self.win
        snap = win.snapshot
        idx = snap.index
        pairs = [(idx[a], idx[b]) for a, b in sorted(self.joined_before) if a in idx and b in idx]
        if not win.engine.features & kb.FEATURE_JOIN_CHECK or not pairs:
            self.note("connected script: no joins to check again after Apply")
        else:
            after = win.engine.join_pairs(win.context, pairs)
            lost = [(snap.names[d["left"]], snap.names[d["right"]], round(d["gap"] * 1000.0 / snap.upm, 1))
                    for d in after if not d["joins"]]
            self.log("joins after Apply", checked=len(pairs), lost=lost[:10])
            if lost:
                self.error("connected script: after Apply %d of %d joins in the sample no longer touch, e.g. %s"
                           % (len(lost), len(pairs), lost[:5]))
            else:
                self.note("connected script: after Apply all %d joins between the sample's letters still touch (ink contact, read back from the font)"
                          % len(pairs))
        self.wait(lambda: self.window_state("after reading the applied font") == "ready", self.connected_revert, 120.0,
                  "the window after reading the applied font")


    def connected_revert(self):
        if not self.call_window(CONNECTED, self.win.revert_last_apply):
            raise RuntimeError("the window did not revert the connected-script Apply")
        self.wait(lambda: self.window_state("during the connected Revert") not in ("applying",), self.connected_reverted,
                  300.0, "reverting the connected-script Apply")

    def connected_reverted(self):
        win = self.win
        snap = win.snapshot
        after = self.font_state(snap.names, snap.master_id)
        kern_diff = diff_kerning(self.before["kerning"], after["kerning"])
        glyph_diff = diff_glyphs(self.before["glyphs"], after["glyphs"]) + diff_components(self.before, after) + \
            diff_outlines(self.before, after)
        self.log("connected reverted", kerning_differences=len(kern_diff), glyph_differences=len(glyph_diff),
                 examples=(kern_diff + glyph_diff)[:10])
        if kern_diff or glyph_diff:
            self.error("after reverting the connected-script Apply %d kerning entries and %d glyph values differ"
                       % (len(kern_diff), len(glyph_diff)))
        else:
            self.note("connected script Revert: the font is as before")
        self.before = None
        self.connected_space()

    def connected_space(self):
        """Space joined letters: the checker counts the joins it breaks."""
        win = self.win
        if not win.engine.features & kb.FEATURE_JOIN_CHECK:
            self.connected_off()
            return
        old = win.result
        win.w.joinMode.set(1)
        self.call_window(CONNECTED, win.joinModeChanged, None)
        self.wait(lambda: self._new_preview(old, "waiting for the Space joined letters preview"),
                  self.connected_space_ready, 180.0, "the preview spacing the joined letters")

    def connected_space_ready(self):
        win = self.win
        st = win.join_check[0] if win.join_check is not None else None
        if st is None:
            self.error("connected script: no join check for Space joined letters")
        else:
            self.log("join check (Space joined letters)", stats=st, button=win.w.joinsButton.getTitle())
            self.note("connected script (Space joined letters): %d of %d joins kept, %d broken (reported, not an error)"
                      % (st["kept"], st["joins"], st["broken"]))
        old = win.result
        win.w.joinMode.set(0)
        self.call_window(CONNECTED, win.joinModeChanged, None)
        self.wait(lambda: self._new_preview(old, "waiting for the Keep joins preview again"), self.connected_off,
                  180.0, "the preview keeping joins again")


    def connected_off(self):
        win = self.win
        old = win.result
        win.w.connected.set(False)
        self.call_window(CONNECTED, win.connectedChanged, None)
        self.wait(lambda: self._new_preview(old, "waiting for the preview without joins again") and win.joins is None,
                  self.connected_off_again, 180.0, "the preview without joins, again")

    def connected_off_again(self):
        m2 = self._metrics()
        back = sum(1 for x, y in zip(self.joins_off_metrics, m2) if any(abs(p - q) > 1e-9 for p, q in zip(x, y)))
        if back:
            self.error("after turning Connected script off %d glyphs' sidebearings differ from before" % back)
        else:
            self.note("connected script off: every glyph back to the spacing without joins")
        self.after_connected()

    # --- the designer harness -----------------------------------------------

    def _result_harness_strength(self):
        key = self.win._result_key
        return key[-1] if key else None

    def _result_harness_style(self):
        key = self.win._result_key
        return key[-2] if key and len(key) >= 2 else None

    def _preview_with(self, strength):
        win = self.win
        return (self.window_state("waiting for the harness preview") == "ready" and win._result_kind == "preview"
                and self._result_harness_strength() == strength and win.result is not None)

    def harness_step(self):
        """Open the Designer Harness window (its pairs join the preview) and
        wait for a preview without the harness."""
        win = self.win
        self.heartbeat.stage(HARNESS)
        if win.w.harness.get():
            win.w.harness.set(False)
            win.harnessChanged(None)
        t = time.perf_counter()
        hw = self.call_window(HARNESS, win.openHarness)
        self.harness_open_ms = (time.perf_counter() - t) * 1000.0
        if win.harness_window is None:
            raise RuntimeError("the Designer Harness window did not open")
        self.wait(lambda: self._preview_with(0.0) and self._covers(win.harness_window.pairs[:20]),
                  self.harness_off_ready, 120.0, "the preview of the harness pairs, without the harness")

    def _covers(self, pairs):
        res = self.win.result
        if res is None or res.ptr is None or not pairs:
            return False
        for a, b, _d in pairs:
            v = res.value(a, b)
            if v != v:
                return False
        return True

    def _metrics(self):
        return [(m.lsb, m.rsb, m.advance) for m in self.win.result.metrics]

    def harness_off_ready(self):
        win = self.win
        hw = win.harness_window
        self.harness_pairs = list(hw.pairs[:20])
        self.harness_m0 = self._metrics()
        self.harness_v0 = dict(((a, b), float(win.result.value(a, b))) for a, b, _ in self.harness_pairs)
        self.harness_rows = len(hw.pairs)
        # on, as a user does it: the checkbox of the harness window
        hw.w.on.set(True)
        self.call_window(HARNESS, hw.onChanged, None)
        self.wait(lambda: self._preview_with(1.0) and self._covers(self.harness_pairs), self.harness_on_ready,
                  120.0, "the preview with the harness")

    def harness_on_ready(self):
        win = self.win
        snap = win.snapshot
        plan = win._result_harness
        per = 1000.0 / snap.upm
        if plan is None:
            raise RuntimeError("the preview with the harness has no harness plan")
        m1 = self._metrics()
        side_bad = []
        for i, ((l0, r0, a0), (l1, r1, a1)) in enumerate(zip(self.harness_m0, m1)):
            dl, dr = plan.sides[i]
            if abs((l1 - l0) - dl) > 1e-3 or abs((r1 - r0) - dr) > 1e-3 or abs((a1 - a0) - dl - dr) > 1e-3:
                side_bad.append([snap.names[i], round(l1 - l0, 2), round(dl, 2), round(r1 - r0, 2), round(dr, 2)])
        pair_bad = []
        examples = []
        for a, b, d in self.harness_pairs:
            v1 = float(win.result.value(a, b))
            want = plan.pair_value.get((a, b), 0.0)
            got = v1 - self.harness_v0[(a, b)]
            if abs(got - want) > 1e-3:
                pair_bad.append([snap.names[a], snap.names[b], round(got, 2), round(want, 2)])
            gap0 = self.harness_m0[a][1] + self.harness_v0[(a, b)] + self.harness_m0[b][0]
            gap1 = m1[a][1] + v1 + m1[b][0]
            examples.append("%s %s %+.0f" % (snap.names[a], snap.names[b], (gap1 - gap0) * per))
            if abs((gap1 - gap0) - d) > 1e-3:
                pair_bad.append([snap.names[a], snap.names[b], "gap", round(gap1 - gap0, 2), round(d, 2)])
        moved = sum(1 for s in plan.sides if abs(s[0]) >= 0.5 or abs(s[1]) >= 0.5)
        biggest = max((abs(x) for s in plan.sides for x in s), default=0.0) * per
        hw = win.harness_window
        model = win.model_pair(self.harness_pairs[0][0], self.harness_pairs[0][1]) if self.harness_pairs else None
        self.log("harness on", stem=plan.stem_measured, looseness=plan.looseness, sides_moved=moved,
                 pairs=len(plan.pairs), biggest_side_shift_per_1000=round(biggest, 1), rows=self.harness_rows,
                 top=examples[:10], side_differences=side_bad[:10], pair_differences=pair_bad[:10],
                 window_open_ms=round(self.harness_open_ms, 1), model_pair=model is not None)
        self.note("designer harness: %d glyph sides and %d pairs corrected (stem %s, largest side shift %.0f per "
                  "1000 em); the preview moved every side and pair exactly as planned%s; the Designer Harness "
                  "window lists %d pairs, e.g. %s; opened in %.0f ms"
                  % (moved, len(plan.pairs), "%.0f" % plan.stem_measured if plan.stem_measured else "—", biggest,
                     "" if not (side_bad or pair_bad) else " EXCEPT %d sides and %d pairs" % (
                         len(side_bad), len(pair_bad)), self.harness_rows, ", ".join(examples[:4]),
                     self.harness_open_ms))
        if side_bad:
            self.error("with the harness %d glyphs' sidebearings moved other than planned, e.g. %s"
                       % (len(side_bad), side_bad[:5]))
        if pair_bad:
            self.error("with the harness %d pairs moved other than planned, e.g. %s" % (len(pair_bad), pair_bad[:5]))
        if not self.harness_rows:
            self.error("the Designer Harness window lists no pairs")
        if model is None:
            self.error("the Designer Harness window cannot draw its first pair (no model spacing)")
        try:
            hw.preview.display()
            self.log("harness image", source=self.capture_png("harness.png", hw.w.getNSWindow()))
        except Exception:
            self.warn(traceback.format_exc())
        # the Letters filter, as a user picks it
        hw.w.which.set(1)
        self.call_window(HARNESS, hw.whichChanged, None)
        not_letters = [(snap.names[a], snap.names[b]) for a, b, _ in hw.pairs
                       if not (plan.is_letter(a) and plan.is_letter(b))
                       or (plan.key[a].islower() and plan.key[b].isupper())]
        letter_rows = [r["pair"] for r in hw.w.list.get()[:8]]
        self.log("harness letters", rows=len(hw.pairs), first=letter_rows, not_letters=not_letters[:5])
        if not hw.pairs or not_letters:
            self.error("the Letters filter of the Designer Harness window lists %d pairs, %d of them not two "
                       "letters" % (len(hw.pairs), len(not_letters)))
        else:
            self.note("Designer Harness window, Letters: %d pairs, e.g. %s" % (
                len(hw.pairs), ", ".join(r.split("   ")[0] for r in letter_rows[:6])))
        # the screen catches up with the new list before the capture
        self.later(0.8, self.harness_capture_letters)

    def harness_capture_letters(self):
        hw = self.win.harness_window
        try:
            hw.preview.display()
            self.log("harness letters image", source=self.capture_png("harness-letters.png", hw.w.getNSWindow()))
        except Exception:
            self.warn(traceback.format_exc())
        self.later(0.2, self.harness_style_step)

    def _pick_style(self, key):
        hw = self.win.harness_window
        keys = [k for k, _label in hw.styles]
        hw.w.style.set(keys.index(key))
        self.call_window(HARNESS, hw.styleChanged, None)

    def harness_style_step(self):
        """The Display conventions, picked as a user does in the Designer
        Harness window: the preview must move every glyph side by the Display
        plan, punctuation included."""
        hw = self.win.harness_window
        if "display" not in [k for k, _label in hw.styles]:
            self.warn("the harness table has no Display conventions")
            self.later(0.2, self.harness_apply)
            return
        self._pick_style("display")
        # the window's list follows the conventions: the preview covers its pairs
        self.wait(lambda: self._preview_with(1.0) and self._result_harness_style() == "display"
                  and self._covers(self.win.harness_window.pairs[:20]), self.harness_style_ready, 120.0,
                  "the preview with the Display conventions")

    def harness_style_ready(self):
        win = self.win
        snap = win.snapshot
        plan = win._result_harness
        if plan is None or plan.style != "display":
            raise RuntimeError("the preview with the Display conventions has no Display plan")
        per = 1000.0 / snap.upm
        bad = []
        for i, ((l0, r0, _a0), (l1, r1, _a1)) in enumerate(zip(self.harness_m0, self._metrics())):
            dl, dr = plan.sides[i]
            if abs((l1 - l0) - dl) > 1e-3 or abs((r1 - r0) - dr) > 1e-3:
                bad.append([snap.names[i], round(l1 - l0, 2), round(dl, 2), round(r1 - r0, 2), round(dr, 2)])
        marks = []
        for ch in (".", ",", "-", "?"):
            name = snap.char_map.get(ch)
            if name in snap.names:
                i = snap.names.index(name)
                marks.append("%s %+.0f/%+.0f" % (name, plan.sides[i][0] * per, plan.sides[i][1] * per))
        moved = sum(1 for sd in plan.sides if abs(sd[0]) >= 0.5 or abs(sd[1]) >= 0.5)
        self.log("harness display", sides_moved=moved, pairs=len(plan.pairs), differences=bad[:10], marks=marks)
        self.note("designer harness, Display conventions picked in its window: %d glyph sides and %d pairs corrected, "
                  "the preview moved every side as planned%s (%s)"
                  % (moved, len(plan.pairs), "" if not bad else " EXCEPT %d" % len(bad), ", ".join(marks)))
        if bad:
            self.error("with the Display conventions %d glyphs' sidebearings moved other than planned, e.g. %s"
                       % (len(bad), bad[:5]))
        # back to text faces, which the rest of the test applies
        self._pick_style("text")
        self.wait(lambda: self._preview_with(1.0) and self._result_harness_style() == "text"
                  and self._covers(self.win.harness_window.pairs[:20]), self.harness_apply, 120.0,
                  "the preview with the text conventions again")

    def harness_apply(self):
        """Apply the preview with the harness: the glyphs it shifted must reach
        the font as the preview shows them."""
        win = self.win
        snap = win.snapshot
        res = win.result
        plan = win._result_harness
        self.before = self.font_state(snap.names, snap.master_id)
        self.harness_written = []
        for name in sorted(win.plan_names(res) or ()):
            i = snap.index.get(name)
            if i is None or not res.metrics[i].valid:
                continue
            s = plan.sides[i]
            if abs(s[0]) >= 1.0 or abs(s[1]) >= 1.0:
                self.harness_written.append((name, res.metrics[i].lsb, res.metrics[i].rsb, tuple(s)))
        previous = win.last_apply
        if not self.call_window(HARNESS, win._apply, res, False):
            raise RuntimeError("the window did not apply the preview with the harness")
        self.wait(lambda: win.last_apply is not previous and self.window_state("during the harness Apply") != "applying",
                  self.harness_applied, 120.0, "applying with the harness")

    def harness_applied(self):
        win = self.win
        snap = win.snapshot
        per = 1000.0 / snap.upm
        bad = []
        for name, lsb_want, rsb_want, _s in self.harness_written:
            lsb, rsb, _w = ink_metrics(self.font, name) or (None, None, None)
            if lsb is None:
                continue
            if max(abs(lsb - lsb_want), abs(rsb - rsb_want)) > 1.01:
                bad.append([name, round(lsb, 1), round(lsb_want, 1), round(rsb, 1), round(rsb_want, 1)])
        examples = ["%s %+.0f/%+.0f" % (n, s[0] * per, s[1] * per) for n, _l, _r, s in self.harness_written[:6]]
        self.log("harness applied", glyphs=len(self.harness_written), differences=bad[:10], examples=examples,
                 summary=win.last_apply)
        self.note("designer harness Apply: %d glyphs the harness shifted written as the preview showed them%s "
                  "(left/right shift per 1000 em, e.g. %s)" % (
                      len(self.harness_written), "" if not bad else ", EXCEPT %d" % len(bad), ", ".join(examples)))
        if not self.harness_written:
            self.error("the harness Apply wrote no glyph the harness shifts")
        if bad:
            self.error("after the harness Apply %d glyphs' sidebearings differ from the preview, e.g. %s"
                       % (len(bad), bad[:5]))
        self.wait(lambda: self.window_state("after the harness Apply") == "ready", self.harness_revert, 120.0,
                  "the window after the harness Apply")

    def harness_revert(self):
        if not self.call_window(HARNESS, self.win.revert_last_apply):
            raise RuntimeError("the window did not revert the harness Apply")
        self.wait(lambda: self.window_state("during the harness Revert") not in ("applying",), self.harness_reverted,
                  300.0, "reverting the harness Apply")

    def harness_reverted(self):
        win = self.win
        snap = win.snapshot
        after = self.font_state(snap.names, snap.master_id)
        kern_diff = diff_kerning(self.before["kerning"], after["kerning"])
        glyph_diff = diff_glyphs(self.before["glyphs"], after["glyphs"]) + diff_components(self.before, after) + \
            diff_outlines(self.before, after)
        self.log("harness reverted", kerning_differences=len(kern_diff), glyph_differences=len(glyph_diff),
                 examples=(kern_diff + glyph_diff)[:10])
        if kern_diff or glyph_diff:
            self.error("after reverting the harness Apply %d kerning entries and %d glyph values differ"
                       % (len(kern_diff), len(glyph_diff)))
        else:
            self.note("designer harness Revert: the font is as before")
        self.before = None
        # off again, with the main window's switch
        win.w.harness.set(False)
        self.call_window(HARNESS, win.harnessChanged, None)
        self.wait(lambda: self._preview_with(0.0), self.harness_off_again, 120.0, "the preview without the harness")

    def harness_off_again(self):
        m2 = self._metrics()
        back = sum(1 for x, y in zip(self.harness_m0, m2) if any(abs(p - q) > 1e-9 for p, q in zip(x, y)))
        if back:
            self.error("after turning the harness off %d glyphs' sidebearings differ from before" % back)
        else:
            self.note("designer harness off: every glyph back to Kinetikern2's own spacing")
        hw = self.win.harness_window
        if hw is not None:
            hw.close()
        self.after_harness()

    # --- spacing groups -------------------------------------------------------

    def groups_step(self):
        """Freeze the capitals, figures looser with half the force — set up in
        the Spacing Groups window the way a user does it (groups_ui)."""
        import kk2_groups as kg
        self.heartbeat.stage(GROUP_WINDOWS)
        win = self.win
        snap = win.snapshot
        # no groups to begin with (the window then shows the set it is handed)
        self.call_window(GROUP_WINDOWS, win.set_groups, kg.GroupSet(), False)
        t = time.perf_counter()
        self.call_window(GROUP_WINDOWS, win.openGroups)
        groups_ms = (time.perf_counter() - t) * 1000.0
        t = time.perf_counter()
        self.call_window(GROUP_WINDOWS, win.openPairs)
        pairs_ms = (time.perf_counter() - t) * 1000.0
        self.heartbeat.stage(GROUPS)
        self.before_run = win.result
        gw = win.groups_window
        if gw is None:
            raise RuntimeError("the Spacing Groups window did not open")
        capitals, figures = self.groups_ui(gw)
        groups = win.groups
        self.frozen_names = set(capitals)
        self.group_figures = sorted(figures)
        if gw.groups is not groups or len(gw.rows.rows()) != len(groups.groups) + 1:
            self.error("the Spacing Groups window does not show the window's groups (%d rows)" % len(gw.rows.rows()))
        self.log("groups set", capitals=len(capitals), figures=len(figures), summary=groups.summary(set(snap.names)),
                 open_groups_window_ms=round(groups_ms, 1), open_pairs_window_ms=round(pairs_ms, 1))
        self.note("spacing groups: %d Latin capitals frozen, %d figures at Looseness +0.4 and 50%% force; the "
                  "Spacing Groups window opened in %.0f ms, the Pairs window in %.0f ms"
                  % (len(capitals), len(figures), groups_ms, pairs_ms))
        if not capitals or not figures:
            raise RuntimeError("the test font has no Latin capitals or no figures to put into groups")
        self.groups_t = time.time()
        self.wait(lambda: self.window_state("after the groups changed") == "ready" and win.result is not self.before_run,
                  self.groups_whole, 120.0, "waiting for the preview with spacing groups")

    def groups_ui(self, gw):
        """Drives the Spacing Groups window like a user: the grid with real
        mouse events (click, Shift-click, Command-click, a dragged rectangle),
        All / None / Invert, the name filter, a section of the list, New
        Group, the name, Freeze, the sliders, Glyphs in no group, taking a
        glyph out of a group and putting it back. Checks each step; returns
        (capitals, figures) as the window grouped them."""
        from AppKit import NSEvent
        down, up, dragged = 1, 2, 6  # NSEventTypeLeftMouseDown, …Up, …Dragged
        shift, command = 1 << 17, 1 << 20
        results = []

        def check(ok, what):
            results.append([what, bool(ok)])
            if not ok:
                self.error("Spacing Groups window: " + what)

        view, grid = gw.grid_view, gw.grid
        window = view.window()
        names = list(grid.names)

        def event(i, kind, flags=0):
            r = grid.rect_of(i)
            pt = view.convertPoint_toView_((r.origin.x + 0.5 * r.size.width, r.origin.y + 0.5 * r.size.height), None)
            return NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                kind, pt, flags, 0, window.windowNumber(), None, 0, 1, 1.0)

        def click(i, flags=0):
            grid.mouse_down(view, event(i, down, flags))
            grid.mouse_up(view, event(i, up, flags))

        cols = max(1, grid.cols)
        click(2)
        check(grid.selection == {names[2]}, "a click selects one glyph")
        click(6, shift)
        check(grid.selection == set(names[2:7]), "Shift-click selects the range")
        click(4, command)
        check(grid.selection == set(names[2:7]) - {names[4]}, "Command-click takes a glyph out of the selection")
        click(4, command)
        check(names[4] in grid.selection, "Command-click puts it back")
        far = min(len(names) - 1, cols + 2)
        grid.mouse_down(view, event(0, down))
        grid.mouse_dragged(view, event(far, dragged))
        grid.mouse_up(view, event(far, up))
        r0, c0, r1, c1 = 0, 0, far // cols, far % cols
        want = set(names[r * cols + c] for r in range(r0, r1 + 1) for c in range(c0, c1 + 1) if r * cols + c < len(names))
        check(grid.selection == want, "dragging a rectangle selects the tiles inside it")
        gw.selectAllGlyphs(None)
        check(grid.selection == set(names), "All selects every glyph")
        gw.selectNoGlyphs(None)
        check(not grid.selection, "None clears the selection")
        gw.invertSelection(None)
        check(grid.selection == set(names), "Invert of nothing is everything")
        gw.selectNoGlyphs(None)
        gw.w.search.set("zero")
        gw.searchChanged(gw.w.search)
        check(grid.names and all("zero" in n.lower() for n in grid.names), "the name filter keeps the matching glyphs")
        gw.w.search.set("")
        gw.searchChanged(gw.w.search)
        check(list(grid.names) == names, "clearing the filter shows every glyph again")

        titles = [t for t, _ in gw._sections]

        def pick(title):
            if title not in titles:
                self.error("Spacing Groups window: no section %r (sections: %s)" % (title, titles))
                return set()
            k = titles.index(title)
            gw.w.sections.setSelection([k])
            gw.sectionSelected(gw.w.sections)
            return set(gw._sections[k][1])

        def members(gid):
            return set(n for n, k in gw.groups.members.items() if k == gid)

        capitals = pick("Latin · Uppercase")
        check(capitals and grid.selection == capitals, "a section of the list selects its glyphs")
        gw.addGroup(None)
        caps = gw.groups.group(gw.current)
        gw.w.name.set("Capitals")
        gw.nameChanged(gw.w.name)
        gw.w.mode.set(1)
        gw.modeChanged(gw.w.mode)
        check(caps is not None and caps.name == "Capitals" and caps.frozen and members(caps.gid) == capitals,
              "New Group takes the selection; the name and Freeze apply")
        figures = pick("Figures")
        gw.addGroup(None)
        figs = gw.groups.group(gw.current)
        gw.w.name.set("Figures")
        gw.nameChanged(gw.w.name)
        gw.w.loose.set(0.4)
        gw.looseChanged(gw.w.loose)
        gw.w.force.set(50.0)
        gw.forceChanged(gw.w.force)
        check(figs is not None and not figs.frozen and abs(figs.looseness - 0.4) < 1e-6 and abs(figs.force - 50.0) < 1e-6
              and members(figs.gid) == figures, "the Looseness and force sliders set the group's values")
        check(bool(gw.w.loose.getNSSlider().isEnabled()), "the sliders are on for a spaced group")
        gw.choose_group(caps.gid)
        check(not gw.w.loose.getNSSlider().isEnabled(), "a frozen group has no Looseness to set")
        gw.selectUnassigned(None)
        check(grid.selection == set(names) - capitals - figures, "Glyphs in no group selects the rest")
        one = sorted(figures)[0]
        grid.selection = {one}
        gw.unassignButton(None)
        check(one not in gw.groups.members, "Take Selected Glyphs out of Their Groups")
        gw.choose_group(figs.gid)
        grid.selection = {one}
        gw.assign(None)
        check(gw.groups.members.get(one) == figs.gid, "Put Selected Glyphs into the group")
        opts = self.win._glyph_opts()
        index = self.win.snapshot.index
        check(opts is not None and all(opts[index[n]][0] for n in capitals if n in index)
              and all(abs(opts[index[n]][1] - 0.4) < 1e-6 and abs(opts[index[n]][2] - 0.5) < 1e-6 for n in figures if n in index),
              "the engine gets the groups (frozen capitals; figures +0.4 at 50 %)")
        # By Category: the glyphs in no group yet go into groups by kind, at
        # the main settings, the painted groups and their glyphs kept; then
        # everything as it was for the stages that follow
        before = set(g.gid for g in gw.groups.groups)
        members_before = dict(gw.groups.members)
        gw.byCategory(None)
        new = [g for g in gw.groups.groups if g.gid not in before]
        snap = self.win.snapshot
        punct = [n for n in snap.names if getattr(snap.infos.get(n), "category", None) == "Punctuation"
                 and n not in members_before]
        pgroup = next((g for g in gw.groups.groups if g.name == "Punctuation"), None)
        check(pgroup is not None and punct and all(gw.groups.members.get(n) == pgroup.gid for n in punct)
              and all(gw.groups.members.get(n) == k for n, k in members_before.items())
              and all(abs(g.looseness) < 1e-9 and abs(g.force - 100.0) < 1e-9 for g in new),
              "By Category: punctuation (and symbols, the other figures) into groups of their own at the main "
              "settings, the painted groups kept")
        for name in set(gw.groups.members) | set(members_before):
            if gw.groups.members.get(name) != members_before.get(name):
                gw.groups.assign([name], members_before.get(name))
        for g in new:
            gw.choose_group(g.gid)
            gw.removeGroup(None)
        check(set(g.gid for g in gw.groups.groups) == before and gw.groups.members == members_before,
              "the category groups deleted again")
        passed = sum(1 for _, ok in results if ok)
        self.log("groups window ui", checks=results)
        self.note("Spacing Groups window, driven like a user: %d of %d steps as expected (click, Shift-click, "
                  "Command-click, drag, All/None/Invert, filter, sections, New Group, name, Freeze, sliders, "
                  "no group, take out and put back, By Category)" % (passed, len(results)))
        return capitals, figures

    def groups_whole(self):
        win = self.win
        self.before_run = win.result
        self.whole_t = time.time()
        if not self.call_window(GROUPS, win.start_whole_font):
            raise RuntimeError("the whole-font run with spacing groups did not start")
        self.wait(self.whole_finished, self.groups_check, 420.0, "waiting for the whole-font run with groups")

    def frozen_keys(self, snap):
        import kk2_apply
        keys = set()
        groups_of = glyph_groups(self.font)
        layer = _layer(self.font)
        for name in self.frozen_names:
            if name not in layer:
                continue
            keys.add(name)
            left, right = groups_of(name)
            if right:
                keys.add(kk2_apply.LEFT_PREFIX + right)
            if left:
                keys.add(kk2_apply.RIGHT_PREFIX + left)
        return keys

    def groups_check(self):
        import kk2_apply
        win = self.win
        snap = win.snapshot
        res = win.result
        self.before_run = None
        seconds = time.time() - self.whole_t
        fitted = res.fitted_looseness
        index = snap.index
        moved = []
        for name in self.frozen_names:
            i = index.get(name)
            spec = snap.specs[i] if i is not None else None
            if spec is None or spec.cur_lsb != spec.cur_lsb:
                continue
            m = getattr(res, "engine_metrics", res.metrics)[i]  # the engine's frame, as spec.cur_* are
            if abs(m.lsb - spec.cur_lsb) > 1e-3 or abs(m.rsb - spec.cur_rsb) > 1e-3:
                moved.append([name, round(spec.cur_lsb, 1), round(m.lsb, 1), round(spec.cur_rsb, 1), round(m.rsb, 1)])
        # no entry may lie between two frozen glyphs or classes
        n = len(snap.names)
        frozen_idx = set(index[x] for x in self.frozen_names if x in index)
        rmem, lmem = {}, {}
        for i in range(n):
            rmem.setdefault(res.glyph_right_class[i], []).append(i)
            lmem.setdefault(res.glyph_left_class[i], []).append(i)
        between = 0
        for kind, a, b, _v, _imp in res.iter_entries():
            left = [a] if kind in (kb.ENTRY_GLYPH_GLYPH, kb.ENTRY_GLYPH_CLASS) else rmem.get(a, [])
            right = [b] if kind in (kb.ENTRY_GLYPH_GLYPH, kb.ENTRY_CLASS_GLYPH) else lmem.get(b, [])
            if left and right and all(i in frozen_idx for i in left) and all(j in frozen_idx for j in right):
                between += 1
        self.log("groups whole-font run", seconds=round(seconds, 2), fitted_looseness=fitted,
                 frozen_moved=len(moved), frozen_examples=moved[:10], entries=res.entry_count,
                 entries_between_frozen=between)
        self.note("spacing groups: whole font %.1f s, Looseness fitted to the frozen capitals %s, %d entries, %d "
                  "between frozen glyphs, %d frozen glyphs moved"
                  % (seconds, "%+.2f" % fitted if fitted is not None else "—", res.entry_count, between, len(moved)))
        if fitted is None:
            self.error("the solve did not fit the Looseness to the frozen glyphs")
        if moved:
            self.error("%d frozen glyphs got new sidebearings, e.g. %s" % (len(moved), moved[:5]))
        if between:
            self.error("%d kerning entries lie between frozen glyphs" % between)
        # the Pairs window on the font as it is (the designer's spacing against Kinetikern2)
        pw = win.pairs_window
        if pw is None:
            self.warn("the Pairs window was closed")
            self.groups_apply()
            return
        pw.w.scope.set(1)
        pw.scopeChanged(None)
        self.pairs_t = time.time()
        self.call_window(GROUPS, pw.measure)
        self.wait(lambda: hasattr(pw, "_stats") and self.window_state("measuring pairs") == "ready",
                  self.groups_pairs_before, 120.0, "measuring pairs before Apply")

    def groups_pairs_before(self):
        pw = self.win.pairs_window
        names = self.win.snapshot.names
        per = 1000.0 / float(self.win.snapshot.upm)
        top = lambda rows: ["%s %s %+.0f" % (names[a], names[b], r * per) for a, b, _c, _m, r in rows[:6]]
        self.log("pairs before apply", seconds=round(time.time() - self.pairs_t, 2), stats=pw._stats,
                 loosest=top(pw._loose), tightest=top(pw._tight), status=pw.w.status.get())
        self.note("pairs window (the font as it is): %d pairs in %.1f s; loosest %s; tightest %s"
                  % (int(pw._stats.get("pairs", 0)), time.time() - self.pairs_t, ", ".join(top(pw._loose)[:3]),
                     ", ".join(top(pw._tight)[:3])))
        if not pw._loose or not pw._tight:
            self.error("the Pairs window measured no loosest or tightest pairs")
        elif pw.w.list.get():
            pw.w.list.setSelection([0])  # the loosest pair, drawn in the preview
            pw.rowSelected(pw.w.list)
        # let the window server show it before the image is taken
        self.later(0.8, self.groups_capture_pairs)

    def groups_capture_pairs(self):
        pw = self.win.pairs_window
        try:
            self.log("pairs image", source=self.capture_png("pairs.png", pw.w.getNSWindow()))
        except Exception:
            self.warn(traceback.format_exc())
        del pw._stats  # the next measurement (after Apply) is waited for afresh
        self.groups_apply()

    def groups_apply(self):
        """Apply; then the frozen glyphs and the kerning among them must be as they were."""
        import kk2_apply
        win = self.win
        snap = win.snapshot
        res = win.result
        mid = snap.master_id
        self.before = self.font_state(snap.names, mid)
        self.group_keys = self.frozen_keys(snap)
        self.plan = kk2_apply.plan(snap, res, True, frozen=self.frozen_names)
        self.log("groups plan", counts=getattr(self.plan, "counts", None))
        previous = win.last_apply
        self.apply_t = time.time()
        if not self.call_window(GROUPS, win.apply_whole_font_result, confirm=False):
            raise RuntimeError("the window did not apply the result with spacing groups")
        self.wait(lambda: win.last_apply is not previous and self.window_state("during the groups Apply") != "applying",
                  self.groups_applied, 300.0, "applying with spacing groups")

    def groups_applied(self):
        win = self.win
        snap = win.snapshot
        mid = snap.master_id
        after = self.font_state(snap.names, mid)
        keys = self.group_keys
        changed_glyphs = []
        for name in self.frozen_names:
            b, a = self.before["glyphs"].get(name), after["glyphs"].get(name)
            if b != a:
                changed_glyphs.append([name, b, a])
        bk, ak = self.before["kerning"], after["kerning"]
        changed_pairs = []
        for lk in set(bk) | set(ak):
            if lk not in keys:
                continue
            rb, ra = bk.get(lk, {}), ak.get(lk, {})
            for rk in set(rb) | set(ra):
                if rk in keys and rb.get(rk) != ra.get(rk):
                    changed_pairs.append([lk, rk, rb.get(rk), ra.get(rk)])
        figures_moved = 0
        for name in self.group_figures:
            b, a = self.before["glyphs"].get(name), after["glyphs"].get(name)
            if b is not None and a is not None and b[2:] != a[2:]:
                figures_moved += 1
        self.log("groups applied", frozen_glyphs_changed=len(changed_glyphs), frozen_examples=changed_glyphs[:10],
                 frozen_pairs_changed=len(changed_pairs), frozen_pair_examples=changed_pairs[:10],
                 figures_respaced=figures_moved, summary=win.last_apply)
        self.note("spacing groups Apply: %d frozen glyphs and %d kerning entries between frozen glyphs changed; "
                  "%d figures re-spaced" % (len(changed_glyphs), len(changed_pairs), figures_moved))
        if changed_glyphs:
            self.error("Apply changed %d frozen glyphs, e.g. %s" % (len(changed_glyphs), changed_glyphs[:3]))
        if changed_pairs:
            self.error("Apply changed %d kerning entries between frozen glyphs, e.g. %s"
                       % (len(changed_pairs), changed_pairs[:3]))
        self.wait(lambda: self.window_state("after the groups' Apply") == "ready", self.groups_measure, 120.0,
                  "waiting for the window after the groups' Apply")

    def groups_measure(self):
        """The Pairs window measures the whole font as it is now."""
        pw = self.win.pairs_window
        if pw is None:
            self.warn("the Pairs window was closed")
            self.groups_pairs()
            return
        pw.w.scope.set(1)
        pw.scopeChanged(None)
        self.pairs_t = time.time()
        self.call_window(GROUPS, pw.measure)
        self.wait(lambda: hasattr(pw, "_stats") and self.window_state("measuring pairs") == "ready",
                  self.groups_pairs, 120.0, "measuring pairs")

    def groups_pairs(self):
        win = self.win
        pw = win.pairs_window
        if pw is not None and hasattr(pw, "_stats"):
            names = win.snapshot.names
            per = 1000.0 / float(win.snapshot.upm)
            top = lambda rows: ["%s %s %+.0f" % (names[a], names[b], r * per) for a, b, _c, _m, r in rows[:6]]
            self.log("pairs after apply", seconds=round(time.time() - self.pairs_t, 2), stats=pw._stats,
                     loosest=top(pw._loose), tightest=top(pw._tight), status=pw.w.status.get())
            self.note("pairs window after Apply (read again): mean difference %.1f units per 1000 em"
                      % (float(pw._stats.get("mae", 0.0)) * per))
            # right after Apply the font is Kinetikern2's spacing (frozen glyphs aside): the
            # measurement must see that, not the sidebearings read before Apply
            mae = float(pw._stats.get("mae", 0.0)) * per
            if mae > 8.0:
                self.error("after Apply the Pairs window still measures a mean difference of %.1f units per 1000 em "
                           "(the font was not read again?)" % mae)
        else:
            self.warn("the pairs window did not measure")
        gw = win.groups_window
        if gw is not None and gw.groups.groups:
            gw.select_members(gw.groups.groups[0].gid)  # the frozen capitals
        # let the window server show the new state before the images are taken
        self.later(0.8, self.groups_capture)

    def groups_capture(self):
        win = self.win
        for filename, sub in (("groups.png", win.groups_window),):
            if sub is None:
                continue
            try:
                self.log(filename[:-4] + " image", source=self.capture_png(filename, sub.w.getNSWindow()))
            except Exception:
                self.warn(traceback.format_exc())
        # back to the font as it was before the groups' Apply
        self.revert_t = time.time()
        if not self.call_window(GROUPS, win.revert_last_apply):
            raise RuntimeError("the window did not revert the groups' Apply")
        self.wait(lambda: self.window_state("during the groups Revert") != "applying", self.groups_reverted, 300.0,
                  "reverting the groups' apply")

    def groups_reverted(self):
        snap = self.win.snapshot
        after = self.font_state(snap.names, snap.master_id)
        kern_diff = diff_kerning(self.before["kerning"], after["kerning"])
        glyph_diff = diff_glyphs(self.before["glyphs"], after["glyphs"]) + diff_components(self.before, after) + \
            diff_outlines(self.before, after)
        self.log("groups reverted", kerning_differences=len(kern_diff), glyph_differences=len(glyph_diff),
                 examples=(kern_diff + glyph_diff)[:10])
        if kern_diff or glyph_diff:
            self.error("after reverting the groups' Apply %d kerning entries and %d glyph values differ"
                       % (len(kern_diff), len(glyph_diff)))
        else:
            self.note("spacing groups Revert: the font is as before")
        self.before = None
        self.finish()

    # --- reading ------------------------------------------------------------

    def font_state(self, names, master_id):
        """The font's kerning plus groups and (ink) sidebearings of the glyphs,
        and the offsets of every component (composites must stay rigid)."""
        glyphs = {}
        groups_of = glyph_groups(self.font)
        layer = _layer(self.font)
        for name in names:
            m = ink_metrics(self.font, name)
            if m is None:
                continue
            glyphs[name] = groups_of(name) + tuple(m)
        components = {}
        outlines = {}
        for name in layer.keys():
            glyph = layer[name]
            comps = glyph.components
            if comps:
                components[name] = [tuple(c.transformation) for c in comps]
            outlines[name] = outline_state(glyph)
        return {"kerning": kerning_table(self.font), "glyphs": glyphs, "components": components,
                "outlines": outlines}

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
            lsb, rsb, _width = ink_metrics(self.font, name) or (None, None, None)
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

    def capture_png(self, filename, window=None):
        """Save the window as the screen shows it (a process may capture its
        own windows without the Screen Recording permission). A window that is
        not on screen (a floating window hides while RoboFont is in the
        background) is drawn offscreen instead, which leaves pop-up buttons and
        push buttons blank. Returns which of the two it saved."""
        window = window or self.nswindow()
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

    def report_dismissed(self):
        """The alerts dismissed since the last call: another extension's are
        warnings, one of Kinetikern2's own an error."""
        texts = list(getattr(self, "dismissed", ()))
        for text in texts[getattr(self, "_dismissed_seen", 0):]:
            if "Kinetikern2" in text or "Open a font first" in text:
                self.error("an alert of Kinetikern2 came up and was dismissed: %s" % text)
            else:
                self.warn("an alert another extension showed was dismissed (nobody answers it in a test "
                          "instance): %s" % text)
        self._dismissed_seen = len(texts)

    def finish(self):
        if self.finished:
            return
        self.finished = True
        self.report_dismissed()
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
                self.font.close(save=False)
                self.log("font closed")
        except Exception:
            self.error(traceback.format_exc())
        self.font = None
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.heartbeat.stop()
        self.gc_watch.stop()
        self.stall_watch.stop()
        self.report["gc"] = self.gc_watch.gens
        self.report["long_gaps"] = self.heartbeat.long_gaps
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


def _robofont_version():
    try:
        from mojo.roboFont import buildNumber, version
        return "%s (%s)" % (version, buildNumber)
    except Exception:
        return None


def run(resources, dismissed=()):
    """Starts the self-test. `dismissed`: the texts of the alerts the test
    instance dismissed (kk2_startup); it may grow while the test runs."""
    test = SelfTest(resources)
    test.dismissed = dismissed
    test.report_dismissed()
    test.start()
