# encoding: utf-8
"""
kk2_snapshot — one master of a Glyphs font, read into engine inputs.

Main thread only: GSFont and GSLayer are not thread-safe, so a master is
copied into kk2_bridge.GlyphSpec objects here and the engine only ever sees
those copies. Reading a whole font at once takes about a second (Arial:
outlines 0.6 s, properties 0.4 s), so a SnapshotReader reads in slices of a
few milliseconds, driven by the window's timer:

  Pass 1  lists every glyph cheaply (export, name, unicodes, category; the
          component names of glyphs outside the spacing set).
  Pass 2  reads each glyph of the spacing set (outline and properties) and
          adds its GlyphSpec to the InputPacker as it goes.
  Pass 3  resolves what refers to other glyphs by name (composite bases,
          metrics keys, auto-aligned composites, tabular figures) into engine
          indices; the targets may have been read after the glyph itself.

Outlines are read from `layer.completeBezierPath`, which already has every
component decomposed with its transform, so accented and composite glyphs
need no special handling.

Sidebearings are measured from the ink bounds of that outline, the frame the
engine works in. Glyphs' own layer.LSB / RSB are kept next to them: in
Glyphs 3.5 they agree to within half a unit on upright and italic masters
alike, and where a master measures differently (italic sidebearings), metrics
keys, which Glyphs evaluates in its own measure, are translated.
"""

from __future__ import division, print_function, unicode_literals

import bisect
import re
import time
import unicodedata

import kk2_bridge as kb

try:
    from GlyphsApp import GSLowercase, GSMinor, GSSmallcaps, GSUppercase
except ImportError:  # outside Glyphs (tests, tools)
    GSUppercase, GSLowercase, GSSmallcaps, GSMinor = 1, 2, 3, 4
try:
    from GlyphsApp import GSRTL
except ImportError:
    GSRTL = 2

# NSBezierPath element types (NSBezierPathElementQuadraticCurveTo is macOS 14+)
_MOVE, _LINE, _CURVE, _CLOSE, _QUAD = 0, 1, 2, 3, 4

SPACING_CATEGORIES = ("Letter", "Number", "Punctuation", "Symbol")
UNKERNED_CATEGORIES = ("Mark", "Separator")
TABULAR_SUFFIXES = (".tf", ".tosf", ".tnum")
DEFAULT_FIGURES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")

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
# block-element characters. The optical model spaces separate shapes.
JOINING_SCRIPTS = frozenset(("Mong", "Phag", "Deva", "Beng", "Guru", "Sylo", "Tirh"))
EDGE_TO_EDGE_BLOCKS = ((0x2500, 0x257F), (0x2580, 0x259F))

# Script by block (ISO 15924), for glyphs that Glyphs gives no script: the
# letters, marks and digits of a block, and everything in the blocks of
# right-to-left scripts (their punctuation is used in their text). Other
# characters, and the few below, are Common.
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


# ---------------------------------------------------------------- helpers
def rhythm_group(glyph, category=None):
    """Engine rhythm group of a GSGlyph (uppercase, lowercase, figures,
    small caps, other). `category`: glyph.category if already read."""
    if category is None:
        category = glyph.category
    if category == "Number":
        return kb.GROUP_FIGURES
    if category != "Letter":
        return kb.GROUP_OTHER
    case = getattr(glyph, "case", None)
    sub = getattr(glyph, "subCategory", None)
    if case == GSSmallcaps or sub == "Smallcaps":
        return kb.GROUP_SMALLCAPS
    if case == GSUppercase or sub == "Uppercase":
        return kb.GROUP_UPPERCASE
    if case in (GSLowercase, GSMinor) or sub == "Lowercase":
        return kb.GROUP_LOWERCASE
    return kb.GROUP_OTHER


