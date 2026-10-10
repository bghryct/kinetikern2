# encoding: utf-8
"""
kk2_pairs_window — pairs from loosest to tightest.

Measures the font's spacing as it is against Kinetikern2's: for every pair in
scope (the glyphs of the sample text, the whole font, or one section), the
white the eye sees now — right sidebearing + left sidebearing + kerning — and
the white Kinetikern2 gives the pair. The difference, once the font's overall
tightness is taken out, says which pairs the font sets looser or tighter than
its own rhythm: the list shows the extremes either way, each drawn as it is
and as Kinetikern2 would set it. Double-click a pair (or Open in Space Center)
to look at it in RoboFont.

The engine does the measuring (all pairs of a whole font in seconds,
off the main thread: about 20 s for Arial); the font's kerning is read once per measurement, on the main thread.
"""

from __future__ import division, print_function, unicode_literals

import math
import threading
import time
import traceback

import objc
import vanilla
from AppKit import (NSAffineTransform, NSColor, NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
                    NSGraphicsContext, NSMakeRect, NSRectFill, NSString, NSView)

from PyObjCTools import AppHelper

import kk2_bridge as kb
import kk2_host as host

SCOPES = ["Glyphs in the sample text", "The whole font", "One section"]
COUNTS = ["50", "100", "250", "500"]
LEFT_PREFIX = "public.kern1."  # a class on the left of a pair: its glyphs' right-side group
RIGHT_PREFIX = "public.kern2."  # a class on the right of a pair: its glyphs' left-side group


def _class(name, factory):
    try:
        return objc.lookUpClass(name)
    except objc.nosuchclass_error:
        return factory()


def _make_preview_class():
    class KK2PairPreview(NSView):
        def drawRect_(self, rect):
            model = getattr(self, "kk2_model", None)
            if model is not None:
                try:
                    model.draw_preview(self, rect)
                except Exception:
                    print(traceback.format_exc())

    return KK2PairPreview


KK2PairPreview = _class("KK2PairPreview", _make_preview_class)


def current_kerning(snapshot, table):
    """The font's kerning as engine input: (kind, left, right, value) with
    glyph indices and the snapshot's group ids. `table`: the font's kerning
    ({(left, right): value}; a side is a glyph name or a public.kern1 /
    public.kern2 group)."""
    steps = current_kerning_steps(snapshot, table, float("inf"))
    while True:
        try:
            next(steps)
        except StopIteration as stop:
            return stop.value


def current_kerning_steps(snapshot, table, budget_s):
    """current_kerning in slices: a generator that yields after about
    `budget_s` seconds of work and returns the list."""
    if not table:
        return []
    index = snapshot.index
    right_ids, left_ids = snapshot.right_group_ids, snapshot.left_group_ids
    out = []
    deadline = time.perf_counter() + budget_s
    for k, ((lk, rk), value) in enumerate(table.items()):
        if not k & 255 and time.perf_counter() > deadline:
            yield
            deadline = time.perf_counter() + budget_s
        lks, rks = str(lk), str(rk)
        if lks.startswith(LEFT_PREFIX):
            left, lclass = right_ids.get(lks[len(LEFT_PREFIX):]), True
        else:
            left, lclass = index.get(lks), False
        if left is None:
            continue
        if rks.startswith(RIGHT_PREFIX):
            right, rclass = left_ids.get(rks[len(RIGHT_PREFIX):]), True
        else:
            right, rclass = index.get(rks), False
        if right is None:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        kind = (kb.ENTRY_CLASS_CLASS if rclass else kb.ENTRY_CLASS_GLYPH) if lclass else (
            kb.ENTRY_GLYPH_CLASS if rclass else kb.ENTRY_GLYPH_GLYPH)
        out.append((kind, left, right, v))
    return out


