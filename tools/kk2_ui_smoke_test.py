#!/usr/bin/env python3
"""
kk2_ui_smoke_test.py — drive the Kinetikern2 window outside Glyphs.

Run with Glyphs' own Python (PyObjC; vanilla and fontTools come from the
Glyphs repositories):

    GPY="$HOME/Library/Application Support/Glyphs 3/Repositories/GlyphsPythonPlugin/Python.framework/Versions/3.11/bin/python3"
    "$GPY" Kinetikern2/tools/kk2_ui_smoke_test.py [--font Arial.ttf] [--glyphs 400] [--png window.png]

A mock of the GlyphsApp API serves the first 400 spacing glyphs of a font
(tools/kk2_fonts.py picks them, as the plugin's snapshot does) as a two-master
font with kerning groups, metrics keys, auto-aligned composites, a space, a few
marks, a non-exporting glyph and some existing kerning. The window opens
invisibly and is driven through its controls the way a user drives it: sliders,
fields, pop-ups and buttons are found by what they show (titles, ranges,
items) and send their actions; the confirmation alert is answered by the test.

  1. sliced reading of the outlines, Phase 1 and the first preview, with the
     run loop in the event-tracking mode (as during a slider drag)
  2. the engine input the snapshot built: spacing set, groups, metrics keys,
     aligned composites, composite bases, tabular figures, scripts
  3. slider and threshold changes while a slow preview runs: the running job
     is cancelled, at most one job runs, and the preview equals a direct
     engine solve with the settings the controls show
  4. Apply to Font for the sample text (Cancel in the alert writes nothing),
     then Revert Last Apply; both write in slices from the window's timer
  5. Apply to Font for the whole font: the master is read again first (the
     sample Apply changed it), the run is cancelled at ~30 % and
     started again; after Apply every written entry, group and sidebearing
     reads back, the mock font's kerning and spacing equal the engine result
     (Glyphs' key precedence), both proof panes show the same spacing, the
     other master is untouched; Revert restores the mock font exactly
  6. closing the window invalidates its timers and frees its job, result and
     context; a second window comes back with the saved settings and closes
     while it reads

Throughout, a 60 Hz heartbeat timer measures the longest main-thread stall
while the window works, every control callback is timed, and the mock records
every write with the undo and interface-update state it happened in.

Prints PASS / FAIL / INFO lines and exits non-zero when anything failed.
"""

from __future__ import division, print_function

import argparse
import gc
import math
import os
import re
import sys
import time
import traceback
import types
import unicodedata
import uuid
import weakref

HERE = os.path.dirname(os.path.abspath(__file__))
KK2 = os.path.dirname(HERE)
RESOURCES = os.path.join(KK2, "plugin", "Kinetikern2.glyphsPlugin", "Contents", "Resources")
GLYPHS_REPOS = os.path.expanduser("~/Library/Application Support/Glyphs 3/Repositories")
for p in (os.path.join(GLYPHS_REPOS, "fonttools", "Lib"), os.path.join(GLYPHS_REPOS, "vanilla", "Lib"), HERE,
          RESOURCES):
    if p not in sys.path:
        sys.path.insert(0, p)

import AppKit  # noqa: E402
from AppKit import (NSAlertFirstButtonReturn, NSAlertSecondButtonReturn, NSApp, NSAppearance,  # noqa: E402
                    NSApplication, NSApplicationActivationPolicyAccessory, NSAttachmentAttributeName, NSBezierPath,
                    NSBitmapImageFileTypePNG, NSButton, NSControlTextDidChangeNotification,
                    NSEventTrackingRunLoopMode, NSMakeRect, NSNotification, NSPopUpButton, NSProgressIndicator,
                    NSSlider, NSTextField, NSTextView)
from Foundation import NSDate, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer  # noqa: E402
from fontTools import unicodedata as fud  # noqa: E402
from fontTools.pens.basePen import BasePen  # noqa: E402

import kk2_bridge as kb  # noqa: E402
import kk2_fonts as kf  # noqa: E402

PREFIX = "com.mirkovelimirovic.Kinetikern2."
SAMPLE = ("Hamburgefonstiv HOHOHO nonono dbqpmuh\n"
          "AVATAR TYPE WAVE LT Tolerance Yo Ta Te Vo P. F, L (H) [n] -x-\n"
          "ÂÃÅÑÕ âãåñõ ÁÉÍÓÚ áéíóú 0123456789\n"
          "/T/o/T/a/V/A /r/period /y/period")
CLASSIC = ("AV", "To", "Ta", "Yo", "LT", "P.")
PHASE_TEXT = re.compile(r"Phase\s*(\d+)\s*/\s*(\d+)\s*:\s*([^\[]*?)\s*\[\s*(\d+)\s*%\s*\]")
READING_TEXT = re.compile(r"Reading outlines\s*\[\s*(\d+)\s*%\s*\]")
WRITES = ("LSB", "RSB", "width", "left group", "right group", "kern", "unkern", "sync LSB", "sync RSB")


class Abort(Exception):
    """A step cannot go on (a timeout, a missing control)."""


# ------------------------------------------------------------------ mock API
# GlyphsApp's constants (Glyphs 3.5, GlyphsApp/__init__)
LTR, BIDI, RTL, LTRTTB, RTLTTB = 0, 1, 2, 4, 8
GSNoCase, GSUppercase, GSLowercase, GSSmallcaps, GSMinor, GSOtherCase = 0, 1, 2, 3, 4, 5
NS_NOT_FOUND = float(0x7FFFFFFFFFFFFFFF)  # Glyphs' answer for a kerning pair it does not have
GLYPHS_SCRIPTS = dict((iso, name) for name, iso in kb.SCRIPT_NAMES.items())  # ISO 15924 → glyph.script
_KEY_OFFSET = re.compile(r"^(.*?)\s*([+-])\s*(\d+(?:\.\d*)?)$")
_KEY_NUMBER = re.compile(r"^-?\d+(?:\.\d*)?$")


class Defaults(dict):
    """Glyphs.defaults: a missing key reads as None, setting None removes it."""

    def __getitem__(self, key):
        return self.get(key)

    def __setitem__(self, key, value):
        if value is None:
            self.pop(key, None)
        else:
            dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        self.pop(key, None)


class MockGlyphs(object):
    font = None
    fonts = []
    defaults = Defaults()
    versionString = "3.5 (mock)"
    buildNumber = "0"
    messages = []

    @staticmethod
    def localize(strings):
        return strings.get("en")

    @staticmethod
    def showMacroWindow():
        pass


def message(text, title="", OKButton=None):
    MockGlyphs.messages.append((title, text))
    print("MESSAGE:", title, text)


MISSING_API = []


def install_glyphsapp():
    module = types.ModuleType("GlyphsApp")
    module.__dict__.update(
        Glyphs=MockGlyphs, Message=message, LTR=LTR, BIDI=BIDI, RTL=RTL, LTRTTB=LTRTTB, RTLTTB=RTLTTB, GSLTR=LTR,
        GSBIDI=BIDI, GSRTL=RTL, GSVertical=LTRTTB, GSNoCase=GSNoCase, GSUppercase=GSUppercase,
        GSLowercase=GSLowercase, GSSmallcaps=GSSmallcaps, GSMinor=GSMinor, GSOtherCase=GSOtherCase,
        FILTER_MENU="FILTER_MENU", GLYPH_MENU="GLYPH_MENU", GSShapeTypePath=2, GSShapeTypeComponent=4)

    def missing(name):
        # `from GlyphsApp import X` of a name the mock lacks fails as it must, and is reported
        if not name.startswith("__"):
            MISSING_API.append(name)
        raise AttributeError("the GlyphsApp mock has no %r" % name)

    module.__getattr__ = missing
    sys.modules["GlyphsApp"] = module


def parse_key(key):
    """A metrics key the way the mock evaluates it: None, ("number", value) or
    ("follow", target, opposite, offset), target "" being the glyph itself."""
    if not key:
        return None
    body = key[1:].strip() if key.startswith("=") else key.strip()
    opposite = body.startswith("|")
    if opposite:
        body = body[1:].strip()
    offset = 0.0
    m = _KEY_OFFSET.match(body)
    if m:
        body, offset = m.group(1).strip(), float(m.group(2) + m.group(3))
    if _KEY_NUMBER.match(body):
        return ("number", float(body) + offset)
    return ("follow", body, opposite, offset)


class _SegmentPen(BasePen):
    """Collects a decomposed outline as segments (BasePen turns quadratic
    TrueType curves into cubics and draws components through the glyph set)."""

    def __init__(self, glyph_set):
        BasePen.__init__(self, glyph_set)
        self.segments = []

    def _moveTo(self, pt):
        self.segments.append(("M", pt))

    def _lineTo(self, pt):
        self.segments.append(("L", pt))

    def _curveToOne(self, pt1, pt2, pt3):
        self.segments.append(("C", pt1, pt2, pt3))

    def _closePath(self):
        self.segments.append(("Z",))

    def _endPath(self):
        pass  # an open contour stays open: no ink


class Outline(object):
    """A glyph's decomposed outline in font units, drawn at a horizontal offset."""

    def __init__(self, segments):
        self.segments = segments
        self._ink = None

    def path(self, dx=0.0):
        p = NSBezierPath.bezierPath()
        for seg in self.segments:
            op = seg[0]
            if op == "M":
                p.moveToPoint_((seg[1][0] + dx, seg[1][1]))
            elif op == "L":
                p.lineToPoint_((seg[1][0] + dx, seg[1][1]))
            elif op == "C":
                p.curveToPoint_controlPoint1_controlPoint2_((seg[3][0] + dx, seg[3][1]),
                                                            (seg[1][0] + dx, seg[1][1]), (seg[2][0] + dx, seg[2][1]))
            else:
                p.closePath()
        return p

    @property
    def ink(self):
        """(x min, x max) of the outline drawn at offset 0; None without ink."""
        if self._ink is None:
            p = self.path()
            if p.elementCount():
                r = p.bounds()
                self._ink = (float(r.origin.x), float(r.origin.x + r.size.width))
            else:
                self._ink = False
        return self._ink or None


class MockComponent(object):
    def __init__(self, name):
        self.componentName = self.name = name
        self.position = (0.0, 0.0)
        self.automaticAlignment = True


