# encoding: utf-8
"""
kk2_snapshot — one UFO font (its default layer), read into engine inputs.

RoboFont's counterpart of the Glyphs plugin's kk2_snapshot: the same
Snapshot, GlyphInfo and SnapshotReader, so the engine, the proof panes, the
designer harness and the windows see a font the same way in both apps.

Main thread only: the font is copied into kk2_bridge.GlyphSpec objects here
and the engine only ever sees those copies. Reading takes up to about a
second for a large font, so a SnapshotReader reads in slices of a few
milliseconds, driven by the window's timer:

  Pass 1  lists every glyph cheaply (export, name, code points, category;
          the component names of glyphs outside the spacing set).
  Pass 2  reads each glyph of the spacing set (outline and properties) and
          adds its GlyphSpec to the InputPacker as it goes.
  Pass 3  resolves what refers to other glyphs by name (composite bases,
          metrics keys, aligned composites, tabular figures) into engine
          indices; the targets may have been read after the glyph itself.

Outlines are drawn through a pen that decomposes components with their
transforms (fontTools' BasePen over the layer), so accented and composite
glyphs need no special handling; curves go to the engine as cubics (a
TrueType quadratic is exactly a cubic). Sidebearings are measured on the ink
of that outline: its exact bounds, curve extrema included (ink_x: from the
points, solving a curve for its extremes only where it reaches past its
on-curve points; the same numbers as the pen to the last bit, several times
faster, which Apply and Revert need: they measure every glyph they move).
GlyphInfo.path is the same outline as an NSBezierPath (inside RoboFont) for
the proof panes and the windows to draw.

A ligature built from components is spaced as one shape (Glyphs would align
its components; RoboFont keeps them where the designer put them).

What Glyphs knows from its glyph database, a UFO does not: a glyph's
category, case and script come from its code point (or, for an unencoded
glyph, from the code point of the name it extends: a.sc → a, f_f_i → f) and
the OpenType categories the font declares (public.openTypeCategories).
Kerning groups are the font's public.kern1 (a glyph's right side) and
public.kern2 groups (its left side). Metrics keys are honoured where a UFO
carries Glyphs' (exported by glyphsLib). An accented glyph built the way
Glyphs aligns one automatically — its base at the origin, marks on it, the
base's advance — is spaced as Glyphs spaces an aligned composite: its sides
follow its base.
"""

from __future__ import division, print_function, unicode_literals

import bisect
import math
import re
import time
import unicodedata

import kk2_bridge as kb

try:
    from fontTools.agl import toUnicode as _agl_to_unicode
except ImportError:  # no fontTools: code points only
    _agl_to_unicode = None
try:
    from fontTools.misc.bezierTools import calcCubicBounds
    from fontTools.misc.transform import Transform
    from fontTools.pens.basePen import BasePen, decomposeQuadraticSegment
    from fontTools.pens.pointPen import PointToSegmentPen
    from fontTools.pens.transformPen import TransformPen
except ImportError:  # RoboFont always has fontTools; tools without it cannot read fonts
    calcCubicBounds = BasePen = decomposeQuadraticSegment = PointToSegmentPen = Transform = TransformPen = None

# Glyphs' case values (GSUppercase …), kept so the modules shared with the
# Glyphs plugin read GlyphInfo.case the same way
GSUppercase, GSLowercase, GSSmallcaps, GSMinor = 1, 2, 3, 4

SPACING_CATEGORIES = ("Letter", "Number", "Punctuation", "Symbol")
UNKERNED_CATEGORIES = ("Mark", "Separator")
TABULAR_SUFFIXES = (".tf", ".tosf", ".tnum")
# A font that leans this much or more (its italic angle) is measured along
# its slant: below it, as upright (Spacing QA's rule for a measured slant).
SLANT_MIN_DEGREES = 3.0
# ...and less than this: a larger italic angle is an error in the font, not a
# slant to measure along (Spacing QA's rule too)
SLANT_MAX_DEGREES = 60.0


def slant_measurable(degrees):
    """An italic angle the glyphs are measured along: 3° to under 60°."""
    return SLANT_MIN_DEGREES <= abs(degrees) < SLANT_MAX_DEGREES


# The GF Latin Kernel as Spacing QA checks it (crates/spacingqa/src/glyphset.rs):
# A–Z, a–z and 14 punctuation marks fit the Looseness; they and these symbols
# are compared. The slant probe reads these glyphs only.
KERNEL_FIT = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.,:;!?-'\"()/&\u2019"
KERNEL_SCORED = "%*@[\\]`{|}\u00a9\u00ae\u00b0\u00b7\u2013\u2014\u2018\u201c\u201d\u2022\u2026\u2122"
SMALLCAP_SUFFIXES = ("sc", "smcp", "c2sc")
DEFAULT_FIGURES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")

SKIP_EXPORT_KEY = "public.skipExportGlyphs"
OT_CATEGORIES_KEY = "public.openTypeCategories"
KERN1_PREFIX = "public.kern1."  # a glyph's right side: the group used when it stands on the left
KERN2_PREFIX = "public.kern2."  # a glyph's left side
# Glyphs' own data in a UFO exported by glyphsLib
GLYPHS_PREFIX = "com.schriftgestaltung.Glyphs."
COMPONENT_INFO_KEY = GLYPHS_PREFIX + "ComponentInfo"
ALIGNMENT_DISABLED = -1

# Unicode blocks of symbols that are spaced but never kerned (as tools/kk2_fonts.py)
TECHNICAL_BLOCKS = (
    (0x2190, 0x21FF), (0x2200, 0x22FF), (0x2300, 0x23FF), (0x2400, 0x243F), (0x2440, 0x245F), (0x2460, 0x24FF),
    (0x2500, 0x257F), (0x2580, 0x259F), (0x25A0, 0x25FF), (0x2600, 0x26FF), (0x2700, 0x27BF), (0x27C0, 0x27EF),
    (0x27F0, 0x27FF), (0x2800, 0x28FF), (0x2900, 0x297F), (0x2980, 0x29FF), (0x2A00, 0x2AFF), (0x2B00, 0x2BFF),
    (0xE000, 0xF8FF), (0x1F000, 0x1FAFF),
)

# Glyphs whose ink is meant to touch or cross the advance edges keep their
# sidebearings (both sides ruled fixed): the letters of right-to-left and of
# joining scripts (Arabic and Syriac letters connect, the headline of
# Devanagari, Bengali and Gurmukhi runs through) and the box-drawing and
# block-element characters. The optical model spaces separate shapes. Joining
# scripts' glyphs are not kerned either (right-to-left glyphs never are in
# left-to-right order; box drawing is a technical block).
JOINING_SCRIPTS = frozenset(("Mong", "Phag", "Deva", "Beng", "Guru", "Sylo", "Tirh"))
EDGE_TO_EDGE_BLOCKS = ((0x2500, 0x257F), (0x2580, 0x259F))

