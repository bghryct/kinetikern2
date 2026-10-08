# encoding: utf-8
"""
kk2_harness_window — the pairs the designer harness changes most.

Lists the font's pairs that the harness (kk2_harness) widens or tightens the
most at the main window's Looseness and the harness strength, and draws the
chosen pair three ways: as the font has it, as Kinetikern2 sets it, and as
Kinetikern2 sets it with the harness. Live: the list and the drawing follow
the main window's sliders and the strength here; while the window is open the
main window's preview also kerns these pairs. Double-click a pair (or Open in
Edit Tab) to look at it in Glyphs.
"""

from __future__ import division, print_function, unicode_literals

import traceback

import objc
import vanilla
from AppKit import (NSAffineTransform, NSColor, NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
                    NSGraphicsContext, NSRectFill, NSString, NSView)

COUNT = 80
FILTERS = [("text", "In running text"), ("letters", "Letters"), ("punctuation", "Punctuation"), ("all", "All pairs")]


def _class(name, factory):
    try:
        return objc.lookUpClass(name)
    except objc.nosuchclass_error:
        return factory()


def _make_preview_class():
    class KK2HarnessPreview(NSView):
        def drawRect_(self, rect):
            model = getattr(self, "kk2_model", None)
            if model is not None:
                try:
                    model.draw_preview(self, rect)
                except Exception:
                    print(traceback.format_exc())

    return KK2HarnessPreview


KK2HarnessPreview = _class("KK2HarnessPreview", _make_preview_class)