class MockLayer(object):
    """One master layer. Moving the LSB moves the outline (and the width with
    it), moving the RSB changes the width, as in Glyphs; metrics keys act only
    on syncMetrics(). An auto-aligned composite takes its offset and width from
    its first component's layer, and writes to it change nothing (they are
    recorded, so the test can tell)."""

    def __init__(self, glyph, master, outline, width, components=(), aligned=False):
        self.parent = glyph
        self.master = master
        self.associatedMasterId = self.layerId = master.id
        self.name = master.name
        self.components = [MockComponent(n) for n in components]
        self.paths = [] if components or not outline.segments else [outline]
        self.isAligned = bool(aligned and components)
        self.leftMetricsKey = self.rightMetricsKey = self.widthMetricsKey = None
        self._outline = outline
        self._width = float(width)
        self._dx = 0.0
        self._path = None  # (offset, NSBezierPath)

    @property
    def shapes(self):
        return list(self.paths) + list(self.components)

    def _font(self):
        return self.parent.parent

    def _base(self):
        if not self.isAligned:
            return None
        glyph = self._font().glyphs[self.components[0].componentName]
        return glyph.layers[self.layerId] if glyph is not None else None

    def _frame(self):
        """(outline offset, width); an aligned composite's are its base's."""
        base = self._base()
        if base is not None:
            return base._frame()
        return self._dx, self._width

    @property
    def width(self):
        return self._frame()[1]

    @width.setter
    def width(self, value):
        self._write("width", float(value))

    @property
    def LSB(self):
        ink = self._outline.ink
        return ink[0] + self._frame()[0] if ink else 0.0

    @LSB.setter
    def LSB(self, value):
        self._write("LSB", float(value))

    @property
    def RSB(self):
        dx, width = self._frame()
        ink = self._outline.ink
        return width - (ink[1] + dx) if ink else width

    @RSB.setter
    def RSB(self, value):
        self._write("RSB", float(value))

    @property
    def completeBezierPath(self):
        dx = self._frame()[0]
        if self._path is None or self._path[0] != dx:
            self._path = (dx, self._outline.path(dx))
        return self._path[1].copy()  # a new path each time, as Glyphs hands them out

    @property
    def bezierPath(self):
        return None if self.components else self.completeBezierPath

    @property
    def bounds(self):
        path = self.completeBezierPath
        return path.bounds() if path.elementCount() else NSMakeRect(0, 0, 0, 0)

    def _write(self, side, value):
        old = getattr(self, side)
        if self.isAligned:
            if abs(value - old) > 1e-9:
                self._font().record("aligned " + side, self.parent.name, self.layerId, old, value)
            return
        self._set(side, value)
        self._font().record(side, self.parent.name, self.layerId, old, value)

    def _set(self, side, value):
        if side == "width":
            self._width = value
        elif side == "LSB":
            if self._outline.ink:
                delta = value - self.LSB
                self._dx += delta
                self._width += delta
        else:
            self._width += value - self.RSB

    def alignComponents(self):
        """An auto-aligned composite's frame is its base's already."""

    def syncMetrics(self):
        """Recomputes the sides that have metrics keys (the layer's key wins
        over the glyph's): "=X", "=X+n", "=X-n", "=|X", "=|" and "=n"."""
        if self.isAligned:
            return
        glyph = self.parent
        for side, key in (("LSB", self.leftMetricsKey or glyph.leftMetricsKey),
                          ("RSB", self.rightMetricsKey or glyph.rightMetricsKey)):
            value = self._key_value(key, side == "LSB")
            if value is None:
                continue
            old = getattr(self, side)
            if abs(value - old) > 1e-9:
                self._set(side, value)
                self._font().record("sync " + side, glyph.name, self.layerId, old, value)

    def _key_value(self, key, left):
        rule = parse_key(key)
        if rule is None:
            return None
        if rule[0] == "number":
            return rule[1]
        _, target, opposite, offset = rule
        if target:
            glyph = self._font().glyphs[target]
            layer = glyph.layers[self.layerId] if glyph is not None else None
            if layer is None:
                return None
        else:
            layer = self
        if left:
            return (layer.RSB if opposite else layer.LSB) + offset
        return (layer.LSB if opposite else layer.RSB) + offset


class LayerList(object):
    def __init__(self):
        self._list = []
        self._by_id = {}

    def append(self, layer):
        self._list.append(layer)
        self._by_id[layer.layerId] = layer

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._list[key]
        return self._by_id.get(key)

    def __iter__(self):
        return iter(self._list)

    def __len__(self):
        return len(self._list)


class MockGlyph(object):
    def __init__(self, font, name, codepoint=None, export=True, category=None, sub=None, case=GSNoCase,
                 script=None):
        self.parent = font
        self.name = name
        self.id = str(uuid.uuid5(uuid.NAMESPACE_URL, "kk2-smoke-test/" + name)).upper()
        self.export = export
        self.unicodes = ("%04X" % codepoint,) if codepoint is not None else ()
        self.category = category
        self.subCategory = sub
        self.case = case
        self.script = script
        self.direction = LTR
        self.leftMetricsKey = self.rightMetricsKey = self.widthMetricsKey = None
        self.layers = LayerList()
        self._groups = [None, None]

    @property
    def unicode(self):
        return self.unicodes[0] if self.unicodes else None

    @property
    def leftKerningGroup(self):
        return self._groups[0]

    @leftKerningGroup.setter
    def leftKerningGroup(self, value):
        self._set_group(0, value)

    @property
    def rightKerningGroup(self):
        return self._groups[1]

    @rightKerningGroup.setter
    def rightKerningGroup(self, value):
        self._set_group(1, value)

    def _set_group(self, side, value):
        value = str(value) if value else None
        old = self._groups[side]
        self._groups[side] = value
        self.parent.record("left group" if side == 0 else "right group", self.name, None, old, value)


class GlyphList(object):
    """font.glyphs: by index or by name (None for an unknown name)."""

    def __init__(self):
        self._list = []
        self._by_name = {}

    def append(self, glyph):
        self._list.append(glyph)
        self._by_name[glyph.name] = glyph

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._list[key]
        return self._by_name.get(key)

    def __iter__(self):
        return iter(self._list)

    def __len__(self):
        return len(self._list)


class MasterList(list):
    def __getitem__(self, key):
        if isinstance(key, str):
            for m in self:
                if m.id == key:
                    return m
            return None
        return list.__getitem__(self, key)


class MockMaster(object):
    def __init__(self, master_id, name, tt):
        upm = tt["head"].unitsPerEm
        os2 = tt["OS/2"]
        self.id = master_id
        self.name = name
        self.ascender = float(tt["hhea"].ascent)
        self.descender = float(tt["hhea"].descent)
        self.capHeight = float(getattr(os2, "sCapHeight", 0) or upm * 0.7)
        self.xHeight = float(getattr(os2, "sxHeight", 0) or upm * 0.5)
        self.italicAngle = 0.0


class MockUndoManager(object):
    def __init__(self):
        self.disabled = 0
        self.registrations = 0
        self.errors = []

    def disableUndoRegistration(self):
        self.disabled += 1

    def enableUndoRegistration(self):
        if self.disabled == 0:
            self.errors.append("enableUndoRegistration without a matching disableUndoRegistration")
            raise RuntimeError("NSInternalInconsistencyException: undo registration is already enabled")
        self.disabled -= 1

    def isUndoRegistrationEnabled(self):
        return self.disabled == 0

    def canUndo(self):
        return self.registrations > 0

    def removeAllActions(self):
        """As NSUndoManager: clears the stacks and turns registration back on."""
        self.registrations = 0
        self.disabled = 0

    def beginUndoGrouping(self):
        pass

    def endUndoGrouping(self):
        pass


class MockDocument(object):
    def __init__(self, font):
        self.font = font

    def undoManager(self):
        return self.font.undo


class MockFont(object):
    """The GSFont parts Kinetikern2 uses. Every write is recorded with the
    undo and interface-update state it happened in (font.log)."""

    def __init__(self, family, upm, masters):
        self.familyName = family
        self.upm = upm
        self.masters = MasterList(masters)
        self.glyphs = GlyphList()
        self.kerningLTR = dict((m.id, {}) for m in masters)  # the live tables, as Glyphs hands them out
        self.kerningRTL = dict((m.id, {}) for m in masters)
        self.selectedFontMaster = masters[0]
        self.masterIndex = 0
        self.filepath = None
        self.undo = MockUndoManager()
        self.parent = MockDocument(self)
        self.updates_disabled = 0
        self.update_errors = 0
        self.log = []  # (kind, what, master id, old, new, undo on, interface updates off)
        self.bad_keys = []
        self._ids = {}

    def add_glyph(self, glyph):
        self.glyphs.append(glyph)
        self._ids[glyph.id] = glyph

    def undoManager(self):
        return self.undo

    def disableUpdateInterface(self):
        self.updates_disabled += 1

    def enableUpdateInterface(self):
        if self.updates_disabled == 0:
            self.update_errors += 1
            return
        self.updates_disabled -= 1

    def record(self, kind, what, master_id, old, new):
        undo_on = self.undo.disabled == 0
        if undo_on:
            self.undo.registrations += 1
        self.log.append((kind, what, master_id, old, new, undo_on, self.updates_disabled > 0))

    # kerning, as GSFont's Objective-C methods
    def _tables(self, direction):
        return self.kerningRTL if direction == RTL else self.kerningLTR

    def _check_key(self, key, left):
        if key.startswith("@"):
            ok = key.startswith("@MMK_L_" if left else "@MMK_R_") and len(key) > 7
        else:
            ok = key in self._ids  # glyph-level keys are glyph ids
        if not ok:
            self.bad_keys.append(("left" if left else "right", key))

    def setKerningForFontMasterID_leftKey_rightKey_value_direction_(self, master_id, left, right, value, direction):
        left, right = str(left), str(right)
        self._check_key(left, True)
        self._check_key(right, False)
        row = self._tables(direction).setdefault(str(master_id), {}).setdefault(left, {})
        old = row.get(right)
        row[right] = value
        self.record("kern", (left, right), master_id, old, value)

    def removeKerningForFontMasterID_leftKey_rightKey_direction_(self, master_id, left, right, direction):
        left, right = str(left), str(right)
        table = self._tables(direction).get(str(master_id)) or {}
        row = table.get(left)
        if row is None or right not in row:
            return
        old = row.pop(right)
        if not row:
            del table[left]
        self.record("unkern", (left, right), master_id, old, None)

    def kerningForFontMasterID_leftKey_rightKey_direction_(self, master_id, left, right, direction):
        row = (self._tables(direction).get(str(master_id)) or {}).get(str(left))
        if row is None or str(right) not in row:
            return NS_NOT_FOUND
        return float(row[str(right)])

    # kerning, as the Python wrapper (glyph names or @MMK keys)
    def _pair_key(self, key):
        key = str(key)
        if key.startswith("@"):
            return key
        glyph = self.glyphs[key]
        return glyph.id if glyph is not None else key

    def setKerningForPair(self, master_id, left, right, value, direction=LTR):
        self.setKerningForFontMasterID_leftKey_rightKey_value_direction_(
            master_id, self._pair_key(left), self._pair_key(right), value, direction)

    def kerningForPair(self, master_id, left, right, direction=LTR):
        return self.kerningForFontMasterID_leftKey_rightKey_direction_(
            master_id, self._pair_key(left), self._pair_key(right), direction)

    def removeKerningForPair(self, master_id, left, right, direction=LTR):
        self.removeKerningForFontMasterID_leftKey_rightKey_direction_(
            master_id, self._pair_key(left), self._pair_key(right), direction)

    def state(self):
        """Everything Apply and Revert may change, as plain values."""
        kerning = dict((mid, dict((lk, dict(row)) for lk, row in table.items()))
                       for mid, table in self.kerningLTR.items())
        groups = dict((g.name, tuple(g._groups)) for g in self.glyphs)
        metrics = dict(((g.name, layer.layerId), (layer.LSB, layer.RSB, layer.width))
                       for g in self.glyphs for layer in g.layers)
        return {"kerning": kerning, "groups": groups, "metrics": metrics}

    def writes(self):
        return [e for e in self.log if e[0] in WRITES or e[0].startswith("aligned ")]


def diff_state(a, b, masters=None, tolerance=1e-6):
    """[(what, key, before, after)] for everything that differs."""
    out = []
    for mid in sorted(set(a["kerning"]) | set(b["kerning"])):
        if masters is not None and mid not in masters:
            continue
        ta, tb = a["kerning"].get(mid, {}), b["kerning"].get(mid, {})
        for lk in sorted(set(ta) | set(tb)):
            ra, rb = ta.get(lk, {}), tb.get(lk, {})
            for rk in sorted(set(ra) | set(rb)):
                if ra.get(rk) != rb.get(rk):
                    out.append(("kerning " + mid, (lk, rk), ra.get(rk), rb.get(rk)))
    if masters is None:
        for name in sorted(set(a["groups"]) | set(b["groups"])):
            if a["groups"].get(name) != b["groups"].get(name):
                out.append(("groups", name, a["groups"].get(name), b["groups"].get(name)))
    for key in sorted(set(a["metrics"]) | set(b["metrics"])):
        if masters is not None and key[1] not in masters:
            continue
        ma, mb = a["metrics"].get(key), b["metrics"].get(key)
        if ma is None or mb is None or any(abs(x - y) > tolerance for x, y in zip(ma, mb)):
            out.append(("metrics", key, ma, mb))
    return out