# Script by block (ISO 15924): the letters, marks and digits of a block, and
# everything in the blocks of right-to-left scripts (their punctuation is used
# in their text). Other characters, and the few below, are Common.
_SCRIPT_BLOCKS = (
    (0x0041, 0x007A, "Latn"), (0x00AA, 0x00AA, "Latn"), (0x00BA, 0x00BA, "Latn"), (0x00C0, 0x02B8, "Latn"),
    (0x02E0, 0x02E4, "Latn"), (0x0370, 0x0373, "Grek"), (0x0375, 0x03E1, "Grek"), (0x03E2, 0x03EF, "Copt"),
    (0x03F0, 0x03FF, "Grek"), (0x0400, 0x052F, "Cyrl"), (0x0530, 0x058F, "Armn"), (0x0590, 0x05FF, "Hebr"),
    (0x0600, 0x06FF, "Arab"), (0x0700, 0x074F, "Syrc"), (0x0750, 0x077F, "Arab"), (0x0780, 0x07BF, "Thaa"),
    (0x07C0, 0x07FF, "Nkoo"), (0x0800, 0x083F, "Samr"), (0x0840, 0x085F, "Mand"), (0x0860, 0x086F, "Syrc"),
    (0x0870, 0x08FF, "Arab"), (0x0900, 0x097F, "Deva"), (0x0980, 0x09FF, "Beng"), (0x0A00, 0x0A7F, "Guru"),
    (0x0A80, 0x0AFF, "Gujr"), (0x0B00, 0x0B7F, "Orya"), (0x0B80, 0x0BFF, "Taml"), (0x0C00, 0x0C7F, "Telu"),
    (0x0C80, 0x0CFF, "Knda"), (0x0D00, 0x0D7F, "Mlym"), (0x0D80, 0x0DFF, "Sinh"), (0x0E00, 0x0E7F, "Thai"),
    (0x0E80, 0x0EFF, "Laoo"), (0x0F00, 0x0FFF, "Tibt"), (0x1000, 0x109F, "Mymr"), (0x10A0, 0x10FF, "Geor"),
    (0x1100, 0x11FF, "Hang"), (0x1200, 0x139F, "Ethi"), (0x13A0, 0x13FF, "Cher"), (0x1780, 0x17FF, "Khmr"),
    (0x1800, 0x18AF, "Mong"), (0x1C80, 0x1C8F, "Cyrl"), (0x1C90, 0x1CBF, "Geor"), (0x1D00, 0x1D25, "Latn"),
    (0x1D26, 0x1D2A, "Grek"), (0x1D2B, 0x1D2B, "Cyrl"), (0x1D2C, 0x1D5C, "Latn"), (0x1D5D, 0x1D61, "Grek"),
    (0x1D62, 0x1D65, "Latn"), (0x1D66, 0x1D6A, "Grek"), (0x1D6B, 0x1D77, "Latn"), (0x1D78, 0x1D78, "Cyrl"),
    (0x1D79, 0x1DBE, "Latn"), (0x1DBF, 0x1DBF, "Grek"), (0x1E00, 0x1EFF, "Latn"), (0x1F00, 0x1FFF, "Grek"),
    (0x2071, 0x2071, "Latn"), (0x207F, 0x207F, "Latn"), (0x2090, 0x209C, "Latn"), (0x2126, 0x2126, "Grek"),
    (0x212A, 0x212B, "Latn"), (0x2132, 0x2132, "Latn"), (0x214E, 0x214E, "Latn"), (0x2160, 0x2188, "Latn"),
    (0x2C00, 0x2C5F, "Glag"), (0x2C60, 0x2C7F, "Latn"), (0x2C80, 0x2CFF, "Copt"), (0x2D00, 0x2D2F, "Geor"),
    (0x2D30, 0x2D7F, "Tfng"), (0x2DE0, 0x2DFF, "Cyrl"), (0x3040, 0x309F, "Hira"), (0x30A0, 0x30FF, "Kana"),
    (0x3100, 0x312F, "Bopo"), (0x3130, 0x318F, "Hang"), (0x3400, 0x4DBF, "Hani"), (0x4E00, 0x9FFF, "Hani"),
    (0xA640, 0xA69F, "Cyrl"), (0xA722, 0xA787, "Latn"), (0xA78B, 0xA7FF, "Latn"), (0xAB30, 0xAB64, "Latn"),
    (0xAB65, 0xAB65, "Grek"), (0xAB66, 0xAB6F, "Latn"), (0xAC00, 0xD7AF, "Hang"), (0xFB00, 0xFB06, "Latn"),
    (0xFB13, 0xFB17, "Armn"), (0xFB1D, 0xFB4F, "Hebr"), (0xFB50, 0xFDFF, "Arab"), (0xFE70, 0xFEFE, "Arab"),
    (0xFF21, 0xFF3A, "Latn"), (0xFF41, 0xFF5A, "Latn"),
)
_SCRIPT_STARTS = [b[0] for b in _SCRIPT_BLOCKS]
_COMMON_IN_BLOCKS = frozenset((0x0605, 0x060C, 0x061B, 0x061F, 0x0640, 0x06DD, 0x08E2, 0xFD3E, 0xFD3F))

NEWLINE = "\n"
SPACE = " "
MISSING = None
_NEWLINES = "\r\n  "
_SPACES = " \t "
_NAME_ENDS = "/" + _SPACES + _NEWLINES  # what ends a "/name" escape

_NAN = float("nan")
_ALIGN_TOLERANCE = 0.5  # font units: a component "at" a position


def _ranges(*spans):
    out = set()
    for span in spans:
        a, b = (span, span) if isinstance(span, int) else span
        out.update(range(a, b + 1))
    return frozenset(out)


# Where Glyphs' glyph database departs from the Unicode categories (Latin,
# Greek, Cyrillic, punctuation and symbols), so a UFO's glyphs are spaced and
# kerned as the Glyphs plugin spaces the same glyphs: spacing accents and
# modifier marks are marks; % & @ § ¶ † ‡ ‰ ′ ″, µ and the letterlike signs are
# symbols; the fraction slash is a figure; format characters are separators.
_GLYPHS_MARK = _ranges(0xA8, 0xAF, 0xB4, 0xB8, 0x60, (0x2B9, 0x2C1), 0x2C6, 0x2C7, (0x2C8, 0x2D1), (0x2D8, 0x2DD),
                       0x2EA, 0x2EB, 0x384, 0x385, 0x559, 0x1FBD, (0x1FBF, 0x1FC1), (0x1FCD, 0x1FCF),
                       (0x1FDD, 0x1FDF), (0x1FED, 0x1FEF), 0x1FFD, 0x1FFE)
_GLYPHS_SYMBOL = _ranges(0x25, 0x26, 0x40, 0xA7, 0xB6, 0xB5, (0x2020, 0x2021), (0x2030, 0x2033), 0x2113, 0x2126,
                         0x212A, 0x212B, (0x2135, 0x2139), 0x2E0, 0x2E4, 0x2EC, 0x2EE, 0x374)
_GLYPHS_NUMBER = _ranges(0x2044)
_GLYPHS_SEPARATOR = _ranges(0x0D, 0x7F, (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064), (0x2066, 0x206F))
_GLYPHS_PUNCTUATION = _ranges(0xAD)


# ---------------------------------------------------------------- helpers
def rhythm_group(info):
    """Engine rhythm group of a glyph (uppercase, lowercase, figures, small
    caps, other) from its GlyphInfo."""
    category = info.category
    if category == "Number":
        return kb.GROUP_FIGURES
    if category != "Letter":
        return kb.GROUP_OTHER
    if info.case == GSSmallcaps:
        return kb.GROUP_SMALLCAPS
    if info.case == GSUppercase:
        return kb.GROUP_UPPERCASE
    if info.case in (GSLowercase, GSMinor):
        return kb.GROUP_LOWERCASE
    return kb.GROUP_OTHER


ZONE_SMALLCAP_SUFFIXES = ("sc", "smcp", "c2sc")


def zone_reference(name, codepoint, flags_codepoint, category):
    """True for a base letter, whose extents set its group's spacing zone
    (kb.GLYPH_ZONE): an encoded letter without a mark or any other
    decomposition (a–z, A–Z, æ, ø, ß, а–я, α–ω…), or the small cap of one
    (a.sc). Accented letters reach above or below and outnumber the base
    letters in most fonts: counted, they lift the lowercase zone to accent
    height, where an f's hook decides its right side. Ligatures and other
    alternates are left out too."""
    if category != "Letter":
        return False
    cp = codepoint
    if cp is None:
        suffix = name.partition(".")[2].split(".")[0]
        if "_" in name or suffix not in ZONE_SMALLCAP_SUFFIXES:
            return False
        cp = flags_codepoint
    if cp is None:
        return False
    try:
        return unicodedata.decomposition(chr(cp)) == ""
    except (ValueError, OverflowError):
        return False


class _NodePen(BasePen if BasePen is not None else object):
    """Records a glyph's closed contours, components decomposed, as the
    engine's nodes: (x, y, kind) with kind 0 on-curve after a line, 1 after a
    curve, 2 off-curve. Open contours are not ink and are dropped."""

    def __init__(self, glyph_set):
        BasePen.__init__(self, glyph_set)
        self.contours = []
        self._current = None

    def _moveTo(self, pt):
        self._current = [(float(pt[0]), float(pt[1]), 0)]  # the start node's kind is settled at closePath

    def _lineTo(self, pt):
        if self._current is not None:
            self._current.append((float(pt[0]), float(pt[1]), 0))

    def _curveToOne(self, pt1, pt2, pt3):
        if self._current is not None:
            self._current.extend([(float(pt1[0]), float(pt1[1]), 2), (float(pt2[0]), float(pt2[1]), 2),
                                  (float(pt3[0]), float(pt3[1]), 1)])

    def _closePath(self):
        current, self._current = self._current, None
        if not current:
            return
        if len(current) > 1 and current[-1][0] == current[0][0] and current[-1][1] == current[0][1]:
            # the last segment ends on the start point: it is the closing segment
            last = current.pop()
            current[0] = (current[0][0], current[0][1], last[2])
        if len(current) >= 2:
            self.contours.append(current)

    def _endPath(self):
        self._current = None  # an open contour: not ink


def draw_contours(glyph, glyph_set):
    """A glyph's closed contours with its components decomposed (engine nodes,
    see _NodePen). A component that refers back to itself raises
    RecursionError (the glyph cannot be read)."""
    pen = _NodePen(glyph_set)
    glyph.draw(pen)
    return pen.contours


