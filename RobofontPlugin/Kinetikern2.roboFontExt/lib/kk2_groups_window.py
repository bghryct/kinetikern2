# encoding: utf-8
"""
kk2_groups_window — the Spacing Groups window: a picker that paints glyphs
into colour-coded groups.

Left: the font's sections (script and case for letters, figures, fractions,
punctuation, symbols…) to pick glyphs by the hundred. Middle: every glyph of
the spacing set as a tile, like a font window — click, Shift-click a
range, Command-click to add or remove, drag a rectangle; each tile is tinted
with its group's colour, frozen ones carry a snowflake. Right: the groups —
add one, name it, give it a colour, and either a Looseness offset and a
kerning force or Freeze — and buttons to put the selected glyphs into the
group chosen. Glyphs in no group follow the main window's settings.

Every change goes to the main window at once (a new preview) and is saved
into the font's lib.
"""

from __future__ import division, print_function, unicode_literals

import functools
import math
import traceback

import objc
import vanilla
from AppKit import (NSAffineTransform, NSBezierPath, NSColor, NSColorSpace, NSFont, NSFontAttributeName,
                    NSForegroundColorAttributeName, NSGraphicsContext, NSMakeRect, NSRectFill, NSString, NSView)

import kk2_groups as kg
import kk2_host as host

GRID_LABEL_H = 14
MODIFIER_SHIFT = 1 << 17
MODIFIER_COMMAND = 1 << 20


def _class(name, factory):
    try:
        return objc.lookUpClass(name)
    except objc.nosuchclass_error:
        return factory()


@functools.lru_cache(maxsize=256)
def hex_to_color(h, alpha=1.0):
    """NSColor of "#rrggbb" (cached: the grid asks for the same few colours
    for every tile)."""
    h = (h or "#888888").lstrip("#")
    try:
        r, g, b = int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0
    except ValueError:
        r = g = b = 0.5
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


def color_to_hex(c):
    try:
        c = c.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
        return "#%02x%02x%02x" % (int(round(255 * c.redComponent())), int(round(255 * c.greenComponent())),
                                  int(round(255 * c.blueComponent())))
    except Exception:
        return None


def _model_call(view, method, *args):
    model = getattr(view, "kk2_model", None)
    if model is None:
        return None
    try:
        return getattr(model, method)(view, *args)
    except Exception:
        print(traceback.format_exc())
        return None


def _make_grid_class():
    class KK2GlyphGrid(NSView):
        """Tiles of glyphs; drawing and mouse handling live in Python (kk2_model)."""

        def isFlipped(self):
            return True

        def acceptsFirstResponder(self):
            return True

        def drawRect_(self, rect):
            _model_call(self, "draw", rect)

        def mouseDown_(self, event):
            _model_call(self, "mouse_down", event)

        def mouseDragged_(self, event):
            _model_call(self, "mouse_dragged", event)

        def mouseUp_(self, event):
            _model_call(self, "mouse_up", event)

        def keyDown_(self, event):
            if not _model_call(self, "key_down", event):
                objc.super(KK2GlyphGrid, self).keyDown_(event)

    return KK2GlyphGrid


def _make_rows_class():
    class KK2GroupRows(NSView):
        """The list of groups with their colours."""

        def isFlipped(self):
            return True

        def drawRect_(self, rect):
            _model_call(self, "draw", rect)

        def mouseDown_(self, event):
            _model_call(self, "mouse_down", event)

    return KK2GroupRows


KK2GlyphGrid = _class("KK2GlyphGrid", _make_grid_class)
KK2GroupRows = _class("KK2GroupRows", _make_rows_class)


_ATTRS = {}


def _attrs(size, color=None, bold=False):
    """Text attributes, made once per size, weight and colour (system
    colours are dynamic: they follow light and dark mode when drawn)."""
    key = (size, bold, color)
    attrs = _ATTRS.get(key)
    if attrs is None:
        attrs = {NSFontAttributeName: NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size),
                 NSForegroundColorAttributeName: color or NSColor.secondaryLabelColor()}
        _ATTRS[key] = attrs
    return attrs