class HarnessWindow(object):
    def __init__(self, main):
        self.main = main
        self.pairs = []  # (left index, right index, delta in font units)
        self.selected = None
        self._glyphs = frozenset()
        self._closed = False
        self._build()

    def _build(self):
        title = "Designer Harness — %s" % (self.main.font.familyName if self.main.font else "")
        w = vanilla.Window((980, 660), title, minSize=(980, 460))
        self.w = w
        mw = self.main.w
        w.on = vanilla.CheckBox((12, 10, 190, 20), "Use the designer harness", sizeStyle="small",
                                value=bool(mw.harness.get()), callback=self.onChanged)
        w.strengthLabel = vanilla.TextBox((210, 13, 60, 17), "Strength", sizeStyle="small")
        w.strength = vanilla.Slider((268, 10, 200, 22), minValue=0.0, maxValue=100.0,
                                    value=float(mw.harnessStrength.get()), callback=self.strengthChanged)
        w.strengthValue = vanilla.TextBox((476, 13, 60, 17), "", sizeStyle="small")
        w.which = vanilla.SegmentedButton((-420, 9, -12, 24), [dict(title=t) for _, t in FILTERS],
                                          callback=self.whichChanged, sizeStyle="small")
        w.which.set(0)
        w.status = vanilla.TextBox((12, 40, -12, 32), "", sizeStyle="small")
        w.list = vanilla.List((12, 76, 520, -40), [], columnDescriptions=[
            dict(title="#", key="rank", width=26),
            dict(title="Pair", key="pair", width=130),
            dict(title="Change", key="delta", width=52),
            dict(title="1st, right", key="left", width=62),
            dict(title="2nd, left", key="right", width=60),
            dict(title="Pair", key="pairc", width=44)],
            selectionCallback=self.rowSelected, doubleClickCallback=self.openTab, allowsMultipleSelection=False,
            drawFocusRing=False)
        self.preview = KK2HarnessPreview.alloc().initWithFrame_(((0, 0), (460, 400)))
        self.preview.kk2_model = self
        w.previewScroll = vanilla.ScrollView((544, 76, -12, -40), self.preview, hasHorizontalScroller=False,
                                             hasVerticalScroller=False, autohidesScrollers=True)
        w.note = vanilla.TextBox((12, -34, -170, 30),
                                 "Change = how much wider (+) or tighter (−) the harness makes the pair: the first "
                                 "glyph's right side, the second glyph's left side and a pair correction, in units "
                                 "per 1000 em. Double-click to open the pair in an Edit tab.",
                                 sizeStyle="mini")
        w.openTabButton = vanilla.Button((-150, -32, -12, 22), "Open in Edit Tab", callback=self.openTab,
                                         sizeStyle="small")
        w.bind("resize", self.resized)
        w.bind("close", self.closed)
        w.open()
        self.resized(None)  # the main window refreshes it once it is its harness_window

    # ------------------------------------------------------------ controls
    def whichChanged(self, sender):
        self.refresh()

    def onChanged(self, sender):
        self.main.w.harness.set(bool(self.w.on.get()))
        self.main.harnessChanged(None)

    def strengthChanged(self, sender):
        self.main.w.harnessStrength.set(float(self.w.strength.get()))
        self.main.harnessChanged(None)

    def settings_changed(self):
        """The main window's harness switch, strength or Looseness changed."""
        if self._closed:
            return
        mw = self.main.w
        self.w.on.set(bool(mw.harness.get()))
        self.w.strength.set(float(mw.harnessStrength.get()))
        self.refresh()

    def resized(self, sender):
        if self._closed:
            return
        size = self.w.previewScroll.getNSScrollView().contentSize()
        self.preview.setFrameSize_(size)
        self.preview.setNeedsDisplay_(True)

    # ---------------------------------------------------------------- data
    def plan(self):
        """The harness the list shows: the main window's, or at full strength
        while it is off (the list then shows what turning it on would do)."""
        main = self.main
        plan = main._harness_plan()
        if plan is None and main.snapshot is not None and main.harness_available():
            plan = main._harness_plan(force_strength=float(self.w.strength.get()) / 100.0 or 1.0)
        return plan

    def glyphs(self):
        """Glyph indices the main window's preview should kern for this list."""
        return self._glyphs

    def refresh(self):
        """Recomputes the list (cheap: no engine job; the drawing waits for the
        main window's preview to cover the pairs)."""
        if self._closed:
            return
        main = self.main
        snap = main.snapshot
        self.w.strengthValue.set("%d %%" % round(float(self.w.strength.get())))
        if snap is None:
            self.w.status.set("The font is still being read.")
            return
        if not main.harness_available():
            self.w.status.set("This build of the engine has no designer harness: rebuild the plugin (build.sh).")
            return
        plan = self.plan()
        if plan is None:
            self.w.status.set("The harness could not be computed for this master (see the Macro panel).")
            return
        on = bool(self.w.on.get()) and float(self.w.strength.get()) > 0
        per = 1000.0 / float(snap.upm)
        pairs = plan.top_pairs(snap, COUNT, which=FILTERS[max(0, self.w.which.get())][0])
        names = snap.names
        rows = []
        for k, (a, b, d) in enumerate(pairs):
            ca = getattr(snap.infos.get(names[a]), "char", None) or ""
            cb = getattr(snap.infos.get(names[b]), "char", None) or ""
            label = "%s%s   %s %s" % (ca, cb, names[a], names[b]) if ca and cb else "%s %s" % (names[a], names[b])
            rows.append({"rank": k + 1, "pair": label, "delta": "%+.0f" % (d * per),
                         "left": "%+.0f" % (plan.sides[a][1] * per), "right": "%+.0f" % (plan.sides[b][0] * per),
                         "pairc": "%+.0f" % (plan.pair_value.get((a, b), 0.0) * per)
                         if (a, b) in plan.pair_value else ""})
        keep = self.selected
        self.pairs = pairs
        self.w.list.set(rows)
        glyphs = frozenset(i for a, b, _ in pairs for i in (a, b))
        if glyphs != self._glyphs:
            self._glyphs = glyphs
            main._request_preview()  # kern these pairs too
        self.w.status.set(
            ("Live: the preview and Apply use it. " if on else "Off: this is what it would do at %d %%. " % round(
                100 * plan.strength)) + plan.summary().capitalize() +
            ". Learned from %d text families on Google Fonts rated well spaced (%d weights and widths)." % (
                plan.table_families, plan.table_observations))
        if rows:
            idx = 0
            if keep is not None:
                for k, (a, b, _d) in enumerate(pairs):
                    if (a, b) == keep[:2]:
                        idx = k
                        break
            self.w.list.setSelection([idx])
        self.rowSelected(None)

    def result_ready(self):
        """The main window has a new preview or whole-font result."""
        if not self._closed:
            self.preview.setNeedsDisplay_(True)

    # -------------------------------------------------------------- preview
    def rowSelected(self, sender):
        sel = self.w.list.getSelection()
        self.selected = self.pairs[sel[0]] if sel and sel[0] < len(self.pairs) else None
        self.preview.setNeedsDisplay_(True)

    def draw_preview(self, view, rect):
        NSColor.textBackgroundColor().set()
        NSRectFill(rect)
        main = self.main
        snap = main.snapshot
        if self.selected is None or snap is None:
            self._label("Choose a pair.", 12, rect.size.height - 24, 12.0)
            return
        a, b, _d = self.selected
        na, nb = snap.names[a], snap.names[b]
        ia, ib = snap.infos.get(na), snap.infos.get(nb)
        if ia is None or ib is None:
            return
        upm = float(snap.upm)
        per = 1000.0 / upm
        h = view.frame().size.height
        wdt = view.frame().size.width
        try:
            kern_now = main._kerning.value(main._kerning.table(snap.master_id), na, nb)
        except Exception:
            kern_now = 0.0
        rows = [("In the font", NSColor.textColor(), 0.0, float(ia.width) + kern_now, 0.0)]
        model = main.model_pair(a, b)
        plan = self.plan()
        if model is None or plan is None:
            self._label("Waiting for the preview to kern this pair…", 14, 14, 11.0)
        else:
            (la, ra, adv_a), (lb, _rb, _adv_b), k = model
            rows.append(("Kinetikern2", NSColor.systemBlueColor(), la - ia.lsb, adv_a + k, lb - ib.lsb))
            sa, sb = plan.sides[a], plan.sides[b]
            pc = plan.pair_value.get((a, b), 0.0)
            rows.append(("With the harness", NSColor.systemOrangeColor(), la + sa[0] - ia.lsb,
                         adv_a + sa[0] + sa[1] + k + pc, lb + sb[0] - ib.lsb))
        scale = min((wdt - 40) / (2.4 * upm), (h - 80) / (3.9 * upm))
        for r, (label, color, shift_a, b_at, shift_b) in enumerate(rows):
            base_y = h - 40 - (r + 1) * 1.25 * upm * scale
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
        if model is not None and plan is not None:
            (la, ra, adv_a), (lb, _rb, _adv_b), k = model
            gap = ra + k + lb
            d = plan.delta(a, b)
            self._label("%s %s · Kinetikern2's gap %.0f, with the harness %.0f (%+.0f) units per 1000 em" % (
                na, nb, gap * per, (gap + d) * per, d * per), 14, 14, 11.0)

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
            self.main.font.newTab("/%s/%s /H/%s/%s/H /o/%s/%s/o" % (names[a], names[b], names[a], names[b],
                                                                    names[a], names[b]))
        except Exception:
            print(traceback.format_exc())

    def closed(self, sender):
        self._closed = True
        self.preview.kk2_model = None
        self._glyphs = frozenset()
        self.main.harness_window_closed()

    def close(self):
        try:
            self.w.close()
        except Exception:
            pass