# the mock font's designer data: groups, metrics keys, existing kerning
FAMILY_GROUPS = (("A", "A", "A"), ("O", "O", "O"), ("o", "o", "o"))  # base glyph and its composites
GLYPH_GROUPS = {
    "C": ("O", None), "G": ("O", None), "Q": ("O", "O"), "D": ("H", "O"),
    "H": ("H", "H"), "I": ("H", "H"), "M": ("H", "H"), "N": ("H", "H"),
    "B": ("H", None), "E": ("H", None), "F": ("H", None), "K": ("H", None), "L": ("H", None), "P": ("H", None),
    "R": ("H", None),
    "c": ("o", None), "d": ("o", None), "e": ("o", None), "q": ("o", None), "b": (None, "o"), "p": (None, "o"),
    "n": ("n", "n"), "m": ("n", "n"), "h": (None, "n"), "r": ("n", None),
    "T": ("T", "T"), "V": ("V", "V"), "W": ("V", "V"), "Y": ("Y", "Y"),
    "i": ("stem", "stem"), "l": ("stem", "stem"), "dotlessi": ("stem", "stem"),
    # a left group named like a glyph that is not in it: a new class named "x" must not merge into it
    "k": ("x", None),
}
GLYPH_KEYS = {  # on the glyph (every master); "=FIXED" becomes a number in font units
    "d": ("=o", "=l"), "b": ("=l", "=o"), "q": ("=c", None), "parenright": ("=|parenleft", "=|parenleft"),
    "m": ("=n", "=n"), "u": ("=|n", "=|n"), "h": ("=l", "=n+3"), "O": (None, "=|"), "hyphen": ("=FIXED", None),
}
LAYER_KEYS = {"q": ("=o", None)}  # first master only: the layer's key wins over the glyph's
EXISTING_KERNING = (
    [("@MMK_L_A", "@MMK_R_O", -20), ("@MMK_L_T", "@MMK_R_o", -90), ("T", "o", -80), ("V", "@MMK_R_A", -60),
     ("@MMK_L_o", "v", -10), ("space", "A", -7), ("A", "space", 5), ("@MMK_L_unused", "@MMK_R_unused", -3)],
    [("@MMK_L_A", "@MMK_R_O", -25), ("T", "o", -85), ("space", "A", -9)],
)


def glyph_properties(codepoint):
    """(category, subCategory, case, script) the way Glyphs describes a character."""
    ch = chr(codepoint)
    cat = unicodedata.category(ch)
    category = {"L": "Letter", "N": "Number", "P": "Punctuation", "S": "Symbol", "Z": "Separator",
                "M": "Mark"}.get(cat[0], "Other")
    sub, case = None, GSNoCase
    if cat in ("Lu", "Lt"):
        sub, case = "Uppercase", GSUppercase
    elif cat[0] == "L":
        sub, case = "Lowercase", GSLowercase  # as tools/kk2_fonts.rhythm_group: Ll, Lm and Lo set lowercase
    elif cat == "Nd":
        sub = "Decimal Digit"
    elif cat[0] == "M":
        sub = "Nonspacing"
    elif cat[0] == "Z":
        sub = "Space"
    return category, sub, case, GLYPHS_SCRIPTS.get(fud.script(ch))


def build_mock_font(path, count):
    """(MockFont, LoadedFont): the first `count` spacing glyphs of the font as
    tools/kk2_fonts.py selects them, in that order, then a space, a few marks
    and a non-exporting alternate; two masters with the same outlines."""
    lf = kf.LoadedFont(path, limit=count)
    tt = lf.font
    glyph_set = tt.getGlyphSet()
    cmap = tt.getBestCmap() or {}
    glyf = tt["glyf"] if "glyf" in tt else None
    hmtx = tt["hmtx"]
    family = tt["name"].getDebugName(1) or os.path.basename(path)
    masters = [MockMaster("m01", "Regular", tt), MockMaster("m02", "Wide", tt)]
    font = MockFont(family, lf.upm, masters)

    names = list(lf.names)
    in_set = set(names)

    def outline(name):
        pen = _SegmentPen(glyph_set)
        glyph_set[name].draw(pen)
        return Outline(pen.segments)

    def components(name):
        if glyf is None:
            return []
        g = glyf[name]
        return [c.glyphName for c in g.components] if g.isComposite() else []

    rows = [(name, cp, True) for name, cp in zip(names, lf.cps)]
    if cmap.get(0x20):
        rows.append((cmap[0x20], 0x20, True))
    marks = [cp for cp in range(0x0300, 0x0370) if cp in cmap and cmap[cp] not in in_set][:6]
    rows.extend((cmap[cp], cp, True) for cp in marks)
    for name, cp, export in rows:
        category, sub, case, script = glyph_properties(cp)
        glyph = MockGlyph(font, name, cp, export, category, sub, case, script)
        font.add_glyph(glyph)
        shape = outline(name)
        comps = components(name)
        # an imported composite is not auto-aligned; those whose other
        # components are marks (not spacing glyphs) are made so here
        aligned = bool(comps) and comps[0] in in_set and all(c not in in_set for c in comps[1:])
        for master in masters:
            glyph.layers.append(MockLayer(glyph, master, shape, hmtx[name][0], comps, aligned))
    if names and names[0] in font.glyphs._by_name:
        base = font.glyphs[names[0]]
        alt = MockGlyph(font, names[0] + ".alt", None, False, base.category, base.subCategory, base.case,
                        base.script)
        font.add_glyph(alt)
        for master in masters:
            layer = base.layers[master.id]
            alt.layers.append(MockLayer(alt, master, layer._outline, layer._width))

    # groups: families of composites with their base, then single glyphs
    for base, left, right in FAMILY_GROUPS:
        for name in names:
            comps = components(name)
            if name == base or (comps and comps[0] == base):
                g = font.glyphs[name]
                g.leftKerningGroup, g.rightKerningGroup = left, right
    for name, (left, right) in GLYPH_GROUPS.items():
        g = font.glyphs[name]
        if g is not None and name in in_set:
            g.leftKerningGroup, g.rightKerningGroup = left, right

    # metrics keys whose targets are in the spacing set, synced as Glyphs keeps them
    fixed_key = "=%d" % round(0.03 * lf.upm)
    keyed = []
    for name, keys in GLYPH_KEYS.items():
        g = font.glyphs[name]
        if g is None or name not in in_set:
            continue
        keys = tuple(fixed_key if k == "=FIXED" else k for k in keys)
        targets = [r[1] for r in (parse_key(k) for k in keys) if r is not None and r[0] == "follow" and r[1]]
        if any(t not in in_set for t in targets):
            continue
        g.leftMetricsKey, g.rightMetricsKey = keys
        keyed.append(g)
    for name, (left, right) in LAYER_KEYS.items():
        g = font.glyphs[name]
        target = (parse_key(left) or (None, None))[1]
        if g is not None and g in keyed and target in in_set:
            layer = g.layers[0]
            layer.leftMetricsKey, layer.rightMetricsKey = left, right
    for _round in range(2):
        for g in keyed:
            for layer in g.layers:
                layer.syncMetrics()

    for master, entries in zip(masters, EXISTING_KERNING):
        for left, right, value in entries:
            if all(k.startswith("@") or font.glyphs[k] is not None for k in (left, right)):
                font.setKerningForPair(master.id, left, right, value)
    font.log = []
    font.undo.registrations = 0
    return font, lf


def expected_rules(font, names, master_id, pinned=()):
    """{name: ((rule, target name, value), (rule, target name, value))} for
    the left and right side, as the snapshot should turn the mock's metrics
    keys and aligned composites into engine rules (value None: not checked).
    `pinned`: glyphs that keep their sidebearings (right-to-left, joining
    scripts, box drawing): both sides fixed at their current values."""
    out = {}
    for name in names:
        glyph = font.glyphs[name]
        layer = glyph.layers[master_id]
        if name in pinned:
            out[name] = ((kb.RULE_FIXED, None, layer.LSB), (kb.RULE_FIXED, None, layer.RSB))
            continue
        if layer.isAligned:
            base = layer.components[0].componentName
            bl = font.glyphs[base].layers[master_id]
            out[name] = ((kb.RULE_FOLLOW_SAME, base, layer.LSB - bl.LSB),
                         (kb.RULE_FOLLOW_SAME, base, layer.RSB - bl.RSB))
            continue
        sides = []
        for left, key in ((True, layer.leftMetricsKey or glyph.leftMetricsKey),
                          (False, layer.rightMetricsKey or glyph.rightMetricsKey)):
            rule = parse_key(key)
            if rule is None:
                sides.append((kb.RULE_FREE, None, None))
            elif rule[0] == "number":
                sides.append((kb.RULE_FIXED, None, layer.LSB if left else layer.RSB))
            else:
                sides.append((kb.RULE_FOLLOW_OPPOSITE if rule[2] else kb.RULE_FOLLOW_SAME, rule[1] or name, rule[3]))
        out[name] = tuple(sides)
    return out


# --------------------------------------------------------- instrumentation
class FakeAlert(object):
    """Stands in for NSAlert: answers at once with `answer`; `on_show` runs
    first (the moment between the window's plan and its writes)."""

    answer = NSAlertFirstButtonReturn
    on_show = None
    shown = []

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        self.message = self.info = ""
        self.titles = []
        return self

    def setMessageText_(self, text):
        self.message = str(text)

    def setInformativeText_(self, text):
        self.info = str(text)

    def addButtonWithTitle_(self, title):
        self.titles.append(str(title))

    def runModal(self):
        return self._answer()

    def beginSheetModalForWindow_completionHandler_(self, window, handler):
        handler(self._answer())

    def _answer(self):
        FakeAlert.shown.append(self)
        if FakeAlert.on_show is not None:
            FakeAlert.on_show(self)
        return FakeAlert.answer

    def __getattr__(self, name):
        if name.startswith("set"):
            return lambda *args: None
        raise AttributeError(name)


class TimerSpy(object):
    """Stands in for NSTimer in kk2_window: makes the real timers and keeps them."""

    def __init__(self):
        self.timers = []

    def _keep(self, timer):
        self.timers.append(timer)
        return timer

    def timerWithTimeInterval_target_selector_userInfo_repeats_(self, *args):
        return self._keep(NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(*args))

    def scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(self, *args):
        return self._keep(NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(*args))

    def timerWithTimeInterval_repeats_block_(self, *args):
        return self._keep(NSTimer.timerWithTimeInterval_repeats_block_(*args))

    def scheduledTimerWithTimeInterval_repeats_block_(self, *args):
        return self._keep(NSTimer.scheduledTimerWithTimeInterval_repeats_block_(*args))

    def __getattr__(self, name):
        return getattr(NSTimer, name)


class EngineWatch(object):
    """Wraps kk2_bridge so the test sees every engine job the window starts,
    every job it frees (and whether it was still running) and every Context
    and Result it takes. Jobs the test starts itself carry `kk2_test`."""

    def __init__(self):
        self.live = {}
        self.max_live = 0
        self.started = []  # (time, kind)
        self.cancelled = []  # (time, kind, phase, fraction)
        self.outputs = []  # weak references to every Context and Result taken
        self.raw_solve = kb.Engine.solve
        prepare, solve, free, take = kb.Engine.prepare, kb.Engine.solve, kb.Job.free, kb.Job.take
        watch = self

        def watched_prepare(engine, *args, **kwargs):
            return watch._started(prepare(engine, *args, **kwargs), "prepare")

        def watched_solve(engine, context, params, kern_mask=None):
            job = solve(engine, context, params, kern_mask)
            return watch._started(job, "whole" if kern_mask is None else "preview")

        def watched_free(job):
            try:
                if job.ptr and not getattr(job, "kk2_test", False):
                    watch._freed(job)
            except Exception:
                pass
            free(job)

        def watched_take(job):
            out = take(job)
            if not getattr(job, "kk2_test", False):
                watch.outputs.append(weakref.ref(out))
            return out

        kb.Engine.prepare, kb.Engine.solve = watched_prepare, watched_solve
        kb.Job.free, kb.Job.take = watched_free, watched_take

    def _started(self, job, kind):
        job.kk2_kind = kind
        self.live[id(job)] = kind
        self.max_live = max(self.max_live, len(self.live))
        self.started.append((time.time(), kind))
        return job

    def _freed(self, job):
        kind = self.live.pop(id(job), getattr(job, "kk2_kind", job.kind))
        state, phase, _phases, fraction, _elapsed = job.poll()
        if state == kb.STATE_RUNNING:
            self.cancelled.append((time.time(), kind, phase, fraction))

    def count(self, kind):
        return sum(1 for _t, k in self.started if k == kind)