def bezier_contours(path):
    """Closed contours of an NSBezierPath as lists of (x, y, node kind)."""
    contours = []
    current = None
    if path is None:
        return contours
    for i in range(path.elementCount()):
        kind, points = path.elementAtIndex_associatedPoints_(i)
        if kind == _MOVE:
            p = points[0]
            current = [(p.x, p.y, 0)]  # the start node's kind is settled at closePath
        elif current is None:
            continue
        elif kind == _LINE:
            p = points[0]
            current.append((p.x, p.y, 0))
        elif kind == _CURVE:
            c1, c2, p = points[0], points[1], points[2]
            current.extend([(c1.x, c1.y, 2), (c2.x, c2.y, 2), (p.x, p.y, 1)])
        elif kind == _QUAD:
            c, p = points[0], points[1]
            current.extend([(c.x, c.y, 2), (p.x, p.y, 3)])
        elif kind == _CLOSE:
            if len(current) > 1 and current[-1][0] == current[0][0] and current[-1][1] == current[0][1]:
                # the last segment ends on the start point: it is the closing segment
                last = current.pop()
                current[0] = (current[0][0], current[0][1], last[2])
            if len(current) >= 2:
                contours.append(current)
            current = None
    return contours  # open subpaths (no closePath) are not ink


def layer_path(layer):
    """The decomposed outline of a layer as an NSBezierPath (or None)."""
    try:
        path = layer.completeBezierPath
    except Exception:
        path = None
    if path is None:
        try:
            path = layer.bezierPath
        except Exception:
            path = None
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


def script_iso(code):
    """kk2_bridge.script_code() back to its four letters ("" for Common)."""
    if not code:
        return ""
    return "".join(chr((code >> s) & 0xFF) for s in (24, 16, 8, 0)).strip()


_OFFSET = re.compile(r"^(.*?)\s*([+-])\s*(\d+(?:\.\d*)?|\.\d+)$")
_NUMBER = re.compile(r"^(\d+(?:\.\d*)?|\.\d+)$")
_NOT_IN_NAMES = re.compile(r"[\s*/()=|+,]")


def parse_metrics_key(key):
    """A Glyphs metrics key → None (no key), ("follow", target, opposite,
    offset) or ("fixed",).

    "=X", "=X+n" and "=X-n" follow glyph X's same side, "=|X" and "=|X+n" its
    opposite side, and "=|" alone the glyph's own opposite side (target "").
    Glyphs 3 returns a layer's key with a double "=" ("==X") and keeps a
    plain same-side reference without one ("X", as .glyphs files store it),
    so the leading "=" signs do not matter. Anything else (a number, "*",
    another formula) is a rule the engine cannot evaluate: the side keeps
    its current value.
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
    """An NSString / str property as str, None when empty."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _number(value, fallback=_NAN):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    return v if v == v and abs(v) < 1e7 else fallback  # NaN, inf and "not found" sentinels


def _codepoints(glyph):
    """The glyph's code points (ints), first one first."""
    values = getattr(glyph, "unicodes", None)
    if not values:
        u = getattr(glyph, "unicode", None)
        values = [u] if u else ()
    out = []
    for u in values:
        try:
            cp = int(str(u), 16)
        except (TypeError, ValueError):
            continue
        if 0 <= cp <= 0x10FFFF:
            out.append(cp)
    return out


def _layer(glyph, master_id):
    try:
        return glyph.layers[master_id]
    except (KeyError, IndexError, TypeError):
        return None


# ------------------------------------------------------------- glyph info
class GlyphInfo(object):
    """One glyph on the snapshot master, as the font has it now.

    `lsb` / `rsb` / `bounds` are measured on the ink of the decomposed
    outline (`path`), the frame the engine and the proofs work in;
    `font_lsb` / `font_rsb` are Glyphs' own layer.LSB / RSB (the same within
    half a unit in Glyphs 3.5; Apply writes `font_lsb + new lsb - lsb`, which
    holds for any measure Glyphs uses).
    """

    __slots__ = ("name", "glyph_id", "unicode", "char", "path", "width", "lsb", "rsb", "bounds", "empty",
                 "font_lsb", "font_rsb", "category", "subcategory", "case", "script", "rtl", "kern", "left_group", "right_group",
                 "left_key", "right_key", "width_key", "components", "aligned")

    @property
    def advance(self):
        return self.width


