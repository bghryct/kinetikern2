# encoding: utf-8
"""
kk2_proof — the side-by-side proofing panes of the Kinetikern2 window.

Each pane is a TextKit 1 NSTextView (an explicit storage → layout manager →
container stack, so the view never switches to TextKit 2) whose glyphs are
text attachments. Each attachment cell draws a glyph's decomposed outline
(the snapshot's GlyphInfo.path):

* the cell's width is the glyph's advance width plus the kerning to the glyph
  on its right (TextKit ignores NSKern on attachment characters, and its
  default cell frame floors widths to whole points, so the cell reports its
  exact frame itself),
* the outline is moved so that its ink starts at the left sidebearing.

A glyph's cells come from a small per-glyph pool: one cell per distinct
(glyph, kern) of the text, reused from render to render. New spacing is
therefore just new cell metrics plus a re-laid attributed string. Spaces stay
real space characters (sized to the font's space glyph with NSKern) so that
word wrapping keeps working.

Right-to-left glyphs are set right to left: a paragraph takes the direction
of its first glyph, and every run of glyphs of one direction carries a
writing-direction override (all glyphs are the same neutral U+FFFC character,
so the bidi algorithm alone could not tell).

Everything here runs on the main thread. A render costs O(tokens of the
sample text) and never looks at the rest of the font.
"""

from __future__ import division, print_function, unicode_literals

import time

import objc
from AppKit import (NSAffineTransform, NSAttachmentAttributeName, NSAttributedString, NSBezierPath, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName, NSGraphicsContext, NSKernAttributeName,
                    NSLayoutManager, NSLineBreakByWordWrapping, NSMakePoint, NSMakeRect, NSMakeSize,
                    NSMutableAttributedString, NSMutableParagraphStyle, NSNotificationCenter, NSObject,
                    NSParagraphStyleAttributeName, NSTextAttachment, NSTextAttachmentCell, NSTextContainer,
                    NSTextStorage, NSTextView, NSViewBoundsDidChangeNotification, NSViewFrameDidChangeNotification,
                    NSViewWidthSizable, NSWritingDirectionAttributeName)

ATTACHMENT = "￼"
# the tokens of kk2_snapshot.tokenize besides glyph names
NEWLINE = "\n"
SPACE = " "
MISSING = None

_LTR, _RTL = 0, 1  # NSWritingDirectionLeftToRight, NSWritingDirectionRightToLeft
_OVERRIDE = 2  # NSWritingDirectionOverride
_SPACE_FONT = NSFont.systemFontOfSize_(1.0)
_SPACE_ADVANCE = _SPACE_FONT.advancementForGlyph_(_SPACE_FONT.glyphWithName_("space")).width
_DELEGATE_KEY = "kk2.wrapDelegate"  # associated-object key: the layout manager keeps its delegate alive
_NO_SIZE = NSMakeSize(0.0, 0.0)
_NO_POINT = NSMakePoint(0.0, 0.0)
_NO_RECT = NSMakeRect(0.0, 0.0, 0.0, 0.0)


def _class(name, factory):
    """One Objective-C class per process: Glyphs runs every plugin in one runtime."""
    try:
        return objc.lookUpClass(name)
    except objc.nosuchclass_error:
        return factory()