class ReaderWatch(object):
    """Times every SnapshotReader.step() (one slice of reading outlines).
    A full garbage collection that happens to start inside a slice (tens of
    ms on a large mock font) is the interpreter's, not the slicing's: its
    time is taken out of the slice and kept apart (`gc_ms`)."""

    def __init__(self, ks):
        self.steps = 0
        self.max_ms = 0.0
        self.total_ms = 0.0
        self.gc_ms = 0.0
        self.budgets = set()
        self._gc_start = None
        self._gc_in_step = 0.0
        step = ks.SnapshotReader.step
        watch = self

        def on_gc(phase, info):
            if phase == "start":
                watch._gc_start = time.perf_counter()
            elif watch._gc_start is not None:
                watch._gc_in_step += time.perf_counter() - watch._gc_start
                watch._gc_start = None

        def watched_step(reader, budget_s=0.008):
            watch._gc_in_step = 0.0
            gc.callbacks.append(on_gc)
            t = time.perf_counter()
            try:
                done = step(reader, budget_s)
            finally:
                ms = 1000.0 * (time.perf_counter() - t)
                gc.callbacks.remove(on_gc)
            collected = 1000.0 * watch._gc_in_step
            watch.steps += 1
            watch.total_ms += ms
            watch.gc_ms += collected
            watch.max_ms = max(watch.max_ms, ms - collected)
            watch.budgets.add(budget_s)
            return done

        ks.SnapshotReader.step = watched_step


class Tee(object):
    """Passes output through and keeps every traceback printed on the way
    (the window prints those it catches in its timer)."""

    def __init__(self, stream):
        self.stream = stream
        self.tracebacks = []

    def write(self, text):
        if "Traceback (most recent call last)" in text:
            self.tracebacks.append(text)
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class KK2SmokeTicker(NSObject):
    """Target of the heartbeat timer."""

    def tick_(self, timer):
        callback = getattr(self, "kk2_callback", None)
        if callback is not None:
            try:
                callback()
            except Exception:
                traceback.print_exc()


class Heartbeat(object):
    """A 60 Hz timer in the common run-loop modes. The gap between two ticks
    is how long the main thread was busy; in judged stages the longest gap is
    kept per stage and per window state. stage() restarts the measurement, so
    the test's own synchronous work is never counted."""

    INTERVAL = 1.0 / 60.0

    def __init__(self, on_tick):
        self.on_tick = on_tick
        self.stage_name = "start"
        self.judged = False
        self.window_state = None
        self.stages = {}  # judged stage → (max gap ms, window state)
        self.by_state = {}  # window state → max gap ms
        self.last = time.perf_counter()
        self.timer = None
        self.ticker = KK2SmokeTicker.alloc().init()
        self.ticker.kk2_callback = self._tick

    def start(self):
        self.last = time.perf_counter()
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            self.INTERVAL, self.ticker, "tick:", None, True)
        self.timer.setTolerance_(0.0)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def stage(self, name, judged=False):
        self.stage_name, self.judged = name, judged
        self.last = time.perf_counter()

    def _tick(self):
        now = time.perf_counter()
        gap = 1000.0 * (now - self.last)
        self.last = now
        if self.judged:
            state = self.window_state or "opening"
            if gap > self.stages.get(self.stage_name, (0.0, None))[0]:
                self.stages[self.stage_name] = (gap, state)
            if gap > self.by_state.get(state, 0.0):
                self.by_state[state] = gap
        self.on_tick()

    def worst(self):
        """(gap ms, stage, window state) of the longest judged stall."""
        best = (0.0, None, None)
        for name, (gap, state) in self.stages.items():
            if gap > best[0]:
                best = (gap, name, state)
        return best

    def stop(self):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        self.ticker.kk2_callback = None


class Report(object):
    """PASS / FAIL / INFO lines, written past the Tee (a failure's traceback
    is not one the window printed)."""

    def __init__(self, stream):
        self.stream = stream
        self.passed = 0
        self.failed = []

    def check(self, ok, what, detail=""):
        self._line("%s  %s%s" % ("PASS" if ok else "FAIL", what, (" — " + detail) if detail else ""))
        if ok:
            self.passed += 1
        else:
            self.failed.append(what)
        return ok

    def info(self, text):
        self._line("INFO  " + text)

    def _line(self, text):
        self.stream.write(text + "\n")
        self.stream.flush()


# -------------------------------------------------------------- the window
def _views(root):
    stack = [root]
    while stack:
        view = stack.pop()
        yield view
        stack.extend(view.subviews() or ())


def _frame(view):
    """The view's frame in window coordinates (x, y, width, height)."""
    r = view.convertRect_toView_(view.bounds(), None)
    return r.origin.x, r.origin.y, r.size.width, r.size.height


class UI(object):
    """The window's controls, found by what they show (titles, ranges, items)
    rather than by attribute names, and operated the way AppKit operates
    them for a user: actions to their targets, text changes to delegates."""

    def __init__(self, nswindow):
        self.nswindow = nswindow
        self.views = list(_views(nswindow.contentView()))

    def slider(self, lo, hi):
        for v in self.views:
            if isinstance(v, NSSlider) and abs(v.minValue() - lo) < 1e-9 and abs(v.maxValue() - hi) < 1e-9:
                return v
        raise Abort("the window has no slider from %g to %g" % (lo, hi))

    def popup(self, test, what):
        for v in self.views:
            if isinstance(v, NSPopUpButton) and test([str(t) for t in v.itemTitles()]):
                return v
        raise Abort("the window has no %s pop-up" % what)

    def button(self, title):
        for v in self.views:
            if isinstance(v, NSButton) and not isinstance(v, NSPopUpButton) and str(v.title()) == title:
                return v
        raise Abort("the window has no %r button" % title)

    def labels(self):
        return [v for v in self.views if isinstance(v, NSTextField) and not v.isEditable()]

    def label(self, text):
        for v in self.labels():
            if text in str(v.stringValue() or ""):
                return v
        raise Abort("the window shows no label with %r" % text)

    def field_beside(self, anchor):
        """The editable text field on the same row as `anchor` (a view), the
        first one to its right."""
        ax, ay, aw, ah = _frame(anchor)
        best = None
        for v in self.views:
            if isinstance(v, NSTextField) and v.isEditable():
                x, y, w, h = _frame(v)
                if x < ax + aw / 2.0 or abs((y + h / 2.0) - (ay + ah / 2.0)) > max(h, ah) / 2.0 + 2.0:
                    continue
                if best is None or x < best[0]:
                    best = (x, v)
        if best is None:
            raise Abort("the window has no editable field beside %r" % str(getattr(anchor, "stringValue",
                                                                                    lambda: anchor)()))
        return best[1]

    def editor(self):
        for v in self.views:
            if isinstance(v, NSTextView) and v.isEditable():
                return v
        raise Abort("the window has no editable sample text")

    def panes(self):
        """(left, right) proof text views."""
        views = sorted((v for v in self.views if isinstance(v, NSTextView) and not v.isEditable()),
                       key=lambda v: _frame(v)[0])
        if len(views) < 2:
            raise Abort("the window has %d proof panes, expected 2" % len(views))
        return views[0], views[-1]

    def progress_bar(self):
        for v in self.views:
            if isinstance(v, NSProgressIndicator) and not v.isIndeterminate():
                return v
        return None

    # the controls the contract names
    def tightness(self):
        return self.slider(-1.0, 1.0)

    def intensity(self):
        return self.slider(0.0, 200.0)

    def threshold(self):
        return self.slider(0.0, 20.0)

    def threshold_field(self):
        return self.field_beside(self.threshold())

    def max_pairs_field(self):
        return self.field_beside(self.label("Max pairs"))

    def scope(self):
        return self.popup(lambda items: "Whole font" in items, "scope")

    def size(self):
        return self.popup(lambda items: items and all(t.isdigit() for t in items), "size")

    def threads(self):
        return self.popup(lambda items: items and items[0].startswith("Auto"), "threads")

    def master(self, names):
        return self.popup(lambda items: items == list(names), "master")


def set_slider(slider, value):
    slider.setDoubleValue_(value)
    slider.sendAction_to_(slider.action(), slider.target())


def choose(popup, title):
    index = popup.indexOfItemWithTitle_(title)
    if index < 0:
        raise Abort("the pop-up has no item %r" % title)
    popup.selectItemAtIndex_(index)
    popup.sendAction_to_(popup.action(), popup.target())


def type_into(field, text):
    """As typing: the field's text changes and its delegate hears of it."""
    field.setStringValue_(text)
    delegate = field.delegate()
    note = NSNotification.notificationWithName_object_(NSControlTextDidChangeNotification, field)
    if delegate is not None and delegate.respondsToSelector_("controlTextDidChange:"):
        delegate.controlTextDidChange_(note)
    elif field.action() is not None:
        field.sendAction_to_(field.action(), field.target())


def edit_text(view, text):
    view.setString_(text)
    view.didChangeText()


def pane_cells(view):
    """Width in points of every glyph cell of a proof pane (None for spaces
    and line breaks, which are characters, not cells)."""
    storage = view.textStorage()
    out = []
    for i in range(storage.length()):
        att = storage.attribute_atIndex_effectiveRange_(NSAttachmentAttributeName, i, None)
        if isinstance(att, tuple):
            att = att[0]
        cell = att.attachmentCell() if att is not None else None
        out.append(float(cell.cellSize().width) if cell is not None else None)
    return out


def open_windows(kw, cls=None):
    """kk2_window's open windows: the window objects (of `cls`, or anything
    with a `w` and a `state`) in its module-level lists and dicts."""
    found = []
    for value in list(vars(kw).values()):
        if isinstance(value, dict):
            items = list(value.values())
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            continue
        for x in items:
            if isinstance(x, weakref.ref):
                x = x()
            if cls is not None:
                ok = isinstance(x, cls)
            else:
                ok = hasattr(x, "w") and hasattr(x, "state")
            if ok and not any(x is y for y in found):
                found.append(x)
    return found


def freed(obj):
    """True when an engine object (Context, Result, Job) is gone or closed."""
    return obj is None or not getattr(obj, "ptr", None)


def spin(seconds, mode=None):
    date = NSDate.dateWithTimeIntervalSinceNow_(seconds)
    if mode is None:
        NSRunLoop.currentRunLoop().runUntilDate_(date)
    else:
        NSRunLoop.currentRunLoop().runMode_beforeDate_(mode, date)


def write_png(nswindow, path):
    view = nswindow.contentView()
    rect = view.bounds()
    rep = view.bitmapImageRepForCachingDisplayInRect_(rect)
    view.cacheDisplayInRect_toBitmapImageRep_(rect, rep)
    rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None).writeToFile_atomically_(path, True)


def patch_vanilla():
    """The window as kk2_window builds it, but invisible, ignoring the mouse and
    without a frame autosave name (nothing goes into this Python's defaults)."""
    import vanilla
    window_open = vanilla.Window.open

    def offscreen_open(self):
        nswindow = self.getNSWindow()
        nswindow.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua"))
        nswindow.setAlphaValue_(0.0)
        nswindow.setIgnoresMouseEvents_(True)
        window_open(self)

    def without_autosave(init):
        def __init__(self, *args, **kwargs):
            kwargs.pop("autosaveName", None)
            init(self, *args, **kwargs)
        return __init__

    # vanilla's windows are Objective-C classes: patched in place, not subclassed
    vanilla.Window.open = offscreen_open
    vanilla.Window.__init__ = without_autosave(vanilla.Window.__init__)
    vanilla.FloatingWindow.__init__ = without_autosave(vanilla.FloatingWindow.__init__)