def contour_bounds(contours):
    """(xMin, yMin, xMax, yMax) of the ink of engine contours, curve extrema
    included; None without ink."""
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    for contour in contours:
        n = len(contour)
        for k, (x, y, kind) in enumerate(contour):
            if kind == 2:
                continue  # off-curve: its curve is measured from the on-curve point after it
            if x < x0:
                x0 = x
            if x > x1:
                x1 = x
            if y < y0:
                y0 = y
            if y > y1:
                y1 = y
            if kind == 1 and n >= 4:
                # the curve that ends here: (on, off, off, on), wrapping around the start
                p0 = contour[(k - 3) % n]
                c1 = contour[(k - 2) % n]
                c2 = contour[(k - 1) % n]
                bx0, by0, bx1, by1 = calcCubicBounds(p0[:2], c1[:2], c2[:2], (x, y))
                x0, y0 = min(x0, bx0), min(y0, by0)
                x1, y1 = max(x1, bx1), max(y1, by1)
    if x0 > x1:
        return None
    return (x0, y0, x1, y1)


def shear_nodes(contours, slant, pivot):
    """Engine contours sheared upright: x − (y − pivot) · slant (slant = tan
    of the lean, + to the right). A shear keeps every height and every
    horizontal distance at a height; what the engine measures besides (disks,
    the distance field) it measures as on an upright design."""
    return [[(x - (y - pivot) * slant, y, kind) for (x, y, kind) in contour] for contour in contours]


def bezier_path(contours):
    """Engine contours as an NSBezierPath (inside RoboFont), else None."""
    try:
        from AppKit import NSBezierPath
    except ImportError:
        return None
    path = NSBezierPath.bezierPath()
    for contour in contours:
        n = len(contour)
        if n < 2:
            continue
        # start on an on-curve node
        start = next((k for k, node in enumerate(contour) if node[2] != 2), None)
        if start is None:
            continue
        nodes = contour[start:] + contour[:start]
        path.moveToPoint_((nodes[0][0], nodes[0][1]))
        k = 1
        while k <= n:
            node = nodes[k % n]
            if node[2] == 2:
                c1, c2, p = nodes[k % n], nodes[(k + 1) % n], nodes[(k + 2) % n]
                path.curveToPoint_controlPoint1_controlPoint2_((p[0], p[1]), (c1[0], c1[1]), (c2[0], c2[1]))
                k += 3
            else:
                if k < n:
                    path.lineToPoint_((node[0], node[1]))
                k += 1
        path.closePath()
    return path


def is_tabular(name):
    """Tabular by name: a .tf / .tosf / .tnum suffix, also before further
    suffixes (zero.tf, one.tosf.ss01)."""
    return any("." + part in TABULAR_SUFFIXES for part in name.split(".")[1:])


def technical(codepoint):
    """True for characters of the TECHNICAL_BLOCKS (spaced, never kerned)."""
    if codepoint is None:
        return False
    return any(a <= codepoint <= b for a, b in TECHNICAL_BLOCKS)


def keeps_sidebearings(info, codepoint):
    """True for a glyph whose sidebearings Kinetikern2 must not change (see
    JOINING_SCRIPTS): right-to-left, a joining script, or box drawing."""
    if info.rtl or script_iso(info.script) in JOINING_SCRIPTS:
        return True
    return codepoint is not None and any(a <= codepoint <= b for a, b in EDGE_TO_EDGE_BLOCKS)


def unicode_script(codepoint):
    """ISO 15924 code of a character by its block (see _SCRIPT_BLOCKS); ""
    for Common."""
    if codepoint is None or codepoint in _COMMON_IN_BLOCKS:
        return ""
    k = bisect.bisect_right(_SCRIPT_STARTS, codepoint) - 1
    if k < 0 or codepoint > _SCRIPT_BLOCKS[k][1]:
        return ""
    script = _SCRIPT_BLOCKS[k][2]
    if script in kb.RTL_SCRIPTS:
        return script
    try:
        return script if unicodedata.category(chr(codepoint))[0] in "LMN" else ""
    except (ValueError, OverflowError):
        return ""


def unicode_script_block(codepoint):
    """The script of the block a code point lies in (see _SCRIPT_BLOCKS),
    whatever its category; "" outside them."""
    k = bisect.bisect_right(_SCRIPT_STARTS, codepoint) - 1
    if k < 0 or codepoint > _SCRIPT_BLOCKS[k][1]:
        return ""
    return _SCRIPT_BLOCKS[k][2]


def script_iso(code):
    """kk2_bridge.script_code() back to its four letters ("" for Common)."""
    if not code:
        return ""
    return "".join(chr((code >> s) & 0xFF) for s in (24, 16, 8, 0)).strip()


def name_codepoint(name):
    """The code point a glyph name stands for: "A", "uni0041", "u1F600",
    the first character of a ligature ("f_f_i" → f) and the name a suffix
    extends ("a.sc" → a); None if the name says nothing."""
    base = name.split(".")[0] if not name.startswith(".") else name
    if not base:
        return None
    first = base.split("_")[0] if "_" in base and not base.startswith("_") else base
    for candidate in (base, first):
        text = None
        if _agl_to_unicode is not None:
            try:
                text = _agl_to_unicode(candidate)
            except Exception:
                text = None
        if text:
            return ord(text[0])
        m = re.match(r"^(?:uni([0-9A-Fa-f]{4})|u([0-9A-Fa-f]{4,6}))$", candidate)
        if m:
            cp = int(m.group(1) or m.group(2), 16)
            if 0 <= cp <= 0x10FFFF:
                return cp
    return None


def glyph_category(name, codepoint, ot_category=None):
    """(category, subcategory, case) as Glyphs' glyph database would give
    them, from the glyph's code point (its own, else that of the name it
    extends) and the OpenType category the font declares for it."""
    if ot_category == "mark":
        return "Mark", "Nonspacing", 0
    if codepoint is None:
        return None, None, 0
    try:
        uc = unicodedata.category(chr(codepoint))
    except (ValueError, OverflowError):
        return None, None, 0
    if codepoint in _GLYPHS_MARK:
        return "Mark", "Spacing" if uc == "Sk" else "Modifier", 0
    if codepoint in _GLYPHS_SYMBOL:
        return "Symbol", None, 0
    if codepoint in _GLYPHS_NUMBER:
        return "Number", "Fraction", 0
    if codepoint in _GLYPHS_SEPARATOR:
        return "Separator", None, 0
    if codepoint in _GLYPHS_PUNCTUATION:
        return "Punctuation", None, 0
    if uc == "Cn" and unicode_script_block(codepoint):
        uc = "Lo"  # newer than this Python's Unicode data: a letter of the block's script
    suffixes = name.split(".")[1:]
    smallcap = any(s in SMALLCAP_SUFFIXES for s in suffixes)
    major = uc[0]
    if major == "L":
        if smallcap:
            return "Letter", "Smallcaps", GSSmallcaps
        if uc in ("Lu", "Lt"):
            return "Letter", "Uppercase", GSUppercase
        if uc == "Ll":
            return "Letter", "Lowercase", GSLowercase
        if uc == "Lm":
            return "Letter", "Modifier", 0
        return "Letter", None, 0
    if major == "N":
        try:
            uname = unicodedata.name(chr(codepoint), "")
        except ValueError:
            uname = ""
        if "FRACTION" in uname:
            return "Number", "Fraction", 0
        return "Number", "Decimal Digit" if uc == "Nd" else None, 0
    if major == "P":
        sub = {"Pd": "Dash", "Ps": "Parenthesis", "Pe": "Parenthesis", "Pi": "Quote", "Pf": "Quote"}.get(uc)
        return "Punctuation", sub, 0
    if major == "S":
        sub = {"Sm": "Math", "Sc": "Currency", "Sk": "Modifier"}.get(uc)
        return "Symbol", sub, 0
    if major == "M":
        return "Mark", "Nonspacing" if uc == "Mn" else "Spacing Combining", 0
    if major == "Z":
        return "Separator", "Space", 0
    return None, None, 0


_OFFSET = re.compile(r"^(.*?)\s*([+-])\s*(\d+(?:\.\d*)?|\.\d+)$")
_NUMBER = re.compile(r"^(\d+(?:\.\d*)?|\.\d+)$")
_NOT_IN_NAMES = re.compile(r"[\s*/()=|+,]")