def _make_cell_class():
    class KK2GlyphCell(NSTextAttachmentCell):
        """Draws one glyph outline; geometry is set from Python. TextKit asks
        for the geometry of every cell on every layout, so the answers are
        made once, in set_glyph."""

        @objc.python_method
        def set_glyph(self, path, width, shift, ascent, descent, color):
            width = max(width, 0.0)
            key = (path, width, shift, ascent, descent, color)
            if self._kk2_key == key:
                return  # a pooled cell that shows the same as last time
            self._kk2_key = key
            self._kk2_path = path
            self._kk2_shift = shift
            self._kk2_ascent = ascent
            self._kk2_color = color
            self._kk2_size = NSMakeSize(width, ascent + descent)
            self._kk2_offset = NSMakePoint(0.0, -descent)
            self._kk2_frame = NSMakeRect(0.0, -descent, width, ascent + descent)

        def cellSize(self):
            return getattr(self, "_kk2_size", _NO_SIZE)

        def cellBaselineOffset(self):
            return getattr(self, "_kk2_offset", _NO_POINT)

        def cellFrameForTextContainer_proposedLineFragment_glyphPosition_characterIndex_(self, container, fragment,
                                                                                          position, index):
            # the default frame floors the width to whole points
            return getattr(self, "_kk2_frame", _NO_RECT)

        def wantsToTrackMouse(self):
            return False

        def drawWithFrame_inView_(self, frame, view):
            path = getattr(self, "_kk2_path", None)
            if path is None:
                return
            ctx = NSGraphicsContext.currentContext()
            ctx.saveGraphicsState()
            t = NSAffineTransform.transform()
            # the text view is flipped: the baseline lies `ascent` below the cell top
            t.translateXBy_yBy_(frame.origin.x + self._kk2_shift, frame.origin.y + self._kk2_ascent)
            t.concat()
            self._kk2_color.setFill()
            path.fill()
            ctx.restoreGraphicsState()

        def drawWithFrame_inView_characterIndex_layoutManager_(self, frame, view, index, layoutManager):
            self.drawWithFrame_inView_(frame, view)

    return KK2GlyphCell


KK2GlyphCell = _class("KK2GlyphCell", _make_cell_class)


def _make_sync_class():
    class KK2PaneSync(NSObject):
        """Keeps scroll views at the same relative vertical position and keeps
        proof text views exactly as wide as their pane."""

        def init(self):
            self = objc.super(KK2PaneSync, self).init()
            if self is None:
                return None
            self._kk2_clips = []
            self._kk2_busy = False
            return self

        @objc.python_method
        def attach(self, scroll_views, fit_width=()):
            center = NSNotificationCenter.defaultCenter()
            for sv in scroll_views:
                clip = sv.contentView()
                clip.setPostsBoundsChangedNotifications_(True)
                center.addObserver_selector_name_object_(self, "clipBoundsChanged:", NSViewBoundsDidChangeNotification, clip)
                self._kk2_clips.append(clip)
            # vanilla adds the document view while the scroll view is still
            # empty, so autoresizing alone would keep the wrong width
            for sv in fit_width:
                clip = sv.contentView()
                clip.setPostsFrameChangedNotifications_(True)
                center.addObserver_selector_name_object_(self, "clipFrameChanged:", NSViewFrameDidChangeNotification, clip)
                _fit_document_width(clip)

        @objc.python_method
        def detach(self):
            NSNotificationCenter.defaultCenter().removeObserver_(self)
            self._kk2_clips = []

        def clipFrameChanged_(self, note):
            _fit_document_width(note.object())

        def clipBoundsChanged_(self, note):
            if self._kk2_busy:
                return
            source = note.object()
            ratio = _scroll_ratio(source)
            if ratio is None:
                return
            self._kk2_busy = True
            try:
                for clip in self._kk2_clips:
                    if clip is not source:
                        _scroll_to_ratio(clip, ratio)
            finally:
                self._kk2_busy = False

    return KK2PaneSync


KK2PaneSync = _class("KK2PaneSync", _make_sync_class)


def _make_wrap_class():
    class KK2WrapDelegate(NSObject):
        """Layout manager delegate: break lines only at spaces.

        Every glyph is an attachment character (U+FFFC), whose Unicode line
        break class is "contingent", so TextKit would otherwise wrap between
        any two glyphs of a word.
        """

        def layoutManager_shouldBreakLineByWordBeforeCharacterAtIndex_(self, layoutManager, index):
            # one character per token: the pane precomputes where words start
            return index in getattr(self, "kk2_word_starts", ())

    return KK2WrapDelegate