def _text(s, x, y, size=10.0, color=None, bold=False):
    NSString.stringWithString_(s).drawAtPoint_withAttributes_((x, y), _attrs(size, color, bold))


@functools.lru_cache(maxsize=8192)
def _text_width(s, size=10.0):
    return NSString.stringWithString_(s).sizeWithAttributes_(_attrs(size)).width


class GridModel(object):
    """The glyph grid: names shown, tile size, selection."""

    def __init__(self, owner):
        self.owner = owner
        self.names = []
        self.tile = 64.0
        self.cols = 1
        self.selection = set()
        self.anchor = None
        self._drag = None

    @property
    def tile_h(self):
        return self.tile + GRID_LABEL_H

    def layout(self, view):
        clip = view.superview()
        width = clip.bounds().size.width if clip is not None else view.frame().size.width
        height = clip.bounds().size.height if clip is not None else 300.0
        self.cols = max(1, int(width // self.tile))
        rows = int(math.ceil(len(self.names) / float(self.cols))) if self.names else 0
        view.setFrameSize_((width, max(height, rows * self.tile_h)))
        view.setNeedsDisplay_(True)

    def rect_of(self, i):
        col, row = i % self.cols, i // self.cols
        return NSMakeRect(col * self.tile, row * self.tile_h, self.tile, self.tile_h)

    def index_at(self, pt):
        col, row = int(pt.x // self.tile), int(pt.y // self.tile_h)
        if col < 0 or col >= self.cols or row < 0:
            return None
        i = row * self.cols + col
        return i if i < len(self.names) else None

    # ------------------------------------------------------------ drawing
    def draw(self, view, rect):
        NSColor.textBackgroundColor().set()
        NSRectFill(rect)
        if not self.names:
            _text("No glyphs to show.", 12, 12, 12.0)
            return
        owner = self.owner
        groups = owner.groups
        upm = owner.upm()
        first = max(0, int(rect.origin.y // self.tile_h))
        last = int((rect.origin.y + rect.size.height) // self.tile_h)
        scale = 0.58 * self.tile / upm
        grid = NSColor.separatorColor()
        for row in range(first, last + 1):
            for col in range(self.cols):
                i = row * self.cols + col
                if i >= len(self.names):
                    break
                r = self.rect_of(i)
                name = self.names[i]
                g = groups.group_of(name)
                if g is not None:
                    hex_to_color(g.color, 0.22).set()
                    NSBezierPath.fillRect_(NSMakeRect(r.origin.x + 1, r.origin.y + 1, r.size.width - 2,
                                                      r.size.height - 2))
                selected = name in self.selection
                if selected:
                    NSColor.selectedContentBackgroundColor().colorWithAlphaComponent_(0.28).set()
                    NSBezierPath.fillRect_(NSMakeRect(r.origin.x + 1, r.origin.y + 1, r.size.width - 2,
                                                      r.size.height - 2))
                grid.set()
                NSBezierPath.strokeRect_(NSMakeRect(r.origin.x + 0.5, r.origin.y + 0.5, r.size.width - 1,
                                                    r.size.height - 1))
                path, adv = owner.path_of(name)
                if path is not None:
                    ctx = NSGraphicsContext.currentContext()
                    ctx.saveGraphicsState()
                    t = NSAffineTransform.transform()
                    t.translateXBy_yBy_(r.origin.x + 0.5 * (self.tile - adv * scale), r.origin.y + 0.8 * self.tile)
                    t.scaleXBy_yBy_(scale, -scale)
                    t.concat()
                    (hex_to_color(g.color) if g is not None and g.frozen else NSColor.textColor()).set()
                    path.fill()
                    ctx.restoreGraphicsState()
                if g is not None:
                    hex_to_color(g.color).set()
                    NSBezierPath.fillRect_(NSMakeRect(r.origin.x + 1, r.origin.y + 1, r.size.width - 2, 3))
                    if g.frozen:
                        _text("❄", r.origin.x + r.size.width - 13, r.origin.y + 3, 10.0, hex_to_color(g.color))
                if selected:
                    NSColor.selectedContentBackgroundColor().set()
                    ring = NSBezierPath.bezierPathWithRect_(NSMakeRect(r.origin.x + 2, r.origin.y + 2,
                                                                       r.size.width - 4, r.size.height - 4))
                    ring.setLineWidth_(2.0)
                    ring.stroke()
                label = name if _text_width(name, 8.5) < self.tile - 4 else name[: max(3, int(self.tile / 6))] + "…"
                _text(label, r.origin.x + 0.5 * (self.tile - _text_width(label, 8.5)),
                      r.origin.y + self.tile, 8.5)
        if self._drag is not None and self._drag.get("rect") is not None:
            x0, y0, x1, y1 = self._drag["rect"]
            box = NSMakeRect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            NSColor.selectedContentBackgroundColor().colorWithAlphaComponent_(0.15).set()
            NSBezierPath.fillRect_(box)
            NSColor.selectedContentBackgroundColor().set()
            NSBezierPath.strokeRect_(box)

    # -------------------------------------------------------------- mouse
    def _point(self, view, event):
        return view.convertPoint_fromView_(event.locationInWindow(), None)

    def mouse_down(self, view, event):
        view.window().makeFirstResponder_(view)
        pt = self._point(view, event)
        i = self.index_at(pt)
        flags = event.modifierFlags()
        shift, cmd = bool(flags & MODIFIER_SHIFT), bool(flags & MODIFIER_COMMAND)
        base = set(self.selection) if (shift or cmd) else set()
        if i is not None:
            name = self.names[i]
            if event.clickCount() == 2:
                self.owner.select_group_of(name)
                return
            if shift and self.anchor is not None and self.anchor < len(self.names):
                a, b = sorted((self.anchor, i))
                self.selection = base | set(self.names[a:b + 1])
            elif cmd:
                self.selection = base ^ {name}
                self.anchor = i
            else:
                self.selection = {name}
                self.anchor = i
        else:
            self.selection = base
        self._drag = {"start": (pt.x, pt.y), "base": base, "rect": None}
        view.setNeedsDisplay_(True)
        self.owner.selection_changed()

    def mouse_dragged(self, view, event):
        if self._drag is None:
            return
        pt = self._point(view, event)
        x0, y0 = self._drag["start"]
        if abs(pt.x - x0) < 3 and abs(pt.y - y0) < 3:
            return
        self._drag["rect"] = (x0, y0, pt.x, pt.y)
        lo_x, hi_x = min(x0, pt.x), max(x0, pt.x)
        lo_y, hi_y = min(y0, pt.y), max(y0, pt.y)
        hit = set()
        c0, c1 = max(0, int(lo_x // self.tile)), min(self.cols - 1, int(hi_x // self.tile))
        r0, r1 = max(0, int(lo_y // self.tile_h)), int(hi_y // self.tile_h)
        for row in range(r0, r1 + 1):
            for col in range(c0, c1 + 1):
                i = row * self.cols + col
                if i < len(self.names):
                    hit.add(self.names[i])
        self.selection = self._drag["base"] | hit
        view.autoscroll_(event)
        view.setNeedsDisplay_(True)
        self.owner.selection_changed()

    def mouse_up(self, view, event):
        self._drag = None
        view.setNeedsDisplay_(True)

    def key_down(self, view, event):
        chars = event.charactersIgnoringModifiers() or ""
        flags = event.modifierFlags()
        if chars == "a" and flags & MODIFIER_COMMAND:
            self.selection = set(self.names)
            view.setNeedsDisplay_(True)
            self.owner.selection_changed()
            return True
        if chars in ("\x7f", ""):  # delete: back to the main settings
            self.owner.unassign(None)
            return True
        return False


class RowsModel(object):
    """The groups list: a row for the main settings, then one per group."""

    ROW_H = 26.0

    def __init__(self, owner):
        self.owner = owner

    def rows(self):
        return [None] + list(self.owner.groups.groups)

    def layout(self, view):
        clip = view.superview()
        width = clip.bounds().size.width if clip is not None else view.frame().size.width
        height = clip.bounds().size.height if clip is not None else 100.0
        view.setFrameSize_((width, max(height, len(self.rows()) * self.ROW_H)))
        view.setNeedsDisplay_(True)

    def draw(self, view, rect):
        NSColor.textBackgroundColor().set()
        NSRectFill(rect)
        owner = self.owner
        counts = owner.groups.counts(owner.name_set)
        assigned = sum(counts.values())
        width = view.frame().size.width
        for k, g in enumerate(self.rows()):
            y = k * self.ROW_H
            selected = (g is None and owner.current is None) or (g is not None and g.gid == owner.current)
            if selected:
                NSColor.selectedContentBackgroundColor().colorWithAlphaComponent_(0.22).set()
                NSBezierPath.fillRect_(NSMakeRect(0, y, width, self.ROW_H))
            color = NSColor.tertiaryLabelColor() if g is None else hex_to_color(g.color)
            color.set()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(8, y + 7, 12, 12)).fill()
            if g is None:
                title = "Main settings (no group)"
                n = len(owner.name_set) - assigned
                desc = "the main window's sliders"
            else:
                title = g.name
                n = counts.get(g.gid, 0)
                desc = g.describe()
            _text(title, 28, y + 3, 11.0, NSColor.labelColor(), bold=selected)
            _text("%d · %s" % (n, desc), 28, y + 15, 8.5)

    def mouse_down(self, view, event):
        pt = view.convertPoint_fromView_(event.locationInWindow(), None)
        k = int(pt.y // self.ROW_H)
        rows = self.rows()
        if 0 <= k < len(rows):
            g = rows[k]
            if event.clickCount() == 2:
                self.owner.select_members(g.gid if g is not None else None)
            else:
                self.owner.choose_group(g.gid if g is not None else None)


class GroupsWindow(object):
    """The Spacing Groups window of one KK2Window."""

    def __init__(self, main):
        self.main = main
        self.current = None  # group id the controls edit (None: the main settings row)
        self.name_set = set()
        self._sections = []
        self._section_rows = []
        self._filter = ""
        self.grid = GridModel(self)
        self.rows = RowsModel(self)
        self._build()
        self.refresh()

    # ------------------------------------------------------------ helpers
    @property
    def groups(self):
        """The main window's groups (never a copy: they may be replaced)."""
        return self.main.groups

    def groups_replaced(self):
        """The main window has a new GroupSet: show it."""
        self.current = None
        self.w.match.set(self.groups.match_frozen)
        self._update_sections()
        self._update_all()
        self._sync_controls()

    def upm(self):
        snap = self.main.snapshot
        return float(snap.upm) if snap is not None else 1000.0

    def path_of(self, name):
        snap = self.main.snapshot
        info = snap.infos.get(name) if snap is not None else None
        if info is None:
            return None, 0.0
        return getattr(info, "path", None), float(getattr(info, "width", 0.0) or 0.0)

    # ----------------------------------------------------------------- UI
    def _build(self):
        title = "Spacing Groups — %s" % host.font_title(self.main.font)
        w = vanilla.Window((1240, 720), title, minSize=(960, 520))
        self.w = w
        # left: sections
        w.sectionsLabel = vanilla.TextBox((12, 10, 220, 17), "Sections", sizeStyle="small")
        w.sections = vanilla.List((12, 30, 310, -126), [], columnDescriptions=[
            dict(title="Section", key="section"), dict(title="Glyphs", key="count", width=46),
            dict(title="Grouped", key="grouped", width=56)],
            selectionCallback=self.sectionSelected, allowsMultipleSelection=True, drawFocusRing=False)
        w.selectAll = vanilla.Button((12, -118, 60, 20), "All", sizeStyle="small", callback=self.selectAllGlyphs)
        w.selectNone = vanilla.Button((76, -118, 60, 20), "None", sizeStyle="small", callback=self.selectNoGlyphs)
        w.selectInvert = vanilla.Button((140, -118, 60, 20), "Invert", sizeStyle="small",
                                        callback=self.invertSelection)
        w.selectFree = vanilla.Button((12, -94, 188, 20), "Glyphs in no group", sizeStyle="small",
                                      callback=self.selectUnassigned)
        w.search = vanilla.SearchBox((12, -66, 310, 22), placeholder="Filter by glyph name",
                                     callback=self.searchChanged)
        w.hint = vanilla.TextBox((12, -38, 310, 30),
                                 "Click a section to select its glyphs; Command-click sections to combine them.",
                                 sizeStyle="mini")

        # middle: the grid
        self.grid_view = KK2GlyphGrid.alloc().initWithFrame_(((0, 0), (600, 400)))
        self.grid_view.kk2_model = self.grid
        w.gridScroll = vanilla.ScrollView((334, 30, -330, -40), self.grid_view, hasHorizontalScroller=False,
                                          autohidesScrollers=True)
        w.gridLabel = vanilla.TextBox((334, 10, -330, 17), "", sizeStyle="small")
        w.tileLabel = vanilla.TextBox((334, -32, 60, 17), "Tile size", sizeStyle="mini")
        w.tile = vanilla.Slider((390, -34, 160, 20), minValue=40, maxValue=120, value=64,
                                callback=self.tileChanged, sizeStyle="small")
        w.selectionInfo = vanilla.TextBox((560, -32, -330, 17), "", sizeStyle="mini")

        # right: groups and their settings
        x = -318
        w.groupsLabel = vanilla.TextBox((x, 10, 200, 17), "Groups", sizeStyle="small")
        self.rows_view = KK2GroupRows.alloc().initWithFrame_(((0, 0), (300, 180)))
        self.rows_view.kk2_model = self.rows
        w.rowsScroll = vanilla.ScrollView((x, 30, -12, 190), self.rows_view, hasHorizontalScroller=False,
                                          autohidesScrollers=True)
        w.addGroup = vanilla.Button((x, 228, 96, 22), "New Group", callback=self.addGroup, sizeStyle="small")
        w.byCategory = vanilla.Button((x + 100, 228, 100, 22), "By Category", callback=self.byCategory,
                                      sizeStyle="small")
        w.byCategory.getNSButton().setToolTip_(
            "Puts the glyphs in no group yet into groups by kind — Figures, Punctuation, Symbols, and the letters "
            "of each script but Latin — so each kind can have its own Looseness and kerning force. New groups "
            "start at the main settings; groups of those names already there keep theirs.")
        w.removeGroup = vanilla.Button((x + 204, 228, -12, 22), "Delete Group", callback=self.removeGroup,
                                       sizeStyle="small")
        w.nameLabel = vanilla.TextBox((x, 262, 60, 17), "Name", sizeStyle="small")
        w.name = vanilla.EditText((x + 60, 260, -60, 21), "", callback=self.nameChanged, sizeStyle="small")
        w.color = vanilla.ColorWell((-50, 258, -12, 24), callback=self.colorChanged)
        w.modeLabel = vanilla.TextBox((x, 294, 60, 17), "Mode", sizeStyle="small")
        w.mode = vanilla.SegmentedButton((x + 60, 291, -12, 22),
                                         [dict(title="Space"), dict(title="Freeze")],
                                         callback=self.modeChanged, sizeStyle="small")
        w.looseLabel = vanilla.TextBox((x, 326, 260, 17), "Looseness (offset from the main slider)",
                                       sizeStyle="small")
        w.loose = vanilla.Slider((x, 346, -64, 22), minValue=-1.0, maxValue=1.0, value=0.0,
                                 callback=self.looseChanged, sizeStyle="small")
        w.looseValue = vanilla.TextBox((-58, 349, -12, 17), "", sizeStyle="small", alignment="right")
        w.forceLabel = vanilla.TextBox((x, 376, 260, 17), "Kerning force (percent of the main intensity)",
                                       sizeStyle="small")
        w.force = vanilla.Slider((x, 396, -64, 22), minValue=0.0, maxValue=300.0, value=100.0,
                                 callback=self.forceChanged, sizeStyle="small")
        w.forceValue = vanilla.TextBox((-58, 399, -12, 17), "", sizeStyle="small", alignment="right")
        w.assign = vanilla.Button((x, 436, -12, 24), "Put Selected Glyphs into This Group", callback=self.assign)
        w.unassignButton = vanilla.Button((x, 466, -12, 22), "Take Selected Glyphs out of Their Groups",
                                          callback=self.unassignButton, sizeStyle="small")
        w.match = vanilla.CheckBox((x, 502, -12, 20), "Match the frozen spacing", sizeStyle="small",
                                   value=self.groups.match_frozen, callback=self.matchChanged)
        w.matchNote = vanilla.TextBox((x, 524, -12, 44),
                                      "New glyphs come out as tight or loose as the frozen ones; the main "
                                      "Looseness slider is then an offset from them.", sizeStyle="mini")
        w.summary = vanilla.TextBox((x, 576, -12, -12), "", sizeStyle="mini")
        w.bind("resize", self.resized)
        w.bind("close", self.closed)
        w.open()
        self._sync_controls()

    # ------------------------------------------------------------ refresh
    def refresh(self):
        """The snapshot changed (or first open): sections and grid anew."""
        snap = self.main.snapshot
        names = list(snap.names) if snap is not None else []
        self.name_set = set(names)
        self._names = names
        self._sections = kg.snapshot_sections(snap) if snap is not None else []
        self.grid.selection &= self.name_set
        self._apply_filter()
        self._update_sections()
        self._update_all()

    def _apply_filter(self):
        f = self._filter.lower().strip()
        self.grid.names = [n for n in self._names if f in n.lower()] if f else list(self._names)
        self.grid.layout(self.grid_view)

    def _update_sections(self):
        members = self.groups.members
        rows = []
        for title, names in self._sections:
            grouped = sum(1 for n in names if n in members)
            rows.append({"section": title, "count": len(names), "grouped": grouped or ""})
        self._section_rows = rows
        selection = self.w.sections.getSelection()
        self.w.sections.set(rows)
        self.w.sections.setSelection([i for i in selection if i < len(rows)])

    def _update_all(self):
        self.rows.layout(self.rows_view)
        self.grid_view.setNeedsDisplay_(True)
        self.rows_view.setNeedsDisplay_(True)
        snap = self.main.snapshot
        self.w.gridLabel.set("%d glyphs of %s%s" % (len(self.grid.names), snap.master_name if snap else "—",
                                                    " (filtered)" if self._filter else ""))
        self.w.summary.set(self.groups.summary(self.name_set) + self.main.groups_note())
        self.selection_changed()

    def _sync_controls(self):
        g = self.groups.group(self.current) if self.current is not None else None
        w = self.w
        editable = g is not None
        for c in (w.name, w.color, w.mode, w.removeGroup):
            c.enable(editable)
        space = editable and not g.frozen
        w.loose.enable(space)
        w.force.enable(space)
        if g is None:
            w.name.set("")
            w.loose.set(0.0)
            w.force.set(100.0)
            w.looseValue.set("")
            w.forceValue.set("")
            w.assign.setTitle("Use the Main Settings for Selected Glyphs")
            return
        w.name.set(g.name)
        w.color.set(hex_to_color(g.color))
        w.mode.set(1 if g.frozen else 0)
        w.loose.set(g.looseness)
        w.force.set(g.force)
        w.looseValue.set("%+.2f" % g.looseness)
        w.forceValue.set("%d%%" % round(g.force))
        w.assign.setTitle("Put Selected Glyphs into “%s”" % g.name)

    def _changed(self, structure=False):
        """A group setting or membership changed: preview again, save later."""
        if structure:
            self._update_sections()
        self._update_all()
        self.main.groups_changed()

    # ----------------------------------------------------------- callbacks
    def selection_changed(self):
        n = len(self.grid.selection)
        self.w.selectionInfo.set("%d selected" % n if n else "Click, Shift-click, Command-click or drag to select")

    def select_group_of(self, name):
        g = self.groups.group_of(name)
        self.choose_group(g.gid if g is not None else None)

    def select_members(self, gid):
        if gid is None:
            sel = set(n for n in self._names if n not in self.groups.members)
        else:
            sel = set(n for n, k in self.groups.members.items() if k == gid and n in self.name_set)
        self.grid.selection = sel
        self.grid_view.setNeedsDisplay_(True)
        self.choose_group(gid)

    def choose_group(self, gid):
        self.current = gid
        self._sync_controls()
        self.rows_view.setNeedsDisplay_(True)

    def sectionSelected(self, sender):
        picked = set()
        for i in sender.getSelection():
            if i < len(self._sections):
                picked.update(self._sections[i][1])
        if picked:
            self.grid.selection = picked
            self.grid_view.setNeedsDisplay_(True)
            self.selection_changed()

    def selectAllGlyphs(self, sender):
        self.grid.selection = set(self.grid.names)
        self.grid_view.setNeedsDisplay_(True)
        self.selection_changed()

    def selectNoGlyphs(self, sender):
        self.grid.selection = set()
        self.w.sections.setSelection([])
        self.grid_view.setNeedsDisplay_(True)
        self.selection_changed()

    def invertSelection(self, sender):
        self.grid.selection = set(self.grid.names) - self.grid.selection
        self.grid_view.setNeedsDisplay_(True)
        self.selection_changed()

    def selectUnassigned(self, sender):
        self.select_members(None)

    def searchChanged(self, sender):
        self._filter = sender.get() or ""
        self._apply_filter()
        self._update_all()

    def tileChanged(self, sender):
        self.grid.tile = float(int(sender.get()))
        self.grid.layout(self.grid_view)

    def resized(self, sender):
        self.grid.layout(self.grid_view)
        self.rows.layout(self.rows_view)

    def addGroup(self, sender):
        g = self.groups.add_group()
        self.current = g.gid
        if self.grid.selection:
            self.groups.assign(sorted(self.grid.selection), g.gid)
        self._sync_controls()
        self._changed(structure=True)
        self.w.name.getNSTextField().selectText_(None)

    def byCategory(self, sender):
        """Groups by kind (kk2_groups.by_category), each spaced on its own."""
        snap = self.main.snapshot
        if snap is None:
            return
        added = kg.by_category(self.groups, kg.snapshot_entries(snap))
        made = [(name, n) for name, n in added if n]
        if made:
            first = next((g for g in self.groups.groups if g.name.strip().lower() == made[0][0].lower()), None)
            if first is not None:
                self.current = first.gid
        self._sync_controls()
        self._changed(structure=True)
        self.w.selectionInfo.set("By category: " + (", ".join("%s %d" % m for m in made) if made else
                                                    "every figure, mark of punctuation, symbol and non-Latin letter "
                                                    "is in a group already"))
        return added

    def removeGroup(self, sender):
        if self.current is None:
            return
        self.groups.remove_group(self.current)
        self.current = None
        self._sync_controls()
        self._changed(structure=True)

    def nameChanged(self, sender):
        if self.current is not None:
            self.groups.update(self.current, name=(sender.get() or "").strip() or "Group")
            self._sync_controls()
            self._update_all()
            self.main.groups_changed(preview=False)

    def colorChanged(self, sender):
        if self.current is not None:
            h = color_to_hex(sender.get())
            if h:
                self.groups.update(self.current, color=h)
                self._update_all()
                self.main.groups_changed(preview=False)

    def modeChanged(self, sender):
        if self.current is not None:
            self.groups.update(self.current, mode=kg.MODE_FREEZE if sender.get() == 1 else kg.MODE_SPACE)
            self._sync_controls()
            self._changed()

    def looseChanged(self, sender):
        if self.current is not None:
            v = round(float(sender.get()), 2)
            self.groups.update(self.current, looseness=v)
            self.w.looseValue.set("%+.2f" % v)
            self._changed()

    def forceChanged(self, sender):
        if self.current is not None:
            v = round(float(sender.get()))
            self.groups.update(self.current, force=v)
            self.w.forceValue.set("%d%%" % v)
            self._changed()

    def assign(self, sender):
        if not self.grid.selection:
            self.w.selectionInfo.set("Select glyphs first")
            return
        self.groups.assign(sorted(self.grid.selection), self.current)
        self._changed(structure=True)

    def unassign(self, sender):
        if self.grid.selection:
            self.groups.assign(sorted(self.grid.selection), None)
            self._changed(structure=True)

    def unassignButton(self, sender):
        self.unassign(None)

    def matchChanged(self, sender):
        self.groups.match_frozen = bool(sender.get())
        self.groups.version += 1
        self._changed()

    def closed(self, sender):
        self.grid_view.kk2_model = None
        self.rows_view.kk2_model = None
        self.main.groups_window_closed()

    def close(self):
        try:
            self.w.close()
        except Exception:
            pass