def read_glyph_info(glyph, layer, name=None, category=None, unicode=None, flags_codepoint=None, spacing=True):
    """Reads one glyph's GlyphInfo from its layer.

    `category` / `unicode`: already read values (else read here);
    `flags_codepoint`: the code point that decides script and kerning when
    the glyph has none of its own (a suffixed variant's base character);
    `spacing`: the glyph takes part in spacing (else `kern` is False).
    """
    info = GlyphInfo()
    info.name = name if name is not None else str(glyph.name)
    info.glyph_id = _string(getattr(glyph, "id", None))
    if category is None:
        category = _string(glyph.category)
    if unicode is None:
        cps = _codepoints(glyph)
        unicode = cps[0] if cps else None
    if flags_codepoint is None:
        flags_codepoint = unicode
    info.unicode = unicode
    info.char = chr(unicode) if unicode is not None else None
    info.category = category
    info.subcategory = _string(getattr(glyph, "subCategory", None))
    try:
        info.case = int(getattr(glyph, "case", 0) or 0)
    except (TypeError, ValueError):
        info.case = 0

    path = layer_path(layer)
    info.path = path
    info.width = _number(layer.width, 0.0)
    rect = path.bounds() if path is not None and path.elementCount() else None
    if rect is not None and (rect.size.width > 0 or rect.size.height > 0):
        x, y = float(rect.origin.x), float(rect.origin.y)
        w, h = float(rect.size.width), float(rect.size.height)
        info.bounds = (x, y, w, h)
        info.lsb = x
        info.rsb = info.width - (x + w)
        info.empty = False
    else:
        info.bounds = (0.0, 0.0, 0.0, 0.0)
        info.lsb = info.rsb = 0.0
        info.empty = True
    info.font_lsb = _number(getattr(layer, "LSB", None), info.lsb)
    info.font_rsb = _number(getattr(layer, "RSB", None), info.rsb)

    script_name = _string(getattr(glyph, "script", None))
    code = kb.script_code(script_name) if script_name else 0
    if not code:
        code = kb.script_code(unicode_script(flags_codepoint))
    info.script = code
    info.rtl = script_iso(code) in kb.RTL_SCRIPTS or getattr(glyph, "direction", None) == GSRTL
    info.kern = bool(spacing) and category not in UNKERNED_CATEGORIES and not technical(flags_codepoint)

    info.left_group = _string(getattr(glyph, "leftKerningGroup", None))
    info.right_group = _string(getattr(glyph, "rightKerningGroup", None))
    # a layer's metrics key wins over the glyph's
    info.left_key = _string(getattr(layer, "leftMetricsKey", None)) or _string(getattr(glyph, "leftMetricsKey", None))
    info.right_key = (_string(getattr(layer, "rightMetricsKey", None)) or
                      _string(getattr(glyph, "rightMetricsKey", None)))
    info.width_key = (_string(getattr(layer, "widthMetricsKey", None)) or
                      _string(getattr(glyph, "widthMetricsKey", None)))
    comps = getattr(layer, "components", None)
    info.components = tuple(n for n in (_string(c.componentName) for c in (comps or ())) if n)
    info.aligned = bool(info.components) and bool(getattr(layer, "isAligned", False))
    return info