KK2WrapDelegate = _class("KK2WrapDelegate", _make_wrap_class)


def _fit_document_width(clip):
    doc = clip.documentView()
    if doc is None:
        return
    width = clip.bounds().size.width
    if abs(doc.frame().size.width - width) > 0.5:
        doc.setFrameSize_(NSMakeSize(width, doc.frame().size.height))


def _scroll_ratio(clip):
    doc = clip.documentView()
    if doc is None:
        return None
    span = doc.frame().size.height - clip.bounds().size.height
    return clip.bounds().origin.y / span if span > 0 else 0.0


def _scroll_to_ratio(clip, ratio):
    doc = clip.documentView()
    if doc is None:
        return
    span = doc.frame().size.height - clip.bounds().size.height
    y = max(0.0, min(span, ratio * span)) if span > 0 else 0.0
    if abs(clip.bounds().origin.y - y) > 0.5:
        clip.scrollToPoint_(NSMakePoint(clip.bounds().origin.x, y))
        clip.superview().reflectScrolledClipView_(clip)


def make_text_view(width=400.0, height=300.0):
    """A read-only TextKit 1 text view for a proof pane: (view, storage)."""
    storage = NSTextStorage.alloc().init()
    layout = NSLayoutManager.alloc().init()
    storage.addLayoutManager_(layout)
    container = NSTextContainer.alloc().initWithContainerSize_(NSMakeSize(width, 1.0e7))
    container.setWidthTracksTextView_(True)
    layout.addTextContainer_(container)
    view = NSTextView.alloc().initWithFrame_textContainer_(NSMakeRect(0, 0, width, height), container)
    view.setEditable_(False)
    view.setSelectable_(False)
    view.setRichText_(True)
    view.setVerticallyResizable_(True)
    view.setHorizontallyResizable_(False)
    view.setAutoresizingMask_(NSViewWidthSizable)
    view.setMinSize_(NSMakeSize(0.0, height))
    view.setMaxSize_(NSMakeSize(1.0e7, 1.0e7))
    view.setTextContainerInset_(NSMakeSize(14.0, 14.0))
    view.setDrawsBackground_(True)
    view.setBackgroundColor_(NSColor.textBackgroundColor())
    return view, storage


def _finite(value, fallback):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    return value if value - value == 0.0 else fallback  # NaN and ±inf fail this


def vertical_metrics(snapshot):
    """(ascender, descender, cap height) of the snapshot's master in font units.

    Read from the master the snapshot was taken of (the Snapshot itself only
    carries what the engine needs); a font without usable values gets the
    usual proportions of the em.
    """
    upm = _finite(getattr(snapshot, "upm", None), 0.0) or 1000.0
    master = None
    font = getattr(snapshot, "font", None)
    if font is not None:
        try:
            for m in font.masters:
                if m.id == snapshot.master_id:
                    master = m
                    break
        except Exception:
            master = None
    ascender = _finite(getattr(master, "ascender", None), 0.0)
    descender = _finite(getattr(master, "descender", None), 0.0)
    cap_height = _finite(getattr(master, "capHeight", None), 0.0)
    if ascender <= 0.0:
        ascender = 0.8 * upm
    if descender >= 0.0:
        descender = -0.2 * upm
    if cap_height <= 0.0:
        cap_height = 0.7 * upm
    return ascender, descender, cap_height


def _paragraph_style(line, direction):
    para = NSMutableParagraphStyle.alloc().init()
    para.setMinimumLineHeight_(line)
    para.setMaximumLineHeight_(line)
    para.setParagraphSpacing_(line * 0.35)
    para.setLineBreakMode_(NSLineBreakByWordWrapping)
    para.setBaseWritingDirection_(direction)
    return para