class PairsWindow(object):
    def __init__(self, main):
        self.main = main
        self.rows = []
        self.pairs = []  # (left index, right index, current, model, residual)
        self.selected = None
        self.waiting_for_whole = False
        self.waiting_for_read = False
        self._measuring = False
        self._closed = False
        self._build()

    def _build(self):
        title = "Pairs, Loosest to Tightest — %s" % host.font_title(self.main.font)
        w = vanilla.Window((980, 680), title, minSize=(760, 480))
        self.w = w
        w.scopeLabel = vanilla.TextBox((12, 13, 60, 17), "Pairs of", sizeStyle="small")
        w.scope = vanilla.PopUpButton((70, 10, 190, 22), SCOPES, callback=self.scopeChanged, sizeStyle="small")
        w.section = vanilla.PopUpButton((268, 10, 200, 22), [], sizeStyle="small")
        w.order = vanilla.SegmentedButton((480, 9, 220, 24), [dict(title="Loosest first"), dict(title="Tightest first")],
                                          callback=self.orderChanged, sizeStyle="small")
        w.order.set(0)
        w.countLabel = vanilla.TextBox((712, 13, 40, 17), "Show", sizeStyle="small")
        w.count = vanilla.PopUpButton((752, 10, 70, 22), COUNTS, sizeStyle="small")
        w.count.set(1)
        w.measure = vanilla.Button((-140, 9, -12, 24), "Measure", callback=self.measureButton)
        w.status = vanilla.TextBox((12, 42, -12, 30), "", sizeStyle="small")
        w.list = vanilla.List((12, 76, 520, -40), [], columnDescriptions=[
            dict(title="#", key="rank", width=32),
            dict(title="Pair", key="pair", width=124),
            dict(title="Δ", key="delta", width=48),
            dict(title="Gap now", key="now", width=58),
            dict(title="Kinetikern2", key="model", width=72),
            dict(title="Kerning now → model", key="kerning", width=118)],
            selectionCallback=self.rowSelected, doubleClickCallback=self.openTab, allowsMultipleSelection=False,
            drawFocusRing=False)
        self.preview = KK2PairPreview.alloc().initWithFrame_(((0, 0), (420, 400)))
        self.preview.kk2_model = self
        w.previewScroll = vanilla.ScrollView((544, 76, -12, -40), self.preview, hasHorizontalScroller=False,
                                             hasVerticalScroller=False, autohidesScrollers=True)
        w.note = vanilla.TextBox((12, -34, -170, 30),
                                 "Δ = how much looser (+) or tighter (−) the font sets the pair than Kinetikern2 does "
                                 "at the font's own overall tightness. All values in units per 1000 em. Double-click "
                                 "to open the pair in a Space Center.", sizeStyle="mini")
        w.openTabButton = vanilla.Button((-170, -32, -12, 22), "Open in Space Center", callback=self.openTab,
                                         sizeStyle="small")
        w.bind("resize", self.resized)
        w.bind("close", self.closed)
        w.open()
        self._fill_sections()
        self.scopeChanged(None)
        self.resized(None)

    # ------------------------------------------------------------ helpers
    def _fill_sections(self):
        gw = getattr(self.main, "groups_window", None)
        secs = gw._sections if gw is not None and gw._sections else []
        if not secs:
            snap = self.main.snapshot
            if snap is not None:
                import kk2_groups as kg
                secs = kg.snapshot_sections(snap)
        self._sections = secs
        self.w.section.setItems(["%s (%d)" % (t, len(n)) for t, n in secs] or ["—"])

    def resized(self, sender):
        clip = self.preview.superview()
        if clip is not None:
            self.preview.setFrame_(clip.bounds())
            self.preview.setNeedsDisplay_(True)

    def scopeChanged(self, sender):
        self.w.section.enable(self.w.scope.get() == 2)

    def orderChanged(self, sender):
        self._show()

    def _count(self):
        return int(COUNTS[self.w.count.get()])

    # ---------------------------------------------------------- measuring
    def measureButton(self, sender):
        self.measure()

    def measure(self):
        main = self.main
        snap = main.snapshot
        if main._font_written:
            # Apply or Revert changed the font: the engine still has the
            # sidebearings it read before, the kerning would be read as it is now
            if main._stepper is not None:
                self.w.status.set("Wait for Apply or Revert to finish, then Measure.")
                return
            self.waiting_for_read = True
            main._load_master(main._master())
            self.w.status.set("Reading the font again (the last Apply or Revert changed it); the measurement "
                              "follows…")
            return
        if snap is None or main.context is None:
            self.w.status.set("The font is still being read — try again in a moment.")
            return
        scope = self.w.scope.get()
        if scope == 0:
            result = main.result
            if result is None or result.ptr is None:
                self.w.status.set("Waiting for the preview of the sample text…")
                return
            mask = bytearray(len(snap.names))
            for i in main._sample_indices():
                mask[i] = 1
        else:
            if main._result_kind != "whole" or main.result is None or main.result.ptr is None:
                if main.start_whole_font(apply_when_done=False):
                    self.waiting_for_whole = True
                    self.w.status.set("Kerning the whole font first (it runs in the background; the list follows "
                                      "when it is done)…")
                else:
                    self.w.status.set("A whole-font result is needed: wait for the running job, then Measure.")
                return
            result = main.result
            if scope == 2:
                k = self.w.section.get()
                names = self._sections[k][1] if 0 <= k < len(self._sections) else []
                mask = snap.kern_mask(names)
            else:
                mask = None
        if self._measuring:
            return
        try:
            # the font's kerning is read here, on the main thread; the engine measures on another one
            table = main._kerning.table(snap.master_id)
            current = current_kerning(snap, table)
        except Exception as e:
            print(traceback.format_exc())
            self.w.status.set("Could not read the kerning: %s" % e)
            return
        context, engine, cap = main.context, main.engine, self._count()
        main.lease(context)
        main.lease(result)
        self._measuring = True
        self.w.measure.enable(False)
        self.w.status.set("Measuring…")

        def work():
            out = error = None
            try:
                out = engine.measure(context, result, current, mask=mask, scope_scripts=True, cap=cap)
            except Exception as e:
                error = (e, traceback.format_exc())
            AppHelper.callAfter(self._measured, context, result, out, error)

        threading.Thread(target=work, name="kk2-measure", daemon=True).start()

    def _measured(self, context, result, out, error):
        """Back on the main thread: show the measurement, hand the leases back."""
        main = self.main
        try:
            if self._closed:
                return
            if error is not None:
                print(error[1])
                self.w.status.set("Could not measure: %s" % error[0])
                return
            stats, loose, tight = out
            snap = main.snapshot
            self._stats = stats
            self._loose, self._tight = loose, tight
            self._result_metrics = [(m.lsb, m.rsb, m.advance, m.valid) for m in result.metrics]
            self._result_value = {}
            for (a, b, _c, _m, _r) in loose + tight:
                v = result.value(a, b)
                self._result_value[(a, b)] = 0.0 if v != v else float(v)
            per = 1000.0 / float(snap.upm)
            self.w.status.set(
                "%s pairs measured · the font is %s Kinetikern2 overall by %.1f units per 1000 em · mean difference "
                "after that %.1f" % (
                    format(int(stats["pairs"]), ","), "looser than" if stats["offset"] >= 0 else "tighter than",
                    abs(stats["offset"]) * per, stats["mae"] * per))
            self._show()
        except Exception:
            print(traceback.format_exc())
        finally:
            self._measuring = False
            if not self._closed:
                self.w.measure.enable(True)
            main.release(result)
            main.release(context)

    def preview_ready(self):
        """The main window has a fresh preview (after reading the font again)."""
        if self.waiting_for_read:
            self.waiting_for_read = False
            self.measure()

    def whole_ready(self):
        """The main window finished a whole-font run (measure if we asked for it)."""
        if self.waiting_for_whole:
            self.waiting_for_whole = False
            self.measure()

    def _show(self):
        if not hasattr(self, "_loose"):
            return
        snap = self.main.snapshot
        per = 1000.0 / float(snap.upm)
        pairs = self._loose if self.w.order.get() == 0 else self._tight
        names = snap.names
        cur_kern = self.main._kerning
        table = cur_kern.table(snap.master_id)
        rows = []
        for k, (a, b, now, model, res) in enumerate(pairs):
            la, lb = names[a], names[b]
            try:
                kn = cur_kern.value(table, la, lb)
            except Exception:
                kn = 0.0
            km = self._result_value.get((a, b), 0.0)
            rows.append({"rank": k + 1, "pair": "%s  %s" % (la, lb), "delta": "%+.0f" % (res * per),
                         "now": "%.0f" % (now * per), "model": "%.0f" % (model * per),
                         # per 1000 em, like the gaps
                         "kerning": "%.0f → %.0f" % (kn * per, km * per) if kn or km else ""})
        self.pairs = pairs
        self.rows = rows
        self.w.list.set(rows)
        if rows:
            self.w.list.setSelection([0])
        self.rowSelected(None)

    # -------------------------------------------------------------- preview
    def rowSelected(self, sender):
        sel = self.w.list.getSelection()
        self.selected = self.pairs[sel[0]] if sel and sel[0] < len(self.pairs) else None
        self.preview.setNeedsDisplay_(True)

    def draw_preview(self, view, rect):
        NSColor.textBackgroundColor().set()
        NSRectFill(rect)
        snap = self.main.snapshot
        if self.selected is None or snap is None:
            self._label("Measure, then choose a pair.", 12, rect.size.height - 24, 12.0)
            return
        a, b, now, model, res = self.selected
        na, nb = snap.names[a], snap.names[b]
        ia, ib = snap.infos.get(na), snap.infos.get(nb)
        if ia is None or ib is None:
            return
        upm = float(snap.upm)
        h = view.frame().size.height
        wdt = view.frame().size.width
        per = 1000.0 / upm
        # as it is: A at 0, B at A's advance + the kerning now
        try:
            kern_now = self.main._kerning.value(self.main._kerning.table(snap.master_id), na, nb)
        except Exception:
            kern_now = 0.0
        ma, mb = self._result_metrics[a], self._result_metrics[b]
        km = self._result_value.get((a, b), 0.0)
        rows = [
            ("In the font", NSColor.textColor(), 0.0, float(ia.width) + kern_now, 0.0),
            ("Kinetikern2", NSColor.systemBlueColor(), ma[0] - ia.lsb, ma[2] + km, mb[0] - ib.lsb),
        ]
        span = 2.4 * upm
        scale = min((wdt - 40) / span, (h - 80) / (2.6 * upm))
        for k, (label, color, shift_a, b_at, shift_b) in enumerate(rows):
            base_y = h - 40 - (k + 1) * 1.25 * upm * scale
            self._label(label, 14, base_y + 1.0 * upm * scale + 4, 11.0)
            ctx = NSGraphicsContext.currentContext()
            for path, x in ((ia.path, shift_a), (ib.path, b_at + shift_b)):
                if path is None:
                    continue
                ctx.saveGraphicsState()
                t = NSAffineTransform.transform()
                t.translateXBy_yBy_(20 + (x + 0.2 * upm) * scale, base_y)
                t.scaleXBy_yBy_(scale, scale)
                t.concat()
                color.set()
                path.fill()
                ctx.restoreGraphicsState()
        self._label("%s %s · Δ %+.0f units per 1000 em · gap now %.0f, Kinetikern2 %.0f" % (
            na, nb, res * per, now * per, model * per), 14, 14, 11.0)

    def _label(self, s, x, y, size):
        attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(size),
                 NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}
        NSString.stringWithString_(s).drawAtPoint_withAttributes_((x, y), attrs)

    def openTab(self, sender):
        if self.selected is None or self.main.font is None:
            return
        a, b = self.selected[0], self.selected[1]
        names = self.main.snapshot.names
        try:
            host.open_text(self.main.font, host.pair_text(names[a], names[b]))
        except Exception:
            print(traceback.format_exc())

    def closed(self, sender):
        self._closed = True
        self.preview.kk2_model = None
        self.main.pairs_window_closed()

    def close(self):
        try:
            self.w.close()
        except Exception:
            pass