# --------------------------------------------------------------- snapshot
class Snapshot(object):
    """One master of one font, copied out of Glyphs for the engine and the
    proofs. Built by a SnapshotReader; complete once the reader is done.

    `names` is the engine's glyph order (the spacing set); `specs[i]` and the
    packer's glyph i belong to `names[i]`. Group ids in the specs index
    `left_group_names` (leftKerningGroup, @MMK_R_) and `right_group_names`
    (rightKerningGroup, @MMK_L_).
    """

    def __init__(self, font, master):
        self.font = font
        self.master_id = str(master.id)
        self.master_name = str(master.name)
        self.upm = float(font.upm)
        self.ascender = _number(getattr(master, "ascender", None), 0.8 * self.upm)
        self.descender = _number(getattr(master, "descender", None), -0.2 * self.upm)
        self.cap_height = _number(getattr(master, "capHeight", None), 0.7 * self.upm)
        self.x_height = _number(getattr(master, "xHeight", None), 0.5 * self.upm)
        self.italic_angle = _number(getattr(master, "italicAngle", None), 0.0)
        self.italic = abs(self.italic_angle) > 1e-6

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
        self.metrics_keyed = set()  # spacing glyphs with a metrics key on this master
        self.aligned = set()  # auto-aligned composites
        self.pinned = set()  # glyphs that keep their sidebearings (keeps_sidebearings)
        self.component_users = {}  # glyph → glyphs (any, on this master) with a component of it
        self.read_ms = 0.0  # main-thread time spent reading
        self.wall_ms = 0.0  # from the first slice to the last
        self.glyph_count_skipped = 0
        self.errors = []
        self._unicode_of = {}  # exporting glyph → first code point
        self._stale = set()

    def glyph_info(self, name):
        """GlyphInfo of any exporting glyph; read lazily for glyphs outside
        the spacing set (and again after invalidate())."""
        info = self.infos.get(name)
        if (info is None or name in self._stale) and name in self.export_names:
            try:
                glyph = self.font.glyphs[name]
                layer = _layer(glyph, self.master_id) if glyph is not None else None
                if layer is None:
                    return info
                info = read_glyph_info(glyph, layer, name, None, self._unicode_of.get(name),
                                       self.flags_codepoint(name), spacing=name in self.index)
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
        else that of the glyph its name extends (arrowright.case → U+2192)."""
        cp = self._unicode_of.get(name)
        if cp is None and "." in name:
            cp = self._unicode_of.get(name.split(".")[0])
        return cp

    def composites_of(self, names):
        """Glyphs of the font that had a component of one of `names` on this
        master when it was read (kk2_apply holds their components in place)."""
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
    """Reads a master into a Snapshot in slices. Call step() from a timer
    until it returns True; `snapshot` is then complete (None if cancelled)."""

    LIST_SHARE = 0.08  # progress shares of passes 1 and 2 (pass 3 has the rest)
    READ_SHARE = 0.9

    def __init__(self, font, master, keep_figure_widths=True):
        if isinstance(master, str):  # a master id
            master = next(m for m in font.masters if str(m.id) == master)
        self.snapshot = None
        self.fraction = 0.0
        self.done = False
        self.cancelled = False
        self._snap = Snapshot(font, master)
        self._keep_figure_widths = keep_figure_widths
        self._pass = 1
        self._pos = 0
        self._glyphs = None
        self._candidates = []  # (glyph, name, category, first code point)
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
        self._glyphs = None
        self._candidates = []

    # pass 1: every glyph, cheaply
    def _list(self, deadline):
        snap = self._snap
        if self._glyphs is None:
            self._glyphs = list(snap.font.glyphs)  # one NSArray fetch
        glyphs = self._glyphs
        n = len(glyphs)
        i = self._pos
        while i < n:
            glyph = glyphs[i]
            i += 1
            if glyph.export:
                name = str(glyph.name)
                snap.export_names.add(name)
                cps = _codepoints(glyph)
                for cp in cps:
                    snap.char_map.setdefault(chr(cp), name)
                if cps:
                    snap._unicode_of[name] = cps[0]
                if name == "space":
                    snap.space_name = name
                category = _string(glyph.category)
                if category in SPACING_CATEGORIES:
                    self._candidates.append((glyph, name, category, cps[0] if cps else None))
                else:
                    self._note_components(glyph, name)
            else:
                self._note_components(glyph, None)
            if time.perf_counter() >= deadline:
                break
        self._pos = i
        if i >= n and snap.space_name is None:
            snap.space_name = snap.char_map.get(" ")
        return i >= n

    def _note_components(self, glyph, name):
        """Notes the components of a glyph outside the spacing set (pass 2
        notes those of the spacing glyphs)."""
        snap = self._snap
        layer = _layer(glyph, snap.master_id)
        comps = getattr(layer, "components", None) if layer is not None else None
        if not comps:
            return
        if name is None:
            name = str(glyph.name)
        for comp in comps:
            base = _string(getattr(comp, "componentName", None))
            if base:
                snap._component_user(base, name)

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
            glyph, name, category, cp = cands[i]
            i += 1
            try:
                self._read_one(glyph, name, category, cp)
            except Exception as e:
                snap.glyph_count_skipped += 1
                if len(snap.errors) < 20:
                    snap.errors.append("%s: %s" % (name, e))
            cost += 0.2 * (time.perf_counter() - t - cost)
        self._glyph_cost = cost
        self._pos = i
        return i >= len(cands)

    def _read_one(self, glyph, name, category, cp):
        snap = self._snap
        layer = _layer(glyph, snap.master_id)
        if layer is None or name in snap.index:
            snap.glyph_count_skipped += 1
            return
        info = read_glyph_info(glyph, layer, name, category, cp, snap.flags_codepoint(name), spacing=True)
        flags = 0
        if info.kern:
            flags |= kb.GLYPH_KERN
        if info.rtl:
            flags |= kb.GLYPH_RTL
        if info.left_group == name:
            flags |= kb.GLYPH_LEFT_KEY
        if info.right_group == name:
            flags |= kb.GLYPH_RIGHT_KEY
        spec = kb.GlyphSpec(name, bezier_contours(info.path), info.width, rhythm_group(glyph, category), flags,
                            info.script, kb.NONE, snap._group_id(True, info.left_group, name),
                            snap._group_id(False, info.right_group, name))
        if not info.empty:
            spec.cur_lsb, spec.cur_rsb = info.lsb, info.rsb
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
        if info.aligned:
            snap.aligned.add(name)

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
        advance, the key stays true (plus "=zero" keeps zero's width) and
        Apply never writes a right side that Glyphs' next metrics update
        would take back."""
        snap = self._snap
        fixed = set()
        if self._keep_figure_widths:
            figures = [n for n in DEFAULT_FIGURES if n in snap.index]
            widths = set(int(round(snap.infos[n].width)) for n in figures)
            if len(figures) >= 5 and len(widths) == 1:
                fixed.update(figures)
            fixed.update(n for n in snap.names if "." in n and is_tabular(n))
        for name in snap.metrics_keyed:
            key = snap.infos[name].width_key
            rule = parse_metrics_key(key)
            if rule is None:
                continue
            fixed.add(name)
            target = rule[1] if rule[0] == "follow" else None
            if target in snap.index:
                fixed.add(target)
        return fixed

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
            _set_rule(spec, True, kb.RULE_FIXED, kb.NONE, info.lsb)
            _set_rule(spec, False, kb.RULE_FIXED, kb.NONE, info.rsb)
            return
        if info.aligned:
            # auto-aligned composites move with their components: the left
            # side with the first spacing component, the right side with the
            # last (Aacute → A, A; f_f_i → f, i)
            spacing = [c for c in info.components if c in snap.index and c != name]
            self._follow_component(spec, info, True, spacing[0] if spacing else None)
            self._follow_component(spec, info, False, spacing[-1] if spacing else None)
        else:
            self._follow_key(i, spec, info, True)
            self._follow_key(i, spec, info, False)

    def _follow_component(self, spec, info, left, target):
        snap = self._snap
        tinfo = snap.infos.get(target) if target is not None else None
        cur = info.lsb if left else info.rsb
        if tinfo is None or tinfo.empty:
            _set_rule(spec, left, kb.RULE_FIXED, kb.NONE, cur)
        else:
            _set_rule(spec, left, kb.RULE_FOLLOW_SAME, snap.index[target], cur - (tinfo.lsb if left else tinfo.rsb))

    def _follow_key(self, i, spec, info, left):
        snap = self._snap
        rule = parse_metrics_key(info.left_key if left else info.right_key)
        if rule is None:
            return
        cur = info.lsb if left else info.rsb
        if rule[0] == "follow":
            _, target, opposite, offset = rule
            target = target or info.name
            t = snap.index.get(target)
            tinfo = snap.infos.get(target) if t is not None else None
            if tinfo is not None and not tinfo.empty and not (t == i and not opposite):
                # Glyphs evaluates the key in its own measure; where that
                # differs from the ink frame (italic sidebearings), it does so
                # by a per-glyph, per-side amount that moving the glyph keeps
                target_left = left != opposite
                offset += self._italic_shift(tinfo, target_left) - self._italic_shift(info, left)
                _set_rule(spec, left, kb.RULE_FOLLOW_OPPOSITE if opposite else kb.RULE_FOLLOW_SAME, t, offset)
                return
        _set_rule(spec, left, kb.RULE_FIXED, kb.NONE, cur)

    def _italic_shift(self, info, left):
        if not self._snap.italic:
            return 0.0
        return (info.font_lsb - info.lsb) if left else (info.font_rsb - info.rsb)

    def _finish(self, t1):
        snap = self._snap
        snap.spacing_names = set(snap.names)
        snap.read_ms = 1000.0 * self._busy
        snap.wall_ms = 1000.0 * (t1 - self._started)
        self.snapshot = snap
        self.done = True
        self._glyphs = None
        self._candidates = []

    def _update_fraction(self):
        if self.done:
            self.fraction = 1.0
            return
        if self._pass == 1:
            n = len(self._glyphs) if self._glyphs is not None else 0
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

    Supports Glyphs' edit-view escapes: "/name" (ended by a space, which is
    consumed, by "/", another white space or a line break) inserts an
    exporting glyph by name.
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