# ----------------------------------------------------------------- the test
class SmokeTest(object):

    def __init__(self, args):
        self.args = args
        self.report = Report(sys.stdout)
        self.t0 = time.time()
        self.win = None
        self.first = None
        self.ui = None
        self.bar = None
        self.labels = []
        self.states = []
        self.last_state = None
        self.progress_texts = []
        self.max_bar = 0.0
        self.modal_since = None
        self.callbacks = []  # (what, ms)
        self.written = False  # an Apply (and Revert) changed the mock font since the window read it
        self.heartbeat = Heartbeat(self.on_tick)

    # --- plumbing -----------------------------------------------------------
    def note_state(self):
        state = getattr(self.win, "state", None)
        self.heartbeat.window_state = state
        if state != self.last_state:
            self.states.append((round(time.time() - self.t0, 2), state))
            self.last_state = state

    def on_tick(self):
        win = self.win
        if win is not None and getattr(win, "w", None) is not None:
            self.note_state()
            for field in self.labels:
                text = str(field.stringValue() or "")
                if (PHASE_TEXT.search(text) or READING_TEXT.search(text)) and (
                        not self.progress_texts or self.progress_texts[-1] != text):
                    self.progress_texts.append(text)
            if self.bar is not None:
                self.max_bar = max(self.max_bar, float(self.bar.doubleValue()))
        modal = NSApp().modalWindow()
        if modal is None:
            self.modal_since = None
        elif self.modal_since is None:
            self.modal_since = time.time()
        elif time.time() - self.modal_since > 1.0:
            self.report.check(False, "no modal window but the confirmation alert",
                              "aborted %r" % str(modal.title()))
            NSApp().abortModal()
            self.modal_since = None

    def act(self, what, fn, *args, **kwargs):
        """Operates a control and times the callback it runs (`judged=False`:
        reported, not held to --max-stall)."""
        self.heartbeat.stage("callback")
        t = time.perf_counter()
        out = fn(*args)
        ms = 1000.0 * (time.perf_counter() - t)
        if kwargs.get("judged", True):
            self.callbacks.append((what, ms))
        else:
            self.report.info("%s: %.0f ms on the main thread" % (what, ms))
        self.heartbeat.stage("test")
        return out

    def idle(self, seconds, stage):
        """Lets the window work for a while (a judged stage)."""
        self.heartbeat.stage(stage, judged=True)
        spin(seconds)
        self.heartbeat.stage("test")

    def wait_for(self, what, condition, timeout, stage=None, mode=None):
        """Runs the run loop until condition() holds. Returns the seconds it took."""
        if stage is not None:
            self.heartbeat.stage(stage, judged=True)
        t0 = time.time()
        try:
            while True:
                if condition():
                    return time.time() - t0
                win = self.win
                if win is not None and getattr(win, "state", None) == "error":
                    raise Abort("the window reports an error while waiting for %s: %s" % (what, self.window_text()))
                if time.time() - t0 > timeout:
                    raise Abort("timed out after %.0f s waiting for %s (window state %r: %s)"
                                % (timeout, what, getattr(win, "state", None), self.window_text()))
                spin(0.02, mode)
        finally:
            self.heartbeat.stage("test")

    def window_text(self):
        try:
            return " | ".join(t for t in (str(v.stringValue() or "").strip() for v in self.labels) if t)[-500:]
        except Exception:
            return "-"

    def current_progress(self):
        """(phase, percent) the window shows; (0, percent) while reading; (None, None)."""
        for field in self.labels:
            text = str(field.stringValue() or "")
            m = PHASE_TEXT.search(text)
            if m:
                return int(m.group(1)), int(m.group(4))
            m = READING_TEXT.search(text)
            if m:
                return 0, int(m.group(1))
        return None, None

    def ready(self, previous=None):
        win = self.win
        return (win.state == "ready" and win.result is not None and win.result is not previous
                and not self.engines.live and not win.panes_due)

    # --- the run ------------------------------------------------------------
    def run(self):
        args = self.args
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        install_glyphsapp()
        t = time.time()
        self.font, self.lf = build_mock_font(args.font, args.glyphs)
        # building the mock allocates a lot: collect now, so the next full
        # collection does not land in (and get charged to) a slice of reading
        gc.collect()
        MockGlyphs.font = self.font
        MockGlyphs.fonts = [self.font]
        self.report.info("mock font %s: %d glyphs (%d spacing), %d masters, upm %d, built in %.1f s"
                         % (self.font.familyName, len(self.font.glyphs), len(self.lf.names),
                            len(self.font.masters), self.font.upm, time.time() - t))

        AppKit.NSAlert = FakeAlert  # `from AppKit import NSAlert` in the window gets the fake
        patch_vanilla()
        import kk2_apply
        import kk2_snapshot
        import kk2_window
        self.ka, self.ks, self.kw = kk2_apply, kk2_snapshot, kk2_window
        if hasattr(kk2_window, "NSAlert"):
            kk2_window.NSAlert = FakeAlert
        self.timer_spy = None
        if hasattr(kk2_window, "NSTimer"):
            self.timer_spy = kk2_window.NSTimer = TimerSpy()
        self.cpu_count = kb.Engine(os.path.join(RESOURCES, kb.DYLIB_NAME)).cpu_count
        self.engines = EngineWatch()
        self.reader = ReaderWatch(kk2_snapshot)
        self.out, self.err = Tee(sys.stdout), Tee(sys.stderr)
        sys.stdout, sys.stderr = self.out, self.err
        self.heartbeat.start()
        try:
            self.open_window()
            self.check_snapshot()
            self.change_settings()
            self.apply_sample()
            self.whole_font()
            self.close_window()
            self.second_window()
        except Abort as e:
            self.report.check(False, "the test ran to the end", str(e))
        except Exception:
            self.report.check(False, "the test ran to the end", traceback.format_exc())
        finally:
            self.cleanup()
        self.final_checks()
        sys.stdout, sys.stderr = self.out.stream, self.err.stream
        print("\n%d passed, %d failed in %.1f s" % (self.report.passed, len(self.report.failed),
                                                    time.time() - self.t0))
        for what in self.report.failed:
            print("  FAILED: " + what)
        return 1 if self.report.failed else 0

    # 1. reading, Phase 1, first preview
    def open_window(self):
        rep, kw = self.report, self.kw
        self.heartbeat.stage("open")
        t = time.perf_counter()
        win = kw.open_window(RESOURCES)
        open_ms = 1000.0 * (time.perf_counter() - t)
        self.win = win
        if not rep.check(win is not None and getattr(win, "w", None) is not None, "the window opens",
                         "%.0f ms" % open_ms):
            raise Abort("no window: %r" % MockGlyphs.messages)
        nswindow = win.w.getNSWindow()
        self.ui = UI(nswindow)
        self.labels = self.ui.labels()
        self.bar = self.ui.progress_bar()
        title = str(nswindow.title())
        rep.check(title == "Kinetikern2 — %s" % self.font.familyName, "window title", repr(title))
        again = kw.open_window(RESOURCES)
        windows = open_windows(kw, type(win))
        rep.check(again is win and len(windows) == 1, "choosing the menu again brings back the same window",
                  "%d window(s) listed" % len(windows))
        items = [str(t) for t in self.ui.threads().itemTitles()]
        m = re.match(r"Auto \((\d+) of (\d+) cores\)", items[0])
        rep.check(bool(m) and int(m.group(2)) == self.cpu_count and items[1:] == [
            str(n) for n in range(1, self.cpu_count + 1)], "threads pop-up offers Auto (N of M cores) and 1…M",
            "%s … %s" % (items[0], items[-1]))
        rep.check(self.bar is not None, "the window has a determinate progress bar")

        # reading, Phase 1 and the first preview with the run loop in the
        # event-tracking mode, as it is while a slider is dragged
        tracking = True
        try:
            secs = self.wait_for("the first preview (run loop in event-tracking mode)", self.ready, 60.0,
                                 stage="reading, Phase 1, first preview", mode=NSEventTrackingRunLoopMode)
        except Abort as e:
            tracking = False
            rep.info(str(e))
            secs = self.wait_for("the first preview", self.ready, self.args.timeout,
                                 stage="reading, Phase 1, first preview")
        rep.check(tracking, "the window's timer runs in the event-tracking run-loop mode (slider drags)",
                  "first preview after %.2f s" % secs)
        snap, res = win.snapshot, win.result
        rw = self.reader
        budget = max(rw.budgets or {0.0})
        rep.check(rw.steps >= 2 and rw.max_ms <= self.args.slice_ms and budget <= 0.0081,
                  "outlines are read in slices",
                  "%d slices of at most %.0f ms, longest %.1f ms (garbage collection taken out: %.0f ms), "
                  "%.0f ms in all (snapshot read_ms %.0f)"
                  % (rw.steps, 1000.0 * budget, rw.max_ms, rw.gc_ms, rw.total_ms, snap.read_ms))
        reading = [t for t in self.progress_texts if READING_TEXT.search(t)]
        rep.check(bool(reading), "progress shows \"Reading outlines [N%]\" while reading",
                  "%d distinct: %s" % (len(reading), ", ".join(repr(t) for t in reading[:3])))
        self.note_state()
        seen = [s for _t, s in self.states]
        rep.check(seen[:1] == ["reading"] and "preparing" in seen and seen[-1] == "ready",
                  "states: reading → preparing → previewing → ready", " → ".join(str(s) for s in seen))
        rep.check(self.engines.count("prepare") == 1 and self.engines.count("preview") >= 1,
                  "one Phase 1 job, then a preview job",
                  "%d prepare, %d preview" % (self.engines.count("prepare"), self.engines.count("preview")))
        mask = bytes(res.kern_mask)
        kerned = len(mask) - mask.count(0)
        rep.info("first preview: %d spacing glyphs, %d of the sample kerned, %d entries (%d class pairs, "
                 "%d exceptions), pass 1 %.0f ms, pass 2 %.0f ms, Phase 1 %.0f ms"
                 % (len(snap.names), kerned, res.entry_count, res.stats["class_entries"],
                    res.stats["exception_entries"], res.stats["pass1_ms"], res.stats["pass2_ms"],
                    win.context.prep_ms))
        left, right = self.ui.panes()
        n_left, n_right = left.textStorage().length(), right.textStorage().length()
        rep.check(n_left > 50 and n_left == n_right, "both proof panes show the sample text",
                  "%d / %d characters" % (n_left, n_right))
        values = {}
        for pair in CLASSIC:
            a, b = (snap.index.get(snap.char_map.get(c)) for c in pair)
            if a is not None and b is not None:
                values[pair] = res.value(a, b)
        negative = [p for p in ("AV", "To") if values.get(p, 0.0) < 0.0]
        rep.check(len(negative) == 2, "the preview kerns AV and To tighter",
                  ", ".join("%s %.1f" % (p, v) for p, v in values.items()))
        cells_l, cells_r = pane_cells(left), pane_cells(right)
        moved = sum(1 for a, b in zip(cells_l, cells_r) if a is not None and b is not None and abs(a - b) > 0.01)
        rep.check(moved > 0, "the right pane shows the new spacing", "%d of %d glyph cells differ from the left"
                  % (moved, sum(1 for a in cells_l if a is not None)))

    # 2. the engine input
    def check_snapshot(self):
        rep, font, lf = self.report, self.font, self.lf
        snap = self.win.snapshot
        mid = snap.master_id
        rep.check(list(snap.names) == list(lf.names), "spacing set = exporting letters, figures, punctuation, "
                  "symbols (no space, marks or non-exporting glyphs)",
                  "%d glyphs, %d expected; %d skipped" % (len(snap.names), len(lf.names), snap.glyph_count_skipped))
        if list(snap.names) != list(lf.names):
            return
        expected = lf.specs()
        # glyphs whose ink meets the advance edges keep their sidebearings
        pinned = set()
        for i, name in enumerate(snap.names):
            cp = lf.cps[i]
            script = fud.script(chr(cp)) if cp is not None else None
            if font.glyphs[name].layers[mid]._outline.ink and (
                    expected[i].flags & kb.GLYPH_RTL or script in self.ks.JOINING_SCRIPTS or
                    (cp is not None and 0x2500 <= cp <= 0x259F)):
                pinned.add(name)
        rep.check(set(getattr(snap, "pinned", ())) == pinned, "snapshot: right-to-left, joining and box-drawing "
                  "glyphs keep their sidebearings", "%d pinned, %d expected" % (len(getattr(snap, "pinned", ())),
                                                                                len(pinned)))
        rules = expected_rules(font, snap.names, mid, pinned)
        follow = (kb.RULE_FOLLOW_SAME, kb.RULE_FOLLOW_OPPOSITE)
        bad = {}

        def wrong(field, name, got, want):
            bad.setdefault(field, []).append("%s: %r ≠ %r" % (name, got, want))

        figures = kb.GROUP_FIGURES
        for i, name in enumerate(snap.names):
            spec, want = snap.specs[i], expected[i]
            glyph = font.glyphs[name]
            layer = glyph.layers[mid]
            if abs(spec.advance - layer.width) > 1e-6:
                wrong("advance", name, spec.advance, layer.width)
            if spec.base != want.base:
                wrong("composite base", name, spec.base, want.base)
            if spec.script != want.script:
                wrong("script", name, spec.script, want.script)
            flags = kb.GLYPH_KERN | kb.GLYPH_RTL | kb.GLYPH_FIXED_ADVANCE
            if spec.flags & flags != want.flags & flags:
                wrong("flags", name, spec.flags & flags, want.flags & flags)
            keys = ((kb.GLYPH_LEFT_KEY if glyph.leftKerningGroup == name else 0) |
                    (kb.GLYPH_RIGHT_KEY if glyph.rightKerningGroup == name else 0))
            if spec.flags & (kb.GLYPH_LEFT_KEY | kb.GLYPH_RIGHT_KEY) != keys:
                wrong("group key flags", name, spec.flags & 24, keys)
            group = (figures if glyph.category == "Number" else
                     kb.GROUP_UPPERCASE if glyph.category == "Letter" and glyph.case == GSUppercase else
                     kb.GROUP_LOWERCASE if glyph.category == "Letter" and glyph.case == GSLowercase else
                     kb.GROUP_OTHER)
            if spec.group != group:
                wrong("rhythm group", name, spec.group, group)
            for gid, names, have in ((spec.left_group, snap.left_group_names, glyph.leftKerningGroup),
                                     (spec.right_group, snap.right_group_names, glyph.rightKerningGroup)):
                got = names[gid] if gid != kb.NONE else None
                if got != have:
                    wrong("kerning group", name, got, have)
            if abs(spec.cur_lsb - layer.LSB) > 1e-6 or abs(spec.cur_rsb - layer.RSB) > 1e-6:
                wrong("current sidebearings", name, (spec.cur_lsb, spec.cur_rsb), (layer.LSB, layer.RSB))
            for side, (rule, target, value) in zip(("lsb", "rsb"), rules[name]):
                got_rule = getattr(spec, side + "_rule")
                got_glyph = getattr(spec, side + "_glyph")
                got_value = getattr(spec, side + "_value")
                ok = got_rule == rule
                if ok and rule in follow:
                    ok = got_glyph == snap.index.get(target, kb.NONE)
                if ok and value is not None and rule != kb.RULE_FREE:
                    ok = abs(got_value - value) < 1e-6
                if not ok:
                    wrong("metric rules", name + " " + side,
                          (got_rule, snap.names[got_glyph] if got_glyph < len(snap.names) else None, got_value),
                          (rule, target, value))
        aligned = sum(1 for n in snap.names if font.glyphs[n].layers[mid].isAligned)
        keyed = sum(1 for n in snap.names if any(r[0] != kb.RULE_FREE for r in rules[n])) - aligned
        for field in ("advance", "composite base", "script", "flags", "group key flags", "rhythm group",
                      "kerning group", "current sidebearings", "metric rules"):
            rows = bad.get(field, [])
            rep.check(not rows, "snapshot: %s of every glyph" % field,
                      ("%d wrong, e.g. %s" % (len(rows), "; ".join(rows[:4]))) if rows else
                      ("%d aligned composites, %d keyed glyphs" % (aligned, keyed) if field == "metric rules" else ""))
        fixed = set(s.name for s in expected if s.flags & kb.GLYPH_FIXED_ADVANCE)
        rep.check(set(snap.fixed) == fixed, "snapshot: tabular figures keep their advance",
                  "%d fixed: %s" % (len(snap.fixed), " ".join(sorted(snap.fixed)[:10])))

    # 3. settings while a slow preview runs
    def change_settings(self):
        rep, ui, win = self.report, self.ui, self.win
        snap = win.snapshot
        chars = [snap.infos[n].char for n in snap.names if snap.infos[n].char]
        slow = "\n".join("".join(chars[k:k + 40]) for k in range(0, len(chars), 40))
        before = win.result
        self.act("sample text: every glyph", edit_text, ui.editor(), slow)
        t = time.time()
        self.wait_for("the all-glyph preview", lambda: self.ready(before), self.args.timeout, stage="slow preview")
        slow_s = time.time() - t
        res = win.result
        rep.info("all-glyph preview: %.2f s, %d glyphs kerned, %d pairs in scope, %d entries"
                 % (slow_s, res.stats["kern_glyphs"], res.stats["pairs_in_scope"], res.entry_count))

        cancelled0 = len(self.engines.cancelled)
        started0 = self.engines.count("preview")
        before = win.result
        self.act("tightness slider", set_slider, ui.tightness(), -0.5)
        self.idle(0.12, "preview after settings changes")
        self.act("intensity slider", set_slider, ui.intensity(), 140.0)
        self.idle(0.12, "preview after settings changes")
        self.act("threshold field", type_into, ui.threshold_field(), "3")
        self.wait_for("the preview with the new settings", lambda: self.ready(before), self.args.timeout,
                      stage="preview after settings changes")
        cancelled = [c for c in self.engines.cancelled[cancelled0:] if c[1] == "preview"]
        started = self.engines.count("preview") - started0
        if slow_s > 0.35:
            rep.check(bool(cancelled), "a settings change cancels the running preview",
                      "%d previews started, %d cancelled while running (at %s)"
                      % (started, len(cancelled), ", ".join("phase %d %.0f%%" % (c[2], 100 * c[3])
                                                            for c in cancelled)))
        else:
            rep.info("the all-glyph preview took only %.2f s: the window may let a running preview finish, so "
                     "cancelling is not tested here (%d started, %d cancelled)" % (slow_s, started, len(cancelled)))
        rep.check(self.engines.max_live <= 1, "at most one engine job at a time",
                  "at most %d at once" % self.engines.max_live)
        rep.check(abs(ui.threshold().doubleValue() - 3.0) < 1e-9, "the threshold field moves the slider",
                  "%g" % ui.threshold().doubleValue())
        mask = self.sample_mask(snap)
        self.compare_direct(win.result, mask, 0, "the preview")
        defaults = MockGlyphs.defaults
        saved = dict((k, defaults[PREFIX + k]) for k in ("tightness", "intensity", "threshold", "sample"))
        want = {"tightness": -0.5, "intensity": 140.0, "threshold": 3.0}

        def same(value, number):
            try:
                return abs(float(value) - number) < 1e-9
            except (TypeError, ValueError):
                return False

        rep.check(all(same(saved[k], v) for k, v in want.items()) and saved["sample"] == slow,
                  "sliders, threshold and sample text are saved in Glyphs.defaults",
                  ", ".join("%s=%r" % (k, v if k != "sample" else "%d chars" % len(v or ""))
                            for k, v in saved.items()))

    def sample_mask(self, snap):
        mask = bytearray(len(snap.names))
        for t in self.ks.tokenize(str(self.ui.editor().string()), snap):
            i = snap.index.get(t) if t is not None else None
            if i is not None:
                mask[i] = 1
        return bytes(mask)

    def compare_direct(self, res, mask, budget, what):
        """The window's result against a solve the test starts itself on the
        window's context, with parameters made from what the controls show."""
        ui, ctx, snap = self.ui, self.win.context, self.win.snapshot
        t = float(ui.tightness().doubleValue())
        coupling = float(ui.intensity().doubleValue()) / 100.0
        threshold = float(ui.threshold().doubleValue()) * snap.upm / 1000.0
        params = kb.make_params(spring=math.exp(-0.55 * t), repulsion=3.86 * math.exp(0.55 * t), coupling=coupling,
                                classes=True, window=True, scope_scripts=True, threshold=threshold, budget=budget,
                                threads=0)
        self.heartbeat.stage("test")
        job = self.engines.raw_solve(ctx.engine, ctx, params, mask)
        job.kk2_test = True
        state = job.wait(600.0)
        if state != kb.STATE_DONE:
            self.report.check(False, "%s equals a direct engine solve" % what, "direct solve state %d: %s"
                              % (state, job.error()))
            job.free()
            return
        ref = job.take()
        job.free()
        try:
            n = res.glyph_count
            kerned = [i for i in range(n) if res.kern_mask[i]]
            same_mask = bytes(res.kern_mask) == bytes(ref.kern_mask)
            lefts = [a for a in kerned for _b in kerned]
            rights = [b for _a in kerned for b in kerned]
            va, vb = res.values(lefts, rights), ref.values(lefts, rights)
            worst = max([abs(x - y) for x, y in zip(va, vb) if x == x and y == y] or [0.0])
            nan = sum(1 for x, y in zip(va, vb) if (x != x) != (y != y))
            metrics = max([abs(res.metrics[i].lsb - ref.metrics[i].lsb) + abs(res.metrics[i].rsb - ref.metrics[i].rsb)
                           for i in range(n) if res.metrics[i].valid and ref.metrics[i].valid] or [0.0])
            self.report.check(same_mask and res.entry_count == ref.entry_count and worst < 1e-3 and not nan
                              and metrics < 1e-3,
                              "%s equals a direct engine solve with the settings the controls show" % what,
                              "tightness %.2f, intensity %.0f %%, threshold %.2f units, budget %d: %d vs %d entries, "
                              "%d pairs, largest difference %.4f, sidebearings %.4f"
                              % (t, 100 * coupling, threshold, budget, res.entry_count, ref.entry_count, len(va),
                                 worst, metrics))
        finally:
            ref.close()

    # 4. Apply to Font for the sample text
    def apply_sample(self):
        rep, ui, win, font = self.report, self.ui, self.win, self.font
        self.act("scope pop-up", choose, ui.scope(), "Glyphs in sample text")
        self.act("sample text", edit_text, ui.editor(), SAMPLE)
        self.idle(0.2, "sample preview")
        # a physics change asks for a new preview whatever the last one kerned
        before = win.result
        self.act("intensity slider", set_slider, ui.intensity(), 150.0)
        self.wait_for("the sample preview", lambda: self.ready(before), self.args.timeout, stage="sample preview")
        want, got = set(self.mask_indices()), self.kerned(win.result)
        rep.check(got == want, "the preview kerns exactly the glyphs of the sample text",
                  "%d kerned, %d kernable glyphs in the sample" % (len(got), len(want)))
        self.act("size pop-up", choose, ui.size(), "72")

        # Cancel in the confirmation alert writes nothing
        state0 = font.state()
        font.log = []
        shown = len(FakeAlert.shown)
        FakeAlert.answer, FakeAlert.on_show = NSAlertSecondButtonReturn, None
        self.act("Apply to Font (answered Cancel)", ui.button("Apply to Font").performClick_, None)
        self.wait_for("the window after Cancel", lambda: win.state == "ready" and not self.engines.live
                      and not win.panes_due, 60.0, stage="apply")
        rep.check(len(FakeAlert.shown) == shown + 1 and not font.writes() and not diff_state(state0, font.state()),
                  "Apply to Font asks first; Cancel writes nothing",
                  "%d alert(s), %d writes" % (len(FakeAlert.shown) - shown, len(font.writes())))
        self.apply_and_verify(whole=False)

    def kerned(self, res):
        return set(i for i in range(res.glyph_count) if res.kern_mask[i])

    def mask_indices(self):
        mask = self.sample_mask(self.win.snapshot)
        return [i for i, m in enumerate(mask) if m and self.win.snapshot.specs[i].flags & kb.GLYPH_KERN]

    def apply_and_verify(self, whole):
        """Apply to Font (confirmed in the alert), the checks, Revert Last Apply.
        For the whole font, a first run is cancelled at ~30 % (its alert, if
        it finishes first, is answered Cancel)."""
        rep, ui, win, font = self.report, self.ui, self.win, self.font
        label = "whole font" if whole else "sample text"
        apply_button = ui.button("Apply to Font")
        if whole:
            FakeAlert.answer, FakeAlert.on_show = NSAlertSecondButtonReturn, None
            cancelled0 = len(self.engines.cancelled)
            prepared = self.engines.count("prepare")
            t = time.time()
            self.act("Apply to Font (whole font, to be cancelled)", apply_button.performClick_, None)
            if self.written:
                # the sample Apply and its Revert changed the master since it was read
                rep.check(win.state == "reading", "after an Apply, Apply to Font reads the master again first",
                          win.state)
                self.wait_for("the whole-font run after reading the master again",
                              lambda: win.state not in ("reading", "preparing", "previewing"), self.args.timeout,
                              stage="reading again before Apply")
                rep.check(self.engines.count("prepare") == prepared + 1, "… and runs Phase 1 on it again",
                          "%d Phase 1 jobs" % (self.engines.count("prepare") - prepared))
            rep.check(win.state == "solving", "Apply to Font (whole font) starts a whole-font run", win.state)
            self.check_disabled()
            self.cancel_whole_run(t, cancelled0)

        before = font.state()
        left_before = pane_cells(ui.panes()[0])
        font.log = []
        held = {}
        replace = bool(ui.button("Replace existing kerning").state())

        def on_show(alert):
            # between the window's plan and its first write: the same plan, for the read-back
            held["alert"] = alert
            held["result"] = win.result
            held["plan"] = self.ka.plan(win.snapshot, win.result, replace, metrics_names=win.plan_names(win.result))
            held["writes_before"] = len(font.writes())
            held["t"] = time.perf_counter()
            held["applying"] = []
            self.heartbeat.stage("apply", judged=True)  # the writes, from here (the test's plan is not counted)

        FakeAlert.answer, FakeAlert.on_show = NSAlertFirstButtonReturn, on_show
        previous = win.last_apply
        progress0 = len(self.progress_texts)
        self.max_bar = 0.0
        t = time.time()
        self.act("Apply to Font (%s)" % label, apply_button.performClick_, None)

        def applied():
            if "applying" in held and win.state == "applying":
                held["applying"].append(self.window_text())
            return win.last_apply is not previous and win.state not in ("applying", "solving") and not win.panes_due

        self.wait_for("Apply to Font (%s)" % label, applied, self.args.timeout,
                      stage="whole-font run" if whole else "apply")
        FakeAlert.on_show = None
        seconds = time.time() - t
        if "plan" not in held:
            raise Abort("Apply to Font (%s) applied without asking first" % label)
        plan, res, snap = held["plan"], held["result"], win.snapshot
        rep.info("Apply (%s) wrote %d kerning entries, %d group sides, %d sidebearing glyphs in at most %.0f ms "
                 "(from the alert's answer to the summary, one 20 ms poll included)"
                 % (label, len(plan.kerning), sum(1 for s in plan.groups_to_set.values() for g in s if g),
                    len(plan.metrics), 1000.0 * (time.perf_counter() - held["t"])))
        rep.check(held["writes_before"] == 0, "nothing is written before the alert is answered",
                  "%d writes" % held["writes_before"])
        texts = [t for t in held["applying"] if "Applying" in t]
        rep.info("Apply wrote over %d timer ticks of the window, e.g. %s"
                 % (len(held["applying"]), texts[-1][:120] if texts else "-"))
        ms = win.last_apply.get("ms") or {}
        rep.info("plan() %.1f ms for %d result entries; apply() %.1f ms (%s)"
                 % (plan.ms, res.entry_count, ms.get("total", float("nan")),
                    ", ".join("%s %.1f" % (k, v) for k, v in ms.items() if k != "total")))
        rep.info("alert: %s / %s" % (held["alert"].message, held["alert"].info.replace("\n", " ")[:300]))
        if whole:
            st = res.stats
            phases = [t for t in self.progress_texts[progress0:] if PHASE_TEXT.search(t)]
            names = set(kb.PHASE_NAMES.values())
            good = [t for t in phases if PHASE_TEXT.search(t).group(3).strip() in names]
            rep.check(bool(good) or seconds < 0.5, "progress shows \"Phase k/3: <phase> [N%]\" during the run",
                      "%d texts, e.g. %s; bar up to %.0f" % (len(phases), ", ".join(repr(x) for x in good[-2:]),
                                                             self.max_bar))
            budget = self.args.max_pairs
            rep.check(res.entry_count <= budget, "the whole-font run keeps to Max pairs",
                      "%d entries of at most %d (%d before the budget, %d dropped)"
                      % (res.entry_count, budget, st["entries_before_budget"], st["dropped_by_budget"]))
            rep.info("whole-font run + apply %.1f s: %d kerned glyphs, %d pairs in scope, %d class pairs solved, "
                     "%d entries (%d class pairs, %d exceptions), %d threads"
                     % (seconds, st["kern_glyphs"], st["pairs_in_scope"], st["class_pairs"], res.entry_count,
                        st["class_entries"], st["exception_entries"], st["threads"]))
        self.verify_apply(plan, before, res, snap, whole)
        if whole:
            self.compare_direct(res, None, self.args.max_pairs, "the whole-font result")
        self.revert(before, left_before, label)
        self.written = True

    def check_disabled(self):
        ui = self.ui
        controls = (("tightness", ui.tightness()), ("intensity", ui.intensity()), ("threshold", ui.threshold()),
                    ("threshold field", ui.threshold_field()), ("max pairs", ui.max_pairs_field()),
                    ("master", ui.master([m.name for m in self.font.masters])))
        enabled = [name for name, c in controls if c.isEnabled()]
        cancel = ui.button("Cancel").isEnabled()
        self.report.check(not enabled and cancel, "a whole-font run disables the controls that would restart it",
                          "still enabled: %s; Cancel %s" % (", ".join(enabled) or "none",
                                                           "enabled" if cancel else "disabled"))

    def cancel_whole_run(self, started, cancelled0):
        rep, win, font = self.report, self.win, self.font
        result_before = win.result

        def at_30():
            if win.state != "solving":
                return True
            phase, percent = self.current_progress()
            return phase is not None and (phase >= 3 or (phase == 2 and percent >= 30))

        self.wait_for("a whole-font run at 30 %", at_30, self.args.timeout, stage="whole-font run (to be cancelled)")
        if win.state != "solving":
            rep.info("the whole-font run ended before it could be cancelled (%.1f s)" % (time.time() - started))
            self.wait_for("the window after the run", lambda: win.state == "ready", 30.0)
            return
        phase, percent = self.current_progress()
        writes = len(font.writes())
        shown = len(FakeAlert.shown)
        t = time.perf_counter()
        self.act("Cancel", self.ui.button("Cancel").performClick_, None)
        ms = 1000.0 * (time.perf_counter() - t)
        stopped = win.state != "solving"
        spin(0.5)
        cancelled = [c for c in self.engines.cancelled[cancelled0:] if c[1] == "whole"]
        rep.check(stopped and bool(cancelled) and win.state == "ready" and not self.engines.live,
                  "Cancel stops a whole-font run at once",
                  "at phase %s %s%%, Cancel took %.1f ms, state %r" % (phase, percent, ms, win.state))
        rep.check(len(font.writes()) == writes and len(FakeAlert.shown) == shown and win.result is result_before,
                  "a cancelled run applies nothing and keeps the preview",
                  "%d writes, %d alerts" % (len(font.writes()) - writes, len(FakeAlert.shown) - shown))

    def verify_apply(self, plan, before, res, snap, whole):
        rep, font, win, ui = self.report, self.font, self.win, self.ui
        mid = snap.master_id
        what = "whole font" if whole else "sample text"
        writes = font.writes()
        rep.check(win.last_apply is not None and win.revert_point is not None and ui.button(
            "Revert Last Apply").isEnabled(), "Apply (%s) keeps a revert point and enables Revert Last Apply" % what,
            ", ".join("%s %s" % (k, v) for k, v in sorted(win.last_apply.items())
                      if isinstance(v, int) and not isinstance(v, bool)))
        undo_on = [w for w in writes if w[5]]
        ui_on = [w for w in writes if not w[6]]
        rep.check(not undo_on and not ui_on and font.undo.disabled == 0 and font.updates_disabled == 0
                  and not font.undo.errors and not font.update_errors,
                  "Apply writes with undo registration and interface updates off, and turns them back on",
                  "%d writes, %d with undo on, %d with updates on" % (len(writes), len(undo_on), len(ui_on)))

        # every kerning entry of the plan, with valid keys
        bad = []
        for lk, rk, v in plan.kerning:
            have = font.kerningForFontMasterID_leftKey_rightKey_direction_(mid, lk, rk, LTR)
            if have >= NS_NOT_FOUND / 2 or abs(have - v) > 1e-6:
                bad.append((lk, rk, v, None if have >= NS_NOT_FOUND / 2 else have))
        rep.check(not bad and not font.bad_keys, "every kerning entry of the plan reads back (%s)" % what,
                  "%d entries; %d differ, %d bad keys %s" % (len(plan.kerning), len(bad), len(font.bad_keys),
                                                            (bad + font.bad_keys)[:3]))
        lefts = set(g.rightKerningGroup for g in font.glyphs if g.rightKerningGroup)
        rights = set(g.leftKerningGroup for g in font.glyphs if g.leftKerningGroup)
        orphans = sorted(set(k for lk, rk, _v in plan.kerning for k in (lk, rk)
                             if (k.startswith("@MMK_L_") and k[7:] not in lefts) or
                             (k.startswith("@MMK_R_") and k[7:] not in rights)))
        rep.check(not orphans, "every class key names a group glyphs carry on that side",
                  "%d orphans %s" % (len(orphans), orphans[:5]))

        # groups: set where planned, and only where a glyph had none
        after = font.state()
        wrong = [(n, sides) for n, sides in plan.groups_to_set.items()
                 if any(w is not None and w != have for w, have in zip(sides, after["groups"].get(n, (None, None))))]
        overwritten = [n for n in after["groups"] for k in (0, 1)
                       if before["groups"][n][k] is not None and after["groups"][n][k] != before["groups"][n][k]]
        new_names = sorted(set(g for sides in plan.groups_to_set.values() for g in sides if g))
        rep.check(not wrong and not overwritten, "groups are set where planned and existing groups are kept",
                  "%d glyphs joined %d groups (%s…); %d wrong, %d overwritten"
                  % (len(plan.groups_to_set), len(new_names), ", ".join(new_names[:6]), len(wrong), len(overwritten)))
        suffixed = [g for g in new_names if ".kk2" in g]
        if suffixed:
            rep.info("new groups renamed so as not to merge into an existing one: %s" % ", ".join(suffixed[:6]))

        # removals and the other master
        written = set((lk, rk) for lk, rk, _v in plan.kerning)
        table = font.kerningLTR.get(mid) or {}
        left_over = [(lk, rk) for lk, rk in (r[:2] for r in plan.removals)
                     if (lk, rk) not in written and rk in (table.get(lk) or {})]
        rep.check(not left_over, "Replace removes the planned existing entries",
                  "%d removals, %d left" % (len(plan.removals), len(left_over)))
        others = set(m.id for m in font.masters if m.id != mid)
        changed = diff_state(before, after, masters=others)
        rep.check(not changed, "the other master is untouched", "%d differences %s" % (len(changed), changed[:3]))

        # sidebearings: as planned, and never on a side Glyphs drives
        mbad = []
        for name, (lsb, rsb) in plan.metrics.items():
            layer = font.glyphs[name].layers[mid]
            for side, want, have in (("LSB", lsb, layer.LSB), ("RSB", rsb, layer.RSB)):
                if want is not None and abs(have - want) > 0.5 + 1e-6:
                    mbad.append((name, side, want, round(have, 2)))
        rep.check(not mbad, "every planned sidebearing reads back (within the half unit of whole-unit moves)",
                  "%d glyphs; %d differ %s" % (len(plan.metrics), len(mbad), mbad[:3]))
        index = snap.index
        ruled = []
        for kind, name, master_id, old, new, _u, _i in writes:
            side = kind.split()[-1]
            if master_id != mid or side not in ("LSB", "RSB") or kind.startswith("sync"):
                continue
            spec = snap.specs[index[name]] if name in index else None
            rule = (spec.lsb_rule if side == "LSB" else spec.rsb_rule) if spec is not None else kb.RULE_FREE
            if kind.startswith("aligned") or rule != kb.RULE_FREE:
                ruled.append((name, side, round(old, 1), round(new, 1)))
        rep.check(not ruled, "Apply leaves sides driven by metrics keys and aligned components to Glyphs",
                  "%d such sides written %s" % (len(ruled), ruled[:4]))

        # Glyphs' kerning of every pair of kerned glyphs = the engine's
        checked, kept, kbad = self.kerning_matches(snap, res, written, before["kerning"].get(mid) or {})
        rep.check(not kbad, "the font's kerning equals the result for every pair of kerned glyphs (%s)" % what,
                  "%d pairs, %d still from an entry Replace does not cover; %d differ %s"
                  % (checked, kept, len(kbad), kbad[:3]))
        # sidebearings: what the right pane showed is what the font has now
        # (sample scope: the glyphs of the sample text)
        shown = win.plan_names(res)
        sbad = []
        for i, name in enumerate(snap.names):
            m = res.metrics[i]
            if not m.valid or (shown is not None and name not in shown):
                continue
            layer = font.glyphs[name].layers[mid]
            d = max(abs(layer.LSB - m.lsb), abs(layer.RSB - m.rsb))
            if d > 1.01:
                sbad.append((name, round(layer.LSB, 1), round(m.lsb, 1), round(layer.RSB, 1), round(m.rsb, 1)))
        rep.check(not sbad, "every glyph's sidebearings in the font equal the result (keys and aligned composites "
                  "followed; %s)" % what, "%d glyphs; %d differ by more than a unit, e.g. (glyph, LSB, result, RSB, "
                  "result) %s" % (len(shown) if shown is not None else len(snap.names), len(sbad), sbad[:3]))
        left, right = ui.panes()
        scale = float(str(ui.size().titleOfSelectedItem())) / snap.upm
        cl, cr = pane_cells(left), pane_cells(right)
        diffs = [abs(a - b) / scale for a, b in zip(cl, cr) if a is not None and b is not None]
        far = [d for d in diffs if d > 2.5]
        rep.check(len(cl) == len(cr) and not far, "after Apply the left pane (the font) shows what the right pane "
                  "(the result) shows (%s)" % what, "%d cells, largest difference %.2f units"
                  % (len(diffs), max(diffs or [0])))

    def kerning_matches(self, snap, res, written, before):
        """The font's kerning of every pair of kerned glyphs, resolved as Glyphs
        resolves it (glyph–glyph, glyph–class, class–glyph, class–class),
        against result.value(). (pairs checked, pairs still decided by an
        existing entry the plan did not cover, [differences])."""
        font, mid = self.font, snap.master_id
        table = font.kerningLTR.get(mid) or {}
        names = snap.names
        kerned = sorted(self.kerned(res))
        keys = {}
        for i in kerned:
            g = font.glyphs[names[i]]
            keys[i] = (g.id, "@MMK_L_" + g.rightKerningGroup if g.rightKerningGroup else None,
                       "@MMK_R_" + g.leftKerningGroup if g.leftKerningGroup else None)
        lefts = [a for a in kerned for _b in kerned]
        rights = [b for _a in kerned for b in kerned]
        values = res.values(lefts, rights) if lefts else []
        checked = kept = 0
        bad = []
        for a, b, v in zip(lefts, rights, values):
            if v != v:
                continue
            checked += 1
            ga, la, _ = keys[a]
            gb, _, rb = keys[b]
            have, source = 0.0, None
            for x, y in ((ga, gb), (ga, rb), (la, gb), (la, rb)):
                if x is None or y is None:
                    continue
                row = table.get(x)
                if row is not None and y in row:
                    have, source = float(row[y]), (x, y)
                    break
            if abs(have - v) <= 0.5 + 1e-4:
                continue
            if source is not None and source not in written and (before.get(source[0]) or {}).get(source[1]) == have:
                kept += 1
                continue
            bad.append((names[a], names[b], round(v, 2), have, source))
        return checked, kept, bad

    def revert(self, before, left_before, label):
        rep, ui, win, font = self.report, self.ui, self.win, self.font
        font.log = []
        self.act("Revert Last Apply (%s)" % label, ui.button("Revert Last Apply").performClick_, None)
        rep.check(win.state == "applying", "Revert Last Apply works in the window's timer (%s)" % label, win.state)
        self.wait_for("Revert Last Apply", lambda: win.state == "ready" and not win.panes_due, 60.0,
                      stage="revert")
        diffs = diff_state(before, font.state())
        rep.check(not diffs, "Revert Last Apply restores the mock font exactly (%s)" % label,
                  "%d differences %s" % (len(diffs), diffs[:4]))
        writes = font.writes()
        rep.check(not [w for w in writes if w[5]] and font.undo.disabled == 0 and font.updates_disabled == 0,
                  "Revert writes with undo registration off", "%d writes" % len(writes))
        rep.check(not ui.button("Revert Last Apply").isEnabled(), "Revert Last Apply is disabled once used")
        scale = float(str(ui.size().titleOfSelectedItem())) / win.snapshot.upm
        cells = pane_cells(ui.panes()[0])
        moved = [abs(a - b) / scale for a, b in zip(cells, left_before) if a is not None and b is not None]
        rep.check(len(cells) == len(left_before) and max(moved or [0]) < 1e-3,
                  "after Revert the left pane shows the font as before",
                  "largest difference %.4f units" % max(moved or [0]))

    # 5. the whole font
    def whole_font(self):
        ui = self.ui
        self.act("Max pairs field", type_into, ui.max_pairs_field(), str(self.args.max_pairs))
        self.act("scope pop-up", choose, ui.scope(), "Whole font")
        FakeAlert.answer, FakeAlert.on_show = NSAlertSecondButtonReturn, None
        self.apply_and_verify(whole=True)
        saved = MockGlyphs.defaults[PREFIX + "maxPairs"]
        self.report.check(saved is not None and int(saved) == self.args.max_pairs,
                          "Max pairs is saved in Glyphs.defaults", repr(saved))

    # 6. closing
    def close_window(self):
        rep, win, kw = self.report, self.win, self.kw
        nswindow = win.w.getNSWindow()
        if self.args.png:
            write_png(nswindow, self.args.png)
            rep.info("wrote " + self.args.png)
        timers = list(self.timer_spy.timers) if self.timer_spy is not None else None
        result, context = win.result, win.context
        self.heartbeat.stage("close")
        nswindow.performClose_(None)
        spin(0.3)
        if any(w is win for w in open_windows(kw, type(win))) and getattr(win, "w", None) is not None:
            rep.info("the close button did not close the window; closing it through vanilla")
            win.w.close()
            spin(0.3)
        rep.check(not any(w is win for w in open_windows(kw, type(win))), "closing drops the window from "
                  "kk2_window's list of open windows")
        if timers is not None:
            rep.check(bool(timers) and not any(t.isValid() for t in timers), "closing invalidates the window's timers",
                      "%d timers made" % len(timers))
        rep.check(not self.engines.live, "closing frees the running engine job", "%d live" % len(self.engines.live))
        rep.check(freed(result) and freed(context) and freed(win.result) and freed(win.context),
                  "closing frees the result and the context")
        result = context = None
        self.first = win
        self.win = None
        self.labels = []
        self.bar = None
        self.ui = None

    def second_window(self):
        rep, kw = self.report, self.kw
        self.heartbeat.stage("second window")
        steps = self.reader.steps
        win = kw.open_window(RESOURCES)
        self.win = win
        if not rep.check(win is not None and getattr(win, "w", None) is not None and win is not self.first,
                         "a new window opens after the first was closed"):
            return
        ui = UI(win.w.getNSWindow())
        settings = {
            "tightness": (ui.tightness().doubleValue(), -0.5), "intensity": (ui.intensity().doubleValue(), 150.0),
            "threshold": (ui.threshold().doubleValue(), 3.0),
            "max pairs": (str(ui.max_pairs_field().stringValue()), str(self.args.max_pairs)),
            "scope": (str(ui.scope().titleOfSelectedItem()), "Whole font"),
            "size": (str(ui.size().titleOfSelectedItem()), "72"),
        }
        wrong = ["%s %r (saved %r)" % (k, a, b) for k, (a, b) in settings.items()
                 if (a != b if isinstance(b, str) else abs(a - b) > 1e-9)]
        rep.check(not wrong and str(ui.editor().string()) == SAMPLE,
                  "a new window comes back with the saved settings and sample text", "; ".join(wrong))
        # close it a few slices into reading the outlines
        try:
            self.wait_for("the second window to start reading", lambda: self.reader.steps >= steps + 2
                          or win.state != "reading", 30.0, stage="second window")
        except Abort as e:
            rep.info(str(e))
        state = win.state
        win.w.getNSWindow().performClose_(None)
        spin(0.5)
        if getattr(win, "w", None) is not None and any(w is win for w in open_windows(kw, type(win))):
            win.w.close()
            spin(0.3)
        after = self.reader.steps
        spin(0.3)
        rep.check(not any(w is win for w in open_windows(kw, type(win))) and not self.engines.live
                  and self.reader.steps == after and freed(win.context) and freed(win.result),
                  "a window closed while it %s stops reading and frees everything" % ("reads" if state == "reading"
                                                                                   else "works (%s)" % state),
                  "%d slices read before the close" % (after - steps))
        self.win = None

    # --- the end ------------------------------------------------------------
    def cleanup(self):
        for win in open_windows(self.kw):
            try:
                if getattr(win, "w", None) is not None:
                    win.w.close()
            except Exception:
                traceback.print_exc()
        spin(0.2)
        self.heartbeat.stop()
        FakeAlert.on_show = None

    def final_checks(self):
        rep = self.report
        worst, stage, state = self.heartbeat.worst()
        per_state = ", ".join("%s %.0f ms" % (s, g) for s, g in sorted(self.heartbeat.by_state.items(),
                                                                       key=lambda x: -x[1]))
        rep.check(worst <= self.args.max_stall, "the main thread is never busy for more than %.0f ms while the "
                  "window works" % self.args.max_stall,
                  "longest %.0f ms (%s, state %s); by state: %s" % (worst, stage, state, per_state))
        slow = sorted(self.callbacks, key=lambda c: -c[1])
        rep.check(not slow or slow[0][1] <= self.args.max_stall, "every control callback returns within %.0f ms"
                  % self.args.max_stall, ", ".join("%s %.0f ms" % c for c in slow[:4]))
        rep.check(not MockGlyphs.messages, "no Message() dialogs", "%r" % MockGlyphs.messages[:2])
        rep.check(not MISSING_API, "the mock covers the GlyphsApp names the modules import",
                  ", ".join(sorted(set(MISSING_API))))
        tracebacks = self.out.tracebacks + self.err.tracebacks
        rep.check(not tracebacks, "no tracebacks printed", (tracebacks[0][-800:] if tracebacks else ""))
        self.first = None
        gc.collect()
        open_outputs = [o for o in (r() for r in self.engines.outputs) if o is not None and getattr(o, "ptr", None)]
        rep.check(not open_outputs, "no engine context or result is left open",
                  "%d of %d taken still open" % (len(open_outputs), len(self.engines.outputs)))
        rep.check(not self.engines.live, "no engine job is left running", "%d" % len(self.engines.live))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--font", default="/System/Library/Fonts/Supplemental/Arial.ttf")
    ap.add_argument("--glyphs", type=int, default=400, help="spacing glyphs of the mock font (default 400)")
    ap.add_argument("--max-pairs", type=int, default=4000, help="Max pairs for the whole-font run (default 4000)")
    ap.add_argument("--max-stall", type=float, default=250.0,
                    help="longest tolerated main-thread stall in ms while the window works (default 250)")
    ap.add_argument("--slice-ms", type=float, default=25.0,
                    help="longest tolerated slice of outline reading in ms (default 25: 8 ms plus one glyph)")
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for any one step (default 300)")
    ap.add_argument("--png", default=None, help="save the window as a PNG before it closes")
    args = ap.parse_args()
    sys.exit(SmokeTest(args).run())


if __name__ == "__main__":
    main()