def parse_metrics_key(key):
    """A Glyphs metrics key → None (no key), ("follow", target, opposite,
    offset) or ("fixed",).

    "=X", "=X+n" and "=X-n" follow glyph X's same side, "=|X" and "=|X+n" its
    opposite side, and "=|" alone the glyph's own opposite side (target "").
    The leading "=" signs do not matter (Glyphs stores "X", "=X" and "==X").
    Anything else (a number, "*", another formula) is a rule the engine
    cannot evaluate: the side keeps its current value.
    """
    if key is None:
        return None
    key = str(key).strip()
    if not key:
        return None
    body = key.lstrip("=").strip()
    opposite = body.startswith("|")
    if opposite:
        body = body[1:].strip()
    offset = 0.0
    m = _OFFSET.match(body)
    if m:
        body, offset = m.group(1).strip(), float(m.group(2) + m.group(3))
    if not body:
        return ("follow", "", True, offset) if opposite else ("fixed",)
    if _NUMBER.match(body) or _NOT_IN_NAMES.search(body):
        return ("fixed",)
    return ("follow", body, opposite, offset)


def _string(value):
    """A property as str, None when empty."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _number(value, fallback=_NAN):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    return v if v == v and abs(v) < 1e7 else fallback


def _codepoints(glyph):
    out = []
    for u in getattr(glyph, "unicodes", None) or ():
        try:
            cp = int(u)
        except (TypeError, ValueError):
            continue
        if 0 <= cp <= 0x10FFFF:
            out.append(cp)
    return out


def _metrics_key(lib, side):
    """A glyph's metrics key for `side` ("left", "right", "width") from the
    data glyphsLib keeps in its lib: the layer's key wins over the glyph's."""
    if not lib:
        return None
    for scope in ("layer.", "glyph."):
        value = _string(lib.get(GLYPHS_PREFIX + scope + side + "MetricsKey"))
        if value:
            return value
    return None


def _component_alignment_off(lib):
    """True when Glyphs' data say a component of the glyph is not aligned
    automatically (glyphsLib's ComponentInfo, alignment −1)."""
    info = lib.get(COMPONENT_INFO_KEY) if lib else None
    if not info:
        return False
    try:
        return any(int(c.get("alignment", 0)) == ALIGNMENT_DISABLED for c in info)
    except Exception:
        return False


def kerning_group_maps(groups):
    """{glyph: group} for each side from a font's groups: (left side —
    public.kern2, right side — public.kern1), group names without their
    prefix; and the glyphs found in more than one group of a side."""
    left, right = {}, {}
    doubles = []
    for group_name in sorted(groups.keys()):
        name = str(group_name)
        if name.startswith(KERN2_PREFIX):
            table, short = left, name[len(KERN2_PREFIX):]
        elif name.startswith(KERN1_PREFIX):
            table, short = right, name[len(KERN1_PREFIX):]
        else:
            continue
        for member in groups[group_name]:
            member = str(member)
            if member in table:
                doubles.append(member)  # a UFO allows one group per side: the first is used
                continue
            table[member] = short
    return left, right, doubles


# ------------------------------------------------------------- glyph info
class GlyphInfo(object):
    """One glyph of the snapshot's font, as the font has it now.

    `lsb` / `rsb` / `bounds` are measured on the ink of the decomposed
    outline, the frame the engine and the proofs work in; `font_lsb` /
    `font_rsb` are the same (a UFO has no other measure: in Glyphs they are
    layer.LSB / RSB). `contours` are the engine's nodes, `path` the same
    outline as an NSBezierPath (None outside RoboFont).
    """

    __slots__ = ("name", "glyph_id", "unicode", "char", "path", "contours", "width", "lsb", "rsb", "bounds",
                 "empty", "font_lsb", "font_rsb", "category", "subcategory", "case", "script", "rtl", "kern",
                 "left_group", "right_group", "left_key", "right_key", "width_key", "components", "aligned")

    @property
    def advance(self):
        return self.width


def read_glyph_info(glyph, layer, name, codepoint, flags_codepoint, category, left_group, right_group,
                    spacing=True):
    """Reads one glyph's GlyphInfo. `codepoint`: its own first code point
    (None for an unencoded glyph); `flags_codepoint`: the code point that
    decides script and kerning (its own, else that of the name it extends);
    `category`: (category, subcategory, case); `spacing`: the glyph takes
    part in spacing (else `kern` is False)."""
    info = GlyphInfo()
    info.name = name
    info.glyph_id = None
    info.unicode = codepoint
    info.char = chr(codepoint) if codepoint is not None else None
    info.category, info.subcategory, info.case = category
    contours = draw_contours(glyph, layer)
    info.contours = contours
    info.path = bezier_path(contours)
    info.width = _number(glyph.width, 0.0)
    bounds = contour_bounds(contours)
    if bounds is not None:
        x0, y0, x1, y1 = bounds
        extent = ink_x(glyph, layer)  # the sides exactly as Apply and Revert measure them
        if extent is not None:
            x0, x1 = extent
        info.bounds = (x0, y0, x1 - x0, y1 - y0)
        info.lsb = x0
        info.rsb = info.width - x1
        info.empty = False
    else:
        info.bounds = (0.0, 0.0, 0.0, 0.0)
        info.lsb = info.rsb = 0.0
        info.empty = True
    info.font_lsb, info.font_rsb = info.lsb, info.rsb
    info.script = kb.script_code(unicode_script(flags_codepoint))
    info.rtl = script_iso(info.script) in kb.RTL_SCRIPTS
    # a joining script's glyphs (their headline or strokes run through the
    # advance edges by design) keep their sidebearings and are not kerned:
    # the model's clearance would push them apart
    info.kern = (bool(spacing) and info.category not in UNKERNED_CATEGORIES and not technical(flags_codepoint)
                 and script_iso(info.script) not in JOINING_SCRIPTS)
    info.left_group = left_group
    info.right_group = right_group
    lib = getattr(glyph, "lib", None)
    info.left_key = _metrics_key(lib, "left")
    info.right_key = _metrics_key(lib, "right")
    info.width_key = _metrics_key(lib, "width")
    info.components = tuple(c.baseGlyph for c in glyph.components if c.baseGlyph)
    info.aligned = False  # decided in pass 3, once the components are read
    return info


_TWO_THIRDS = 0.66666666666666667  # fontTools' BasePen raises a quadratic to a cubic with this


def _segment_x(x0, y0, offs, x3, y3, kind):
    """(xMin, xMax) of the curve from (x0, y0) through the off-curve points
    `offs` to (x3, y3), extrema included, with the arithmetic of the pen
    path (a quadratic split at its implied on-curve points and raised to
    cubics, then calcCubicBounds); None for a kind of segment this does not
    measure (the caller draws that contour through the pen)."""
    if kind == "curve" and len(offs) == 2:
        b = calcCubicBounds((x0, y0), offs[0], offs[1], (x3, y3))
        return b[0], b[2]
    if kind == "qcurve" or (kind == "curve" and len(offs) == 1):
        lo = hi = None
        for (qx, qy), (x2, y2) in decomposeQuadraticSegment(list(offs) + [(x3, y3)]):
            c1 = (x0 + _TWO_THIRDS * (qx - x0), y0 + _TWO_THIRDS * (qy - y0))
            c2 = (x2 + _TWO_THIRDS * (qx - x2), y2 + _TWO_THIRDS * (qy - y2))
            b = calcCubicBounds((x0, y0), c1, c2, (x2, y2))
            lo = b[0] if lo is None or b[0] < lo else lo
            hi = b[2] if hi is None or b[2] > hi else hi
            x0, y0 = x2, y2
        return lo, hi
    return None


_PEN = object()  # _contour_x: measure this contour with the pen


def _contour_x(contour, dx, dy):
    """(xMin, xMax) of a contour's ink moved by (dx, dy); None if it is no
    ink (open, or fewer than two nodes), _PEN for a contour this does not
    walk. Only the curves whose off-curve points reach past the on-curve
    points are solved for their extrema: a curve stays inside its control
    points."""
    points = getattr(contour, "_points", None)
    if points is None:
        points = list(contour)
    n = len(points)
    if n < 2:
        return None  # no point, or one: no node or one, not ink
    kinds = [p.segmentType for p in points]
    if kinds[0] == "move":
        return None  # open: not ink
    xs = [p.x + dx for p in points]
    on = [x for x, k in zip(xs, kinds) if k is not None]
    if not on:
        return _PEN  # a TrueType contour of off-curve points only
    lo, hi = min(on), max(on)
    if len(on) == n:
        if n == 2 and xs[0] == xs[1] and points[0].y == points[1].y:
            return None  # one node once the closing point is dropped
        return lo, hi
    if min(xs) >= lo and max(xs) <= hi:
        return lo, hi
    start = next(i for i, k in enumerate(kinds) if k is not None)
    prev = start
    offs = []
    for step in range(1, n + 1):
        k = (start + step) % n
        kind = kinds[k]
        if kind is None:
            offs.append(k)
            continue
        if offs and (min(xs[j] for j in offs) < lo or max(xs[j] for j in offs) > hi):
            seg = _segment_x(xs[prev], points[prev].y + dy, [(xs[j], points[j].y + dy) for j in offs],
                             xs[k], points[k].y + dy, kind)
            if seg is None:
                return _PEN
            lo, hi = min(lo, seg[0]), max(hi, seg[1])
        offs = []
        prev = k
    return lo, hi


def _pen_x(glyph, layer, transformation):
    """(xMin, xMax) of a glyph drawn through the pen with `transformation`."""
    pen = _NodePen(layer)
    glyph.draw(TransformPen(pen, transformation))
    b = contour_bounds(pen.contours)
    return (b[0], b[2]) if b is not None else None


def _glyph_x(glyph, layer, dx, dy, depth):
    lo = hi = None
    for contour in glyph:
        e = _contour_x(contour, dx, dy)
        if e is _PEN:
            pen = _NodePen(layer)
            contour.drawPoints(PointToSegmentPen(TransformPen(pen, (1, 0, 0, 1, dx, dy))))
            b = contour_bounds(pen.contours)
            e = (b[0], b[2]) if b is not None else None
        if e is not None:
            lo = e[0] if lo is None or e[0] < lo else lo
            hi = e[1] if hi is None or e[1] > hi else hi
    for component in glyph.components:
        name = component.baseGlyph
        if not name or name not in layer:
            continue  # the pen path skips a missing base glyph too
        if depth > 100:
            raise RecursionError("component loop at %s" % name)
        t = component.transformation
        if t[0] == 1 and t[1] == 0 and t[2] == 0 and t[3] == 1:
            e = _glyph_x(layer[name], layer, dx + t[4], dy + t[5], depth + 1)
        else:
            e = _pen_x(layer[name], layer, Transform(1, 0, 0, 1, dx, dy).transform(t))
        if e is not None:
            lo = e[0] if lo is None or e[0] < lo else lo
            hi = e[1] if hi is None or e[1] > hi else hi
    return (lo, hi) if lo is not None else None


def ink_x(glyph, layer):
    """(xMin, xMax) of a glyph's ink — its closed contours, components
    decomposed — or None without ink: what contour_bounds(draw_contours())
    measures, to the last bit, but walking the points (the pen only for a
    scaled or rotated component and unusual contours). Read fresh, never
    from caches a held notification has not refreshed yet."""
    return _glyph_x(glyph, layer, 0, 0, 0)


def ink_metrics(glyph, layer):
    """(lsb, rsb, width) of a glyph on the ink of its decomposed outline
    (lsb, rsb None without ink), read fresh: never from caches that a held
    notification has not refreshed yet."""
    extent = ink_x(glyph, layer)
    width = float(glyph.width)
    if extent is None:
        return None, None, width
    return extent[0], width - extent[1], width


# --------------------------------------------------------------- snapshot
class Snapshot(object):
    """One font (its default layer), copied out of RoboFont for the engine
    and the proofs. Built by a SnapshotReader; complete once the reader is
    done.

    `names` is the engine's glyph order (the spacing set); `specs[i]` and the
    packer's glyph i belong to `names[i]`. Group ids in the specs index
    `left_group_names` (public.kern2 groups) and `right_group_names`
    (public.kern1 groups), names without their prefix. `master_id` names the
    layer read and `master_name` the font, as the windows show it.
    """

    def __init__(self, font):
        import kk2_host as host
        self.font = font
        self.dfont = host.naked(font)
        layer = self.dfont.layers.defaultLayer
        self.layer = layer
        self.master_id = str(getattr(layer, "name", None) or "public.default")
        self.master_name = host.font_title(font)
        info = self.dfont.info
        self.upm = _number(getattr(info, "unitsPerEm", None), 1000.0) or 1000.0
        self.ascender = _number(getattr(info, "ascender", None), 0.8 * self.upm)
        self.descender = _number(getattr(info, "descender", None), -0.2 * self.upm)
        self.cap_height = _number(getattr(info, "capHeight", None), 0.7 * self.upm)
        self.x_height = _number(getattr(info, "xHeight", None), 0.5 * self.upm)
        self.italic_angle = _number(getattr(info, "italicAngle", None), 0.0)
        self.italic = abs(self.italic_angle) > 1e-6
        # measured along the italic angle (set_slant): the engine sees the
        # outlines sheared upright about half the x-height, and `frame` holds
        # per spacing glyph what that adds to its (LSB, RSB)
        self.slant = 0.0
        self.slant_pivot = 0.0
        self.frame = []
        # tan of the slant the stems show (stem_slant), for a font that
        # declares no italic angle; where the slant measured along comes from
        self.stem_slant = 0.0
        self.slant_from = ""

        self.names = []
        self.index = {}
        self.spacing_names = set()
        self.infos = {}
        self.specs = []
        self.packer = kb.InputPacker()
        self.left_group_names = []
        self.right_group_names = []
        self.left_group_ids = {}
        self.right_group_ids = {}
        self.left_group_members = {}  # group name → spacing glyphs in it
        self.right_group_members = {}
        self.char_map = {}
        self.export_names = set()
        self.space_name = None
        self.fixed = set()
        self.metrics_keyed = set()  # spacing glyphs with a metrics key
        self.aligned = set()  # composites spaced as aligned
        self.pinned = set()  # glyphs that keep their sidebearings (keeps_sidebearings)
        self.component_users = {}  # glyph → glyphs (any, in the layer) with a component of it
        self.read_ms = 0.0  # main-thread time spent reading
        self.wall_ms = 0.0  # from the first slice to the last
        self.glyph_count_skipped = 0
        self.group_doubles = []  # glyphs in more than one kerning group of a side
        self.errors = []
        self._unicode_of = {}  # exporting glyph → first code point
        self._category_of = {}  # exporting glyph → (category, subcategory, case)
        self._left_of = {}  # glyph → public.kern2 group (short name)
        self._right_of = {}  # glyph → public.kern1 group
        self._stale = set()

    @property
    def slant_degrees(self):
        """How far the font leans (degrees, + right): by its italic angle
        where it declares one of SLANT_MIN_DEGREES or more, else by the slant
        its stems show (stem_slant), else its italic angle."""
        if slant_measurable(self.italic_angle) or not self.stem_slant:
            return -self.italic_angle
        return math.degrees(math.atan(self.stem_slant))

    @property
    def slant_source(self):
        """Where the slant to measure along comes from: "declared" (the
        italic angle), "measured" (the stems), or "" (upright)."""
        if slant_measurable(self.italic_angle):
            return "declared"
        return "measured" if self.stem_slant and slant_measurable(self.slant_degrees) else ""

    def set_slant(self, on, stem=0.0):
        """Measure along the slant (`on`): the italic angle when the font
        leans by SLANT_MIN_DEGREES or more (less than SLANT_MAX_DEGREES), else
        the slant its stems show (`stem`: tan, stem_slant), for a design that
        leans without declaring it. Kinetikern2's model is built on upright
        letters: measured upright, an italic comes out too loose and uneven
        (on 171 Google Fonts italics, gaps 29.8 units per 1000 em from the
        designers' against 21.3 measured along the angle). Sidebearings and
        kerning are horizontal offsets, which a shear keeps; about half the
        x-height, the sides are where the eye (and Glyphs) judges them. Set
        before the glyphs are read."""
        self.stem_slant = float(stem or 0.0)
        self.slant = self.slant_pivot = 0.0
        self.slant_from = ""
        source = self.slant_source
        if on and source:
            self.slant = math.tan(math.radians(self.slant_degrees))
            self.slant_pivot = 0.5 * self.x_height
            self.slant_from = source

    def engine_sides(self, name):
        """(LSB, RSB) of a spacing glyph in the engine's frame: as the font
        has them, or measured along the italic angle."""
        info = self.infos[name]
        i = self.index.get(name)
        sl, sr = self.frame[i] if i is not None and i < len(self.frame) else (0.0, 0.0)
        return info.lsb + sl, info.rsb + sr

    def glyph_info(self, name):
        """GlyphInfo of any exporting glyph; read lazily for glyphs outside
        the spacing set (and again after invalidate())."""
        info = self.infos.get(name)
        if (info is None or name in self._stale) and name in self.export_names:
            try:
                if name not in self.layer:
                    return info
                glyph = self.layer[name]
                info = read_glyph_info(glyph, self.layer, name, self._unicode_of.get(name),
                                       self.flags_codepoint(name), self._category_of.get(name, (None, None, 0)),
                                       self._left_of.get(name), self._right_of.get(name),
                                       spacing=name in self.index)
                if name in self.index:
                    info.aligned = name in self.aligned
            except Exception as e:
                if len(self.errors) < 20:
                    self.errors.append("%s: %s" % (name, e))
                return info
            self.infos[name] = info
            self._stale.discard(name)
        return info

    def invalidate(self, names=None):
        """Marks what was read of `names` (every glyph if None) as stale, so
        glyph_info() reads them again, e.g. after Apply changed their
        metrics. O(len(names)); the engine input (specs, packer) is kept."""
        self._stale.update(self.infos if names is None else names)

    def flags_codepoint(self, name):
        """The code point that decides a glyph's script and kerning: its own,
        else that of the name it extends (arrowright.case → U+2192)."""
        cp = self._unicode_of.get(name)
        if cp is None:
            cp = name_codepoint(name)
        return cp

    def composites_of(self, names):
        """Glyphs of the font that had a component of one of `names` when it
        was read (kk2_apply holds their components in place)."""
        out = set()
        for name in names:
            out.update(self.component_users.get(name, ()))
        return out

    def _component_user(self, base, composite):
        users = self.component_users.get(base)
        if users is None:
            users = self.component_users[base] = set()
        users.add(composite)

    def kern_mask(self, names):
        """Engine mask (one byte per glyph) with the given glyphs set."""
        mask = bytearray(len(self.names))
        for name in names:
            i = self.index.get(name)
            if i is not None:
                mask[i] = 1
        return mask

    def _group_id(self, left, group, member):
        if not group:
            return kb.NONE
        names, ids, members = ((self.left_group_names, self.left_group_ids, self.left_group_members) if left else
                               (self.right_group_names, self.right_group_ids, self.right_group_members))
        gid = ids.get(group)
        if gid is None:
            gid = ids[group] = len(names)
            names.append(group)
        members.setdefault(group, []).append(member)
        return gid


class SnapshotReader(object):
    """Reads a font into a Snapshot in slices. Call step() from a timer until
    it returns True; `snapshot` is then complete (None if cancelled)."""

    LIST_SHARE = 0.08  # progress shares of passes 1 and 2 (pass 3 has the rest)
    READ_SHARE = 0.9

    def __init__(self, font, keep_figure_widths=True, along_slant=True, stem_slant=0.0, only=None):
        """`stem_slant`: tan of the slant the font's stems show (stem_slant),
        measured along with `along_slant` where it declares no italic angle;
        `only`: the names of the spacing glyphs to read (None: every one)."""
        self.snapshot = None
        self.fraction = 0.0
        self.done = False
        self.cancelled = False
        self._snap = Snapshot(font)
        self._snap.set_slant(along_slant, stem_slant)
        self._only = frozenset(only) if only is not None else None
        self._keep_figure_widths = keep_figure_widths
        self._pass = 1
        self._pos = 0
        self._names = None
        self._skip = set()
        self._ot_categories = {}
        self._categories = {}  # name → (category, subcategory, case), memoised
        self._candidates = []  # (name, category triple, first code point)
        self._glyph_cost = 0.0004  # running estimate of one glyph's read in pass 2 (s)
        self._started = None
        self._busy = 0.0

    def step(self, budget_s=0.008):
        """Reads for at most about `budget_s` seconds (at least one glyph).
        True when done. Each pass starts in a slice of its own."""
        if self.done or self.cancelled:
            return True
        t0 = time.perf_counter()
        if self._started is None:
            self._started = t0
        deadline = t0 + max(0.0, budget_s)
        try:
            if self._pass == 1:
                finished = self._list(deadline)
            elif self._pass == 2:
                finished = self._read(deadline)
            else:
                finished = self._resolve(deadline)
            if finished:
                self._pass += 1
                self._pos = 0
        finally:
            t1 = time.perf_counter()
            self._busy += t1 - t0
        if self._pass > 3:
            self._finish(t1)
        self._update_fraction()
        return self.done

    def read_all(self):
        """Reads everything in one go (tools and tests; never the UI)."""
        while not self.step(1.0):
            pass
        return self.snapshot

    def cancel(self):
        self.cancelled = True
        self._names = None
        self._candidates = []

    # pass 1: every glyph, cheaply
    def _list(self, deadline):
        snap = self._snap
        layer = snap.layer
        if self._names is None:
            dfont = snap.dfont
            order = [n for n in (getattr(dfont, "glyphOrder", None) or []) if n in layer]
            seen = set(order)
            order.extend(sorted(n for n in layer.keys() if n not in seen))
            self._names = order
            lib = dfont.lib
            self._skip = set(str(n) for n in (lib.get(SKIP_EXPORT_KEY) or ()))
            self._ot_categories = dict((str(k), str(v)) for k, v in (lib.get(OT_CATEGORIES_KEY) or {}).items())
            snap._left_of, snap._right_of, snap.group_doubles = kerning_group_maps(dfont.groups)
        names = self._names
        n = len(names)
        i = self._pos
        while i < n:
            name = str(names[i])
            i += 1
            glyph = layer[name]
            if name not in self._skip:
                snap.export_names.add(name)
                cps = _codepoints(glyph)
                for cp in cps:
                    snap.char_map.setdefault(chr(cp), name)
                if cps:
                    snap._unicode_of[name] = cps[0]
                if name == "space":
                    snap.space_name = name
                category = self._category(name, glyph, cps)
                snap._category_of[name] = category
                if category[0] in SPACING_CATEGORIES and (self._only is None or name in self._only):
                    self._candidates.append((name, category, cps[0] if cps else None))
                else:
                    self._note_components(glyph, name)
            else:
                self._note_components(glyph, name)
            if time.perf_counter() >= deadline:
                break
        self._pos = i
        if i >= n and snap.space_name is None:
            snap.space_name = snap.char_map.get(" ")
        return i >= n

    def _category(self, name, glyph, cps, depth=0):
        """A glyph's (category, subcategory, case): by its code point (its
        own, else that of the name it extends); for a name the AGL does not
        know (Jacute, idotaccent.sc), that of the glyph its name extends
        (Jacute.sc → Jacute) or, for a composite, of its first component."""
        snap = self._snap
        known = self._categories.get(name)
        if known is not None:
            return known
        cp = cps[0] if cps else name_codepoint(name)
        category = glyph_category(name, cp, self._ot_categories.get(name))
        if category[0] is None and depth < 8:
            layer = snap.layer
            base = name.split(".")[0]
            other = None
            if base and base != name and base in layer:
                other = base
            elif glyph is not None and glyph.components and glyph.components[0].baseGlyph in layer:
                other = glyph.components[0].baseGlyph
            if other is not None and other != name:
                og = layer[other]
                category = self._category(other, og, _codepoints(og), depth + 1)
                if category[0] == "Letter" and any(s in SMALLCAP_SUFFIXES for s in name.split(".")[1:]):
                    category = ("Letter", "Smallcaps", GSSmallcaps)
        self._categories[name] = category
        return category

    def _note_components(self, glyph, name):
        """Notes the components of a glyph outside the spacing set (pass 2
        notes those of the spacing glyphs)."""
        for comp in glyph.components:
            if comp.baseGlyph:
                self._snap._component_user(str(comp.baseGlyph), name)

    # pass 2: the spacing set, outline and properties
    def _read(self, deadline):
        snap = self._snap
        cands = self._candidates
        i = start = self._pos
        cost = self._glyph_cost
        while i < len(cands):
            t = time.perf_counter()
            if i > start and t + cost > deadline:
                break  # the next glyph would likely run past the slice
            name, category, cp = cands[i]
            i += 1
            try:
                self._read_one(name, category, cp)
            except Exception as e:  # RecursionError: a component that refers back to its glyph
                snap.glyph_count_skipped += 1
                if len(snap.errors) < 20:
                    snap.errors.append("%s: %s" % (name, e))
            cost += 0.2 * (time.perf_counter() - t - cost)
        self._glyph_cost = cost
        self._pos = i
        return i >= len(cands)

    def _read_one(self, name, category, cp):
        snap = self._snap
        if name in snap.index or name not in snap.layer:
            snap.glyph_count_skipped += 1
            return
        glyph = snap.layer[name]
        info = read_glyph_info(glyph, snap.layer, name, cp, snap.flags_codepoint(name), category,
                               snap._left_of.get(name), snap._right_of.get(name), spacing=True)
        flags = 0
        if info.kern:
            flags |= kb.GLYPH_KERN
        if info.rtl:
            flags |= kb.GLYPH_RTL
        if info.left_group == name:
            flags |= kb.GLYPH_LEFT_KEY
        if info.right_group == name:
            flags |= kb.GLYPH_RIGHT_KEY
        if zone_reference(name, cp, snap.flags_codepoint(name), info.category):
            flags |= kb.GLYPH_ZONE
        if cp is not None and 0x30 <= cp <= 0x39:
            flags |= kb.GLYPH_FIGURE  # the decoration test reads the default figures
        contours, sl, sr = info.contours, 0.0, 0.0
        if snap.slant and not info.empty:
            # the engine measures along the slant: what that adds to each side
            contours = shear_nodes(info.contours, snap.slant, snap.slant_pivot)
            b = contour_bounds(contours)
            if b is not None:
                sl, sr = b[0] - info.lsb, (info.width - info.rsb) - b[2]
        spec = kb.GlyphSpec(name, contours, info.width, rhythm_group(info), flags, info.script, kb.NONE,
                            snap._group_id(True, info.left_group, name),
                            snap._group_id(False, info.right_group, name))
        if not info.empty:
            spec.cur_lsb, spec.cur_rsb = info.lsb + sl, info.rsb + sr
        snap.frame.append((sl, sr))
        k = snap.packer.add(spec)
        assert k == len(snap.names)
        snap.index[name] = k
        snap.names.append(name)
        snap.specs.append(spec)
        snap.infos[name] = info
        for base in info.components:
            snap._component_user(base, name)
        if info.left_key or info.right_key or info.width_key:
            snap.metrics_keyed.add(name)

    # pass 3: names → indices
    def _resolve(self, deadline):
        snap = self._snap
        if self._pos == 0:
            snap.fixed = self._fixed_advances()
        n = len(snap.names)
        i = self._pos
        while i < n:
            self._resolve_one(i)
            i += 1
            if time.perf_counter() >= deadline:
                break
        self._pos = i
        return i >= n

    def _fixed_advances(self):
        """Glyphs whose advance stays: tabular figures (by suffix, or the
        default figures when they all share one width), every glyph with a
        width metrics key, and the glyphs such keys follow. Kinetikern2 does
        not evaluate width keys: with both ends of a key keeping their
        advance, the key stays true."""
        snap = self._snap
        fixed = set()
        if self._keep_figure_widths:
            figures = [n for n in DEFAULT_FIGURES if n in snap.index]
            widths = set(int(round(snap.infos[n].width)) for n in figures)
            if len(figures) >= 5 and len(widths) == 1:
                fixed.update(figures)
            fixed.update(n for n in snap.names if "." in n and is_tabular(n))
        for name in snap.metrics_keyed:
            rule = parse_metrics_key(snap.infos[name].width_key)
            if rule is None:
                continue
            fixed.add(name)
            target = rule[1] if rule[0] == "follow" else None
            if target in snap.index:
                fixed.add(target)
        return fixed

    def _aligned(self, name, info):
        """True for a composite built the way Glyphs aligns an accented glyph
        automatically: no contours of its own, one spacing component (a glyph
        that is not a mark) at the origin with no scaling or slanting, marks
        anywhere, its advance the base's, the base in the spacing set (a base
        that is not spaced never moves: a composite of an unencoded drawing
        is spaced as the glyph it is). Glyphs' own data (glyphsLib) can turn
        the alignment off. A ligature built from components (f_i) is spaced as
        one shape: its components keep their places."""
        snap = self._snap
        layer = snap.layer
        glyph = layer[name]
        if len(glyph) or not info.components:  # len(): its contours
            return False
        if _component_alignment_off(getattr(glyph, "lib", None)):
            return False
        x = 0.0
        spacing = 0
        for comp in glyph.components:
            base = comp.baseGlyph
            if not base or base == name or base not in layer:
                return False
            category = snap._category_of.get(base)
            if category is None:
                category = self._category(base, layer[base], _codepoints(layer[base]))
            if category[0] in UNKERNED_CATEGORIES:
                continue  # a mark: anywhere
            if base not in snap.index:
                return False  # nothing to follow
            xx, xy, yx, yy, dx, _dy = comp.transformation
            if abs(xx - 1.0) > 1e-9 or abs(yy - 1.0) > 1e-9 or abs(xy) > 1e-9 or abs(yx) > 1e-9:
                return False
            if abs(dx - x) > _ALIGN_TOLERANCE:
                return False
            x = dx + float(layer[base].width)
            spacing += 1
        return spacing == 1 and abs(float(glyph.width) - x) <= _ALIGN_TOLERANCE

    def _resolve_one(self, i):
        snap = self._snap
        name = snap.names[i]
        spec = snap.specs[i]
        info = snap.infos[name]
        if name in snap.fixed:
            spec.flags |= kb.GLYPH_FIXED_ADVANCE
        if info.components:
            base = snap.index.get(info.components[0], kb.NONE)
            spec.base = base if base != i else kb.NONE
        if info.empty:
            return  # no outline: the engine leaves the glyph alone
        if keeps_sidebearings(info, snap.flags_codepoint(name)):
            # both sides as they are, the advance with them (the engine
            # applies side rules to glyphs whose advance it does not keep)
            snap.pinned.add(name)
            spec.flags &= ~kb.GLYPH_FIXED_ADVANCE
            lsb, rsb = snap.engine_sides(name)
            _set_rule(spec, True, kb.RULE_FIXED, kb.NONE, lsb)
            _set_rule(spec, False, kb.RULE_FIXED, kb.NONE, rsb)
            return
        aligned = False
        try:
            aligned = info.components and self._aligned(name, info)
        except Exception as e:
            if len(snap.errors) < 20:
                snap.errors.append("%s: %s" % (name, e))
        if aligned:
            # an aligned composite moves with its base: both sides follow it
            info.aligned = True
            snap.aligned.add(name)
            spacing = [c for c in info.components if c in snap.index and c != name]
            self._follow_component(spec, info, True, spacing[0] if spacing else None)
            self._follow_component(spec, info, False, spacing[-1] if spacing else None)
        else:
            self._follow_key(i, spec, info, True)
            self._follow_key(i, spec, info, False)

    def _follow_component(self, spec, info, left, target):
        snap = self._snap
        tinfo = snap.infos.get(target) if target is not None else None
        cur = snap.engine_sides(info.name)[0 if left else 1]
        if tinfo is None or tinfo.empty:
            _set_rule(spec, left, kb.RULE_FIXED, kb.NONE, cur)
        else:
            _set_rule(spec, left, kb.RULE_FOLLOW_SAME, snap.index[target],
                      cur - snap.engine_sides(target)[0 if left else 1])

    def _follow_key(self, i, spec, info, left):
        snap = self._snap
        rule = parse_metrics_key(info.left_key if left else info.right_key)
        if rule is None:
            return
        cur = snap.engine_sides(info.name)[0 if left else 1]
        if rule[0] == "follow":
            _, target, opposite, offset = rule
            target = target or info.name
            t = snap.index.get(target)
            tinfo = snap.infos.get(target) if t is not None else None
            if tinfo is not None and not tinfo.empty and not (t == i and not opposite):
                if snap.italic:
                    # Glyphs evaluates a key along the italic angle, a
                    # measure a UFO does not keep: the two sides keep the
                    # relation they have now on the ink
                    target_left = left != opposite
                    offset = cur - snap.engine_sides(target)[0 if target_left else 1]
                _set_rule(spec, left, kb.RULE_FOLLOW_OPPOSITE if opposite else kb.RULE_FOLLOW_SAME, t, offset)
                return
        _set_rule(spec, left, kb.RULE_FIXED, kb.NONE, cur)

    def _finish(self, t1):
        snap = self._snap
        snap.spacing_names = set(snap.names)
        snap.packer.frame = list(snap.frame) if snap.slant else None
        snap.read_ms = 1000.0 * self._busy
        snap.wall_ms = 1000.0 * (t1 - self._started)
        self.snapshot = snap
        self.done = True
        self._names = None
        self._candidates = []

    def _update_fraction(self):
        if self.done:
            self.fraction = 1.0
            return
        if self._pass == 1:
            n = len(self._names) if self._names is not None else 0
            f = self.LIST_SHARE * (self._pos / n if n else 0.0)
        elif self._pass == 2:
            n = len(self._candidates)
            f = self.LIST_SHARE + self.READ_SHARE * (self._pos / n if n else 1.0)
        else:
            n = len(self._snap.names)
            rest = 1.0 - self.LIST_SHARE - self.READ_SHARE
            f = self.LIST_SHARE + self.READ_SHARE + rest * (self._pos / n if n else 1.0)
        self.fraction = min(1.0, max(self.fraction, f))


def _set_rule(spec, left, rule, glyph, value):
    if left:
        spec.lsb_rule, spec.lsb_glyph, spec.lsb_value = rule, glyph, float(value)
    else:
        spec.rsb_rule, spec.rsb_glyph, spec.rsb_value = rule, glyph, float(value)


# ------------------------------------------------------------ sample text
def tokenize(text, snapshot):
    """Sample text → list of glyph names, NEWLINE, SPACE or MISSING.

    "/name" (ended by a space, which is consumed, by "/", another white space
    or a line break) inserts an exporting glyph by name, as in a Space
    Center.
    """
    known = getattr(snapshot, "export_names", None) or snapshot.names
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _NEWLINES:
            if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1
            tokens.append(NEWLINE)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] not in _NAME_ENDS:
            j = i + 1
            while j < n and text[j] not in _NAME_ENDS:
                j += 1
            name = text[i + 1:j]
            if name in known:
                tokens.append(name)
                i = j + 1 if j < n and text[j] == " " else j
                continue
        if ch in _SPACES:
            tokens.append(SPACE)
        else:
            tokens.append(snapshot.char_map.get(ch, MISSING))
        i += 1
    return tokens