class ProofPane(object):
    """One proofing pane: glyph cells per (glyph, kern), the text re-laid on every render."""

    def __init__(self, title=""):
        self.title = title
        self.view, self.storage = make_text_view()
        self._wrap = KK2WrapDelegate.alloc().init()
        layout = self.view.layoutManager()
        layout.setDelegate_(self._wrap)
        objc.setAssociatedObject(layout, _DELEGATE_KEY, self._wrap, objc.OBJC_ASSOCIATION_RETAIN)
        self.tokens = []
        self.kerned_pairs = 0  # adjacent glyph pairs with a non-zero kern in the last render
        self._snapshot = None
        self._vertical = None
        self._pool = {}  # glyph name (MISSING: the missing-glyph box) → [NSTextAttachment, ...]
        self._paths = {}  # glyph name → (source path, scale, scaled path, ink left, ink width)

    def _forget(self):
        self._snapshot = None
        self._vertical = None
        self._pool = {}
        self._paths = {}

    def clear(self):
        """Empties the pane (e.g. while the font is read again)."""
        self.tokens = []
        self.kerned_pairs = 0
        self._wrap.kk2_word_starts = frozenset()
        self.storage.beginEditing()
        self.storage.setAttributedString_(NSAttributedString.alloc().init())
        self.storage.endEditing()

    def warm(self, tokens, snapshot, point_size=48.0, budget_s=0.008):
        """Makes, for about `budget_s` seconds, what a render of `tokens`
        would make for the first time (scaled outlines, attachment cells).
        True once nothing is left: a caller can spread the first render of a
        long sample over several timer ticks and then render in one."""
        if snapshot is not self._snapshot:
            self._forget()
            self._snapshot = snapshot
        upm = _finite(snapshot.upm, 0.0) or 1000.0
        scale = float(point_size) / upm
        deadline = time.perf_counter() + max(0.0, budget_s)
        seen = set()
        for tok in tokens:
            if tok is MISSING or tok == SPACE or tok == NEWLINE or tok in seen:
                continue
            seen.add(tok)
            cached = self._paths.get(tok)
            pool = self._pool.get(tok)
            if cached is not None and cached[1] == scale and pool:
                continue
            info = snapshot.glyph_info(tok)
            if info is None:
                continue
            self._scaled_path(tok, info, scale)
            if not pool:
                self._take(tok, {})
            if time.perf_counter() >= deadline:
                return False
        return True

    def render(self, tokens, snapshot, metrics=None, kern=None, point_size=48.0, color=None):
        """Lays out `tokens` (see kk2_snapshot.tokenize) at `point_size`.

        `metrics(name)` returns (lsb, rsb, advance) in font units for a glyph
        spaced differently from the font, or None to draw it as it is.
        `kern(left_name, right_name)` returns the kerning between two
        adjacent glyphs in font units, left and right as they stand on the
        line (NaN counts as 0). Either may be None: the font as it is, no
        kerning.
        """
        tokens = list(tokens)
        if snapshot is not self._snapshot:
            self._forget()
            self._snapshot = snapshot
        if self._vertical is None:
            self._vertical = vertical_metrics(snapshot)
        self.tokens = tokens
        count = len(tokens)
        upm = _finite(snapshot.upm, 0.0) or 1000.0
        scale = float(point_size) / upm
        ascender, descender, cap_height = self._vertical
        ascent = max(ascender, cap_height) * scale * 1.05
        descent = -descender * scale * 1.05
        color = color or NSColor.textColor()
        line = (ascent + descent) * 1.12

        # each distinct glyph once: name → (path, advance, shift, rtl) in points, None if it cannot be drawn
        glyphs = {}
        for tok in tokens:
            if tok is not MISSING and tok != SPACE and tok != NEWLINE and tok not in glyphs:
                glyphs[tok] = self._glyph(tok, snapshot, metrics, scale)
        chars = [NEWLINE if tok == NEWLINE else SPACE if tok == SPACE else ATTACHMENT for tok in tokens]
        drawn = [chars[i] == ATTACHMENT and glyphs.get(tokens[i]) is not None for i in range(count)]
        rtl = [drawn[i] and glyphs[tokens[i]][3] for i in range(count)]
        word_starts = [0]
        word_starts.extend(i for i in range(1, count) if chars[i - 1] != ATTACHMENT)
        self._wrap.kk2_word_starts = frozenset(word_starts)

        # kerning, in font units, on the cell of each pair's left glyph
        extra = [0.0] * count
        self.kerned_pairs = 0
        if kern is not None:
            values = {}
            for i in range(count - 1):
                if not (drawn[i] and drawn[i + 1]) or rtl[i] != rtl[i + 1]:
                    continue
                # a right-to-left run puts the later glyph on the left
                left, pair = (i + 1, (tokens[i + 1], tokens[i])) if rtl[i] else (i, (tokens[i], tokens[i + 1]))
                k = values.get(pair)
                if k is None:
                    k = values[pair] = _finite(kern(pair[0], pair[1]), 0.0)
                if k:
                    extra[left] = k
                    self.kerned_pairs += 1

        base = {NSParagraphStyleAttributeName: _paragraph_style(line, _LTR), NSFontAttributeName: _SPACE_FONT,
                NSForegroundColorAttributeName: color}
        text = NSMutableAttributedString.alloc().initWithString_attributes_("".join(chars), base)
        text.beginEditing()

        # spaces: the width of the font's space glyph
        space_kern = self._space_width(snapshot, metrics) * scale - _SPACE_ADVANCE
        i = 0
        while i < count:
            if chars[i] == SPACE:
                j = i + 1
                while j < count and chars[j] == SPACE:
                    j += 1
                text.addAttribute_value_range_(NSKernAttributeName, space_kern, (i, j - i))
                i = j
            else:
                i += 1

        # glyph cells: one per distinct (glyph, kern), taken from the pool
        made = {}
        taken = {}
        for i, tok in enumerate(tokens):
            if chars[i] != ATTACHMENT:
                continue
            key = (tok, extra[i]) if drawn[i] else (MISSING, 0.0)
            att = made.get(key)
            if att is None:
                att = self._take(key[0], taken)
                if drawn[i]:
                    path, advance, shift, _ = glyphs[tok]
                    att.attachmentCell().set_glyph(path, advance + extra[i] * scale, shift, ascent, descent, color)
                else:
                    self._set_missing(att, cap_height, upm, scale, ascent, descent)
                made[key] = att
            text.addAttribute_value_range_(NSAttachmentAttributeName, att, (i, 1))
        for name in list(self._pool):
            if taken.get(name):
                del self._pool[name][taken[name]:]
            else:
                del self._pool[name]
        if any(rtl):
            self._set_directions(text, chars, drawn, rtl, line)

        text.endEditing()
        self.storage.beginEditing()
        self.storage.setAttributedString_(text)
        self.storage.endEditing()

    def _set_directions(self, text, chars, drawn, rtl, line):
        """Paragraph directions and per-run overrides for text with RTL glyphs."""
        rtl_para = None
        count = len(chars)
        start = 0
        while start < count:
            end = start
            while end < count and chars[end] != NEWLINE:
                end += 1
            glyphs = [i for i in range(start, end) if drawn[i]]
            if any(rtl[i] for i in glyphs):
                if rtl[glyphs[0]]:
                    if rtl_para is None:
                        rtl_para = _paragraph_style(line, _RTL)
                    text.addAttribute_value_range_(NSParagraphStyleAttributeName, rtl_para,
                                                   (start, min(end + 1, count) - start))
                # a run spans glyphs of one direction and the spaces between them
                run_start = glyphs[0]
                for prev, nxt in zip(glyphs, glyphs[1:] + [None]):
                    if nxt is not None and rtl[nxt] == rtl[prev]:
                        continue
                    direction = (_RTL if rtl[prev] else _LTR) | _OVERRIDE
                    text.addAttribute_value_range_(NSWritingDirectionAttributeName, [direction],
                                                   (run_start, prev + 1 - run_start))
                    run_start = nxt
            start = end + 1

    def _take(self, name, taken):
        """The next attachment of `name`'s pool that this render has not used yet."""
        pool = self._pool.setdefault(name, [])
        n = taken.get(name, 0)
        if n == len(pool):
            att = NSTextAttachment.alloc().init()
            cell = KK2GlyphCell.alloc().init()
            # set here: looking up an attribute a PyObjC object lacks searches
            # its Objective-C methods first (a quarter of a millisecond)
            cell._kk2_key = None
            att.setAttachmentCell_(cell)
            pool.append(att)
        taken[name] = n + 1
        return pool[n]

    def _glyph(self, name, snapshot, metrics, scale):
        """(scaled path, advance, outline shift, rtl) of a glyph in points, or None."""
        info = snapshot.glyph_info(name)
        if info is None:
            return None
        path, ink_left, ink_width = self._scaled_path(name, info, scale)
        lsb, advance = _cell_metrics(info, ink_left, ink_width, metrics(name) if metrics is not None else None)
        return (path, advance * scale, (lsb - ink_left) * scale, bool(getattr(info, "rtl", False)))

    def _scaled_path(self, name, info, scale):
        """(outline scaled into the flipped view, ink left, ink width), cached per glyph and size."""
        source = info.path
        cached = self._paths.get(name)
        if cached is not None and cached[0] is source and cached[1] == scale:
            return cached[2:]
        path, ink_left, ink_width = None, 0.0, 0.0
        if source is not None and source.elementCount():
            bounds = source.bounds()
            ink_left, ink_width = float(bounds.origin.x), float(bounds.size.width)
            flip = NSAffineTransform.transform()
            flip.scaleXBy_yBy_(scale, -scale)
            path = source.copy()
            path.transformUsingAffineTransform_(flip)
        self._paths[name] = (source, scale, path, ink_left, ink_width)
        return path, ink_left, ink_width

    def _space_width(self, snapshot, metrics):
        name = snapshot.char_map.get(" ") or "space"
        info = snapshot.glyph_info(name)
        if info is None:
            return snapshot.upm * 0.25
        m = metrics(name) if metrics is not None else None
        advance = _finite(m[2], None) if m is not None else None
        return advance if advance is not None else _finite(info.width, snapshot.upm * 0.25)

    def _set_missing(self, att, cap_height, upm, scale, ascent, descent):
        """Makes `att` the outlined half-em box that stands in for a missing glyph."""
        width = upm * 0.5 * scale
        h = cap_height * scale
        box = NSBezierPath.bezierPathWithRect_(NSMakeRect(width * 0.1, -h, width * 0.8, h))
        box.appendBezierPathWithRect_(NSMakeRect(width * 0.1 + 1.0, -h + 1.0, width * 0.8 - 2.0, h - 2.0))
        box.setWindingRule_(1)  # even-odd: an outlined box
        color = NSColor.tertiaryLabelColor() if hasattr(NSColor, "tertiaryLabelColor") else NSColor.grayColor()
        att.attachmentCell().set_glyph(box, width, 0.0, ascent, descent, color)


def _cell_metrics(info, ink_left, ink_width, m):
    """(lsb, advance) of a glyph cell in font units.

    The glyph's own values unless `m` = metrics(name) gives new ones; a
    missing new advance follows from the new sidebearings. The outline is
    always drawn with its ink at the lsb, so a GlyphInfo whose metrics were
    refreshed without a new path still draws in the right place.
    """
    lsb = _finite(getattr(info, "lsb", None), ink_left)
    advance = _finite(getattr(info, "width", None), 0.0)
    if m is None:
        return lsb, advance
    new_lsb, new_rsb, new_advance = (_finite(v, None) for v in m)
    if new_lsb is None:
        new_lsb = lsb
    if new_advance is None:
        if new_rsb is not None and ink_width > 0.0:
            new_advance = new_lsb + ink_width + new_rsb
        else:
            new_advance = advance + (new_lsb - lsb)
    return new_lsb, new_advance