# ------------------------------------------------------------ measured slant
def master_italic_angle(font):
    """A font's italic angle (degrees as UFOs give it: negative leans right;
    0 when it has none)."""
    import kk2_host as host
    return _number(getattr(host.naked(font).info, "italicAngle", None), 0.0)


def stem_slant(engine, font):
    """tan of the slant a font's stems show (+ leans right), or 0 for an
    upright design: Spacing QA's detector, in the engine (kk2_stem_slant), on
    the glyphs it names (l i h n m u r k b p) and the top of the x. 0 with an
    older engine, or without an x."""
    import kk2_host as host
    names = engine.stem_glyphs()
    if not names:
        return 0.0
    layer = host.naked(font).layers.defaultLayer

    def contours(name):
        return draw_contours(layer[name], layer) if name in layer else []

    x = contours("x")
    if not x:
        return 0.0
    packer = kb.InputPacker()
    for name in names:
        packer.add(kb.GlyphSpec(name, contours(name), 0.0))
    upm = _number(getattr(host.naked(font).info, "unitsPerEm", None), 1000.0) or 1000.0
    return engine.stem_slant(packer, max(p[1] for c in x for p in c), float(upm))


class SlantProbe(object):
    """Whether a font that declares no italic angle but whose stems lean
    is measured along that slant: Spacing QA's rule (kk2_lean_wins), on the
    glyphs of the GF Latin Kernel the way Spacing QA checks them. They are
    read upright and along the slant; each set is prepared (a connected
    script with its joins: bands are heights, which a shear keeps), fitted
    to the font's own spacing (its letters and punctuation; the kept joins
    of a connected script, as Keep joins does), solved there, pair by pair
    with the designer harness when there is one, and measured against the
    font's spacing. The slant wins where that leaves at most 80 % of the
    shape error upright, or where upright the fit stops at the limit of the
    Looseness range and along the slant it does not.

    Call step() from a timer until it returns True: `lean` is then the
    verdict (None: it could not tell, `error` says why) and `numbers` what
    it measured: ((shape error, fit) upright, (shape error, fit) along the
    slant), shape errors in units per 1000 em."""

    def __init__(self, engine, font, key, slant, snapshot, physics, joins=None, current_steps=None,
                 harness=None, threads=0, budget_s=0.008):
        """`key`: the window's key for the font (its master_id here);
        `snapshot`: the font as read (its glyph names and characters);
        `physics`: Looseness → (spring, repulsion, coupling) at 100 %
        intensity; `joins`: None, or ({name: (left band, right band)},
        {name: join kind}) of a connected script whose joins are kept;
        `current_steps`: snapshot → a generator returning the font's
        kerning as engine input (kk2_pairs_window.current_kerning_steps);
        `harness`: (snapshot, Looseness, kept sides) → the solve's harness
        argument, or None."""
        self.engine = engine
        self.master_id = key
        self.slant = float(slant)
        self.lean = None
        self.numbers = None
        self.error = None
        self.trace = None
        self.done = False
        self._physics = physics
        self._joins = joins
        self._current_steps = current_steps
        self._harness = harness
        self._threads = int(threads)
        self._budget = float(budget_s)
        chars = KERNEL_FIT + KERNEL_SCORED
        names = []
        for ch in chars:
            name = snapshot.char_map.get(ch)
            if name and name in snapshot.index and name not in names:
                names.append(name)
        self._fit_names = set(snapshot.char_map.get(ch) for ch in KERNEL_FIT)
        self._readers = [SnapshotReader(font, along_slant=False, only=names),
                         SnapshotReader(font, along_slant=True, stem_slant=self.slant, only=names)]
        self._held = []  # contexts, results and jobs to free
        self._steps = self._run()

    def step(self, budget_s=None):
        """Works for about `budget_s` seconds (reading in slices; engine jobs
        are polled). True when done."""
        if self.done:
            return True
        if budget_s is not None:
            self._budget = float(budget_s)
        try:
            next(self._steps)
        except StopIteration:
            self.done = True
        except Exception as e:
            import traceback
            self.error, self.trace, self.done = str(e) or e.__class__.__name__, traceback.format_exc(), True
        if self.done:
            self._free()
        return self.done

    def cancel(self):
        for reader in self._readers:
            reader.cancel()
        self.error = self.error or "cancelled"
        self.done = True
        self._free()

    def _free(self):
        """Frees what the probe holds: a job still running is cancelled."""
        for obj in self._held:
            try:
                if isinstance(obj, kb.Job):
                    obj.free()  # cancels a running job; never blocks
                else:
                    obj.close()
            except Exception:
                pass
        self._held = []

    def _run(self):
        snaps = []
        for reader in self._readers:
            while not reader.step(self._budget):
                yield
            if reader.snapshot is None:
                raise RuntimeError("reading cancelled")
            snaps.append(reader.snapshot)
            yield
        if any(len(s.names) < 20 for s in snaps):
            raise RuntimeError("too few of the kernel's glyphs to compare")
        # the font's kerning as engine input: both sets read the same
        # glyphs in the same order, so their indices and group ids agree
        current = []
        if self._current_steps is not None:
            current = (yield from self._current_steps(snaps[0])) or []
        out = []
        for snap in snaps:
            ctx = yield from self._wait(self._prepare(snap, current))
            fit, result = yield from self._fit_and_solve(snap, ctx)
            stats = self.engine.measure(ctx, result, current, mask=None, scope_scripts=True, cap=1)[0]
            out.append((stats["mae"] * 1000.0 / snap.upm, fit))
            yield
        self.numbers = (out[0], out[1])
        self.lean = self.engine.lean_wins(out[0], out[1])

    def _wait(self, job):
        """Polls `job` a tick at a time; its Context or Result."""
        self._held.append(job)
        while job.poll()[0] == kb.STATE_RUNNING:
            yield
        state = job.poll()[0]
        if state != kb.STATE_DONE:
            raise kb.EngineError(job.error() or self.engine.last_error() or "the engine stopped")
        out = job.take()
        self._held.remove(job)
        job.free()
        self._held.append(out)
        return out

    def _prepare(self, snap, current):
        if self._joins is None:
            return self.engine.prepare(snap.packer, snap.upm, self._threads)
        bands_of, kinds_of = self._joins
        bands = [bands_of.get(name) or (None, None) for name in snap.names]
        if self.engine.features & kb.FEATURE_JOIN_CHECK:
            kinds = bytearray(int(kinds_of.get(name, 0)) for name in snap.names)
            return self.engine.prepare(snap.packer, snap.upm, self._threads, joins=bands, join_kinds=kinds,
                                       current=current, keep_joins=True)
        return self.engine.prepare(snap.packer, snap.upm, self._threads, joins=bands)

    def _params(self, looseness, fit_frozen=False, skip_pass2=False):
        """Spacing QA's solve: pair by pair, every pair, no threshold."""
        spring, repulsion, coupling = self._physics(looseness)
        return kb.make_params(spring=spring, repulsion=repulsion, coupling=coupling, classes=False, window=True,
                              scope_scripts=True, threshold=0.0, budget=0, threads=self._threads,
                              fit_frozen=fit_frozen, skip_pass2=skip_pass2)

    def _fit_and_solve(self, snap, ctx):
        """The Looseness fitted to the font's spacing, and the solve there."""
        kept = None
        if self._joins is not None:
            # Keep joins: matched to the joined letters (Pass 1 is enough)
            first = yield from self._wait(self.engine.solve(ctx, self._params(0.0, fit_frozen=True, skip_pass2=True)))
            fit = first.fitted_looseness  # a property
            try:
                bits = self.engine.join_sides(ctx)
                left = [i for i, b in enumerate(bits) if b & kb.JOINSIDE_LEFT_KEPT]
                right = [i for i, b in enumerate(bits) if b & kb.JOINSIDE_RIGHT_KEPT]
                kept = (left, right) if left or right else None
            except Exception:
                kept = None
        else:
            which = [1 if name in self._fit_names else 0 for name in snap.names]
            fit = self.engine.fit_looseness(ctx, self._params(0.0), which)
        fit = 0.0 if fit is None else float(fit)
        harness = self._harness(snap, fit, kept) if self._harness is not None else None
        result = yield from self._wait(self.engine.solve(ctx, self._params(fit), harness=harness))
        return fit, result
