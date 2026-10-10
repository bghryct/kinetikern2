# encoding: utf-8
"""
Headless tests of the Kinetikern2 extension: everything but the windows
(the Joins window's rows are checked with vanilla stubbed), outside
RoboFont, on in-memory UFO fonts (fontParts' fontshell over defcon, what
RoboFont edits) with the real engine.

    python3 tests/test_headless.py [font.ufo | font.ttf ...]

Needs python3 with fontParts, defcon and fontTools, and the engine library in
the built extension (./build.sh). The library must have this Python's
architecture (the universal build has both). glyphsLib is optional: without
it the categories are not compared with Glyphs' glyph database.

main() runs, in this order:

1. test_snapshot: a synthetic font with what Glyphs knows and a UFO does
   not: unencoded glyphs (a.sc, f_i, zero.tf), an accented letter built
   from components, a ligature built from components, a metrics key from
   Glyphs (glyphsLib's lib keys), kerning groups and kerning. The snapshot
   must read categories, groups, aligned composites and rules as the Glyphs
   plugin would.
2. test_glyphs_categories: the snapshot's categories against Glyphs' own
   glyph database (glyphsLib), on the encoded Latin, Greek, Cyrillic,
   punctuation and symbol glyphs: at least 99.5 % the same.
3. test_ink_x: ink_x (the ink measure Apply and Revert use) against the
   pen, on odd shapes (transformed and nested components, a contour of
   off-curve points only, curves past their points, open and one-point
   contours) and on every glyph of the fonts given.
4. test_slant: italics measured along their italic angle: the frame,
   results back in the font's frame, then apply_and_revert on a slanted
   synthetic font.
5. apply_and_revert on the synthetic font, without and with the designer
   harness: solve the whole font, plan, Apply, read back (kerning, groups,
   sidebearings on the ink, composites rigid, followers moved with their
   bases), Revert and compare every glyph (outlines, components, anchors,
   widths), the kerning and the groups with the font before: they must be
   identical, every coordinate to the last bit.
6. test_groups: spacing groups: frozen glyphs keep everything.
7. test_by_category: groups by category (figures, punctuation, symbols and
   each script but Latin, at the main settings; a second run adds nothing),
   and a Looseness on Punctuation that opens the punctuation only.
8. test_conflict: a Revert that keeps a glyph changed after Apply.
9. test_keep_joins: a synthetic connected script: Keep joins keeps every
   join through Apply (read back from the font by ink contact) and the
   font's kerning of a join pair, a period keeps clear of the exit stroke,
   Revert is exact; Space joined letters is counted.
10. test_joins_window: the Joins window's rows: the broken join with the
    kerning that mends it, nothing broken by Keep joins, drawing advice,
    Open Proof in context; Spacing QA's rule on crafted findings (a
    consistent joiner, a hairline gap that nearly touches, a partly
    connected hand, a design whose glyphs touch by construction).
11. test_decorated: an underline drawn exactly from edge to edge: the
    detector finds no overlap, the decoration test finds it, every glyph
    keeps both sides and Keep joins keeps the line whole.
12. test_flush_joins: a script whose strokes meet exactly flush: the
    detector finds no overlap, the touching rule finds it connected, and
    Keep joins keeps every join.
13. apply_and_revert on each font given (a real font's GPOS pair kerning
    turned into UFO groups and pairs), without and with the designer
    harness.
"""

from __future__ import division, print_function, unicode_literals

import copy
import math
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIB = os.path.join(ROOT, "Kinetikern2.roboFontExt", "lib")
SOURCE = os.path.join(ROOT, "source", "lib")
sys.path.insert(0, SOURCE)

import fontParts.fontshell as fontshell  # noqa: E402

import kk2_apply as ka  # noqa: E402
import kk2_bridge as kb  # noqa: E402
import kk2_groups as kg  # noqa: E402
import kk2_harness as kh  # noqa: E402
import kk2_snapshot as ks  # noqa: E402

FAILURES = []


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        FAILURES.append(what)
    return ok


# ------------------------------------------------------------------ fonts
def _box(pen, x0, y0, x1, y1):
    pen.moveTo((x0, y0))
    pen.lineTo((x0, y1))
    pen.lineTo((x1, y1))
    pen.lineTo((x1, y0))
    pen.closePath()


def _bowl(pen, cx, cy, rx, ry):
    k = 0.5523
    pen.moveTo((cx, cy - ry))
    pen.curveTo((cx + k * rx, cy - ry), (cx + rx, cy - k * ry), (cx + rx, cy))
    pen.curveTo((cx + rx, cy + k * ry), (cx + k * rx, cy + ry), (cx, cy + ry))
    pen.curveTo((cx - k * rx, cy + ry), (cx - rx, cy + k * ry), (cx - rx, cy))
    pen.curveTo((cx - rx, cy - k * ry), (cx - k * rx, cy - ry), (cx, cy - ry))
    pen.closePath()


def synthetic_font():
    """A small font with what the snapshot has to work out by itself."""
    f = fontshell.RFont()
    f.info.familyName, f.info.styleName = "Kinetikern Test", "Regular"
    f.info.unitsPerEm, f.info.ascender, f.info.descender = 1000, 750, -250
    f.info.capHeight, f.info.xHeight = 700, 500

    def glyph(name, unicode, width, draw):
        g = f.newGlyph(name)
        g.width = width
        if unicode is not None:
            g.unicodes = [unicode]
        if draw is not None:
            draw(g.getPen())
        return g

    glyph("space", 0x20, 250, None)
    glyph("H", ord("H"), 700, lambda p: (_box(p, 80, 0, 170, 700), _box(p, 530, 0, 620, 700), _box(p, 170, 320, 530, 400)))
    glyph("O", ord("O"), 760, lambda p: _bowl(p, 380, 350, 320, 360))
    glyph("A", ord("A"), 680, lambda p: (p.moveTo((20, 0)), p.lineTo((300, 700)), p.lineTo((380, 700)),
                                         p.lineTo((660, 0)), p.lineTo((570, 0)), p.lineTo((340, 600)),
                                         p.lineTo((110, 0)), p.closePath()))
    glyph("V", ord("V"), 680, lambda p: (p.moveTo((20, 700)), p.lineTo((300, 0)), p.lineTo((380, 0)),
                                         p.lineTo((660, 700)), p.lineTo((570, 700)), p.lineTo((340, 100)),
                                         p.lineTo((110, 700)), p.closePath()))
    glyph("T", ord("T"), 640, lambda p: (_box(p, 20, 620, 620, 700), _box(p, 280, 0, 360, 620)))
    glyph("n", ord("n"), 560, lambda p: (_box(p, 70, 0, 150, 500), _box(p, 400, 0, 480, 420), _box(p, 150, 420, 480, 500)))
    glyph("o", ord("o"), 560, lambda p: _bowl(p, 280, 250, 230, 260))
    glyph("a", ord("a"), 540, lambda p: (_bowl(p, 250, 220, 190, 220), _box(p, 400, 0, 470, 500)))
    glyph("f", ord("f"), 320, lambda p: (_box(p, 60, 0, 140, 720), _box(p, 20, 420, 300, 490)))
    glyph("i", ord("i"), 240, lambda p: (_box(p, 80, 0, 160, 500), _box(p, 80, 600, 160, 690)))
    glyph("x", ord("x"), 520, lambda p: (_box(p, 40, 0, 480, 500),))
    glyph("period", ord("."), 260, lambda p: _box(p, 80, 0, 180, 100))
    glyph("comma", ord(","), 260, lambda p: _box(p, 80, -120, 180, 100))
    acute = glyph("acutecomb", 0x0301, 0, lambda p: _box(p, -60, 560, 40, 680))
    acute.appendAnchor("_top", (0, 520))
    a = f["a"]
    a.appendAnchor("top", (260, 520))
    # an accented letter as Glyphs aligns it: a at the origin, the mark on it
    aacute = glyph("aacute", 0x00E1, 540, None)
    aacute.appendComponent("a", (0, 0))
    aacute.appendComponent("acutecomb", (260, 0))
    # a ligature built from components, laid out one after the other
    fi = glyph("f_i", None, 560, None)
    fi.appendComponent("f", (0, 0))
    fi.appendComponent("i", (320, 0))
    # unencoded: small cap, tabular figure; figures
    glyph("a.sc", None, 480, lambda p: _box(p, 40, 0, 440, 480))
    for k, name in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")):
        glyph(name, 0x30 + k, 560, lambda p, k=k: _box(p, 60 + 4 * k, 0, 500 - 3 * k, 700))
    glyph("zero.tf", None, 600, lambda p: _box(p, 60, 0, 540, 700))
    # a metrics key from Glyphs (glyphsLib keeps it in the lib): Ograve follows O
    og = glyph("Ograve", 0x00D2, 760, lambda p: _bowl(p, 380, 350, 320, 360))
    og.lib["com.schriftgestaltung.Glyphs.glyph.leftMetricsKey"] = "=O"
    og.lib["com.schriftgestaltung.Glyphs.glyph.rightMetricsKey"] = "=O"
    # kerning groups and kerning, the UFO's way
    f.groups["public.kern1.O"] = ["O", "Ograve"]
    f.groups["public.kern2.O"] = ["O", "Ograve"]
    f.groups["public.kern1.T"] = ["T"]
    f.kerning[("public.kern1.T", "o")] = -80
    f.kerning[("A", "V")] = -60
    f.kerning[("public.kern1.O", "A")] = -20
    f.lib["public.glyphOrder"] = list(f.keys())
    return f


def ttf_font(path):
    """A real font as an in-memory UFO: outlines, widths, code points, and its
    GPOS pair kerning (classes become public.kern1 / public.kern2 groups)."""
    from fontTools.ttLib import TTFont
    tt = TTFont(path)
    gs = tt.getGlyphSet()
    f = fontshell.RFont()
    f.info.familyName = tt["name"].getDebugName(1)
    f.info.styleName = tt["name"].getDebugName(2)
    f.info.unitsPerEm = tt["head"].unitsPerEm
    os2 = tt["OS/2"]
    f.info.ascender, f.info.descender = os2.sTypoAscender, os2.sTypoDescender
    f.info.capHeight = getattr(os2, "sCapHeight", 0) or int(0.7 * f.info.unitsPerEm)
    f.info.xHeight = getattr(os2, "sxHeight", 0) or int(0.5 * f.info.unitsPerEm)
    f.info.italicAngle = tt["post"].italicAngle  # as a UFO has it: negative for a font that leans right
    cmap = {}
    for cp, name in tt.getBestCmap().items():
        cmap.setdefault(name, []).append(cp)
    order = tt.getGlyphOrder()
    for name in order:
        g = f.newGlyph(name)
        g.width = tt["hmtx"][name][0]
        if name in cmap:
            g.unicodes = sorted(cmap[name])
        gs[name].draw(g.getPen())
    f.lib["public.glyphOrder"] = order
    # GPOS pair positioning (kern feature lookups of type 2)
    if "GPOS" in tt:
        gpos = tt["GPOS"].table
        kern_lookups = set()
        for fr in gpos.FeatureList.FeatureRecord:
            if fr.FeatureTag == "kern":
                kern_lookups.update(fr.Feature.LookupListIndex)
        left_of, right_of = {}, {}
        pairs = {}
        for li in sorted(kern_lookups):
            lookup = gpos.LookupList.Lookup[li]
            subtables = lookup.SubTable
            if lookup.LookupType == 9:
                subtables = [st.ExtSubTable for st in subtables]
            for st in subtables:
                if getattr(st, "LookupType", 2) != 2 and lookup.LookupType != 9:
                    continue
                if st.Format == 1:
                    for first, ps in zip(st.Coverage.glyphs, st.PairSet):
                        for rec in ps.PairValueRecord:
                            v = getattr(rec.Value1, "XAdvance", 0) if rec.Value1 else 0
                            if v:
                                pairs.setdefault((first, rec.SecondGlyph), v)
                elif st.Format == 2:
                    cov = st.Coverage.glyphs
                    c1 = st.ClassDef1.classDefs
                    c2 = st.ClassDef2.classDefs
                    groups1, groups2 = {}, {}
                    for g in cov:
                        groups1.setdefault(c1.get(g, 0), []).append(g)
                    for g, c in c2.items():
                        groups2.setdefault(c, []).append(g)
                    names1, names2 = {}, {}
                    for c, members in groups1.items():
                        members = [m for m in members if m not in right_of]
                        if not members:
                            continue
                        name = "public.kern1." + members[0]
                        names1[c] = name
                        for m in members:
                            right_of[m] = name
                        f.groups[name] = list(f.groups.get(name, ())) + members
                    for c, members in groups2.items():
                        if c == 0:
                            continue
                        members = [m for m in members if m not in left_of]
                        if not members:
                            continue
                        name = "public.kern2." + members[0]
                        names2[c] = name
                        for m in members:
                            left_of[m] = name
                        f.groups[name] = list(f.groups.get(name, ())) + members
                    for c1i, row in enumerate(st.Class1Record):
                        for c2i, rec in enumerate(row.Class2Record):
                            v = getattr(rec.Value1, "XAdvance", 0) if rec.Value1 else 0
                            if v and c1i in names1 and c2i in names2:
                                pairs.setdefault((names1[c1i], names2[c2i]), v)
        for pair, v in pairs.items():
            f.kerning[pair] = v
    return f


def load_font(path):
    """A .ufo opened in memory (never saved), or a .ttf / .otf turned into one."""
    if path.rstrip("/").lower().endswith(".ufo"):
        return fontshell.RFont(path)
    return ttf_font(path)


# ------------------------------------------------------------------ state
def outline(layer, name):
    """A glyph's decomposed outline: [(operator, points)]."""
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    pen = DecomposingRecordingPen(layer)
    layer[name].draw(pen)
    return pen.value


def shifted_only(a, b):
    """The x shift that turns outline a into b, or None if b is not a
    translation of a along x."""
    if len(a) != len(b):
        return None
    dx = None
    for (op_a, pts_a), (op_b, pts_b) in zip(a, b):
        if op_a != op_b or len(pts_a) != len(pts_b):
            return None
        for pa, pb in zip(pts_a, pts_b):
            if abs(pb[1] - pa[1]) > 1e-6:
                return None
            d = pb[0] - pa[0]
            if dx is None:
                dx = d
            elif abs(d - dx) > 1e-6:
                return None
    return dx or 0.0


def glyph_state(g):
    """Everything Apply may change in a glyph, exactly (a Revert puts back
    every coordinate to the last bit)."""
    # repr: a .glif writes 600 and 600.0 differently (and fontParts' own
    # transformation is all floats: the defcon one is the font's)
    contours = [[(repr(p.x), repr(p.y), p.type) for p in c.points] for c in g.contours]
    comps = [(c.baseGlyph, tuple(repr(v) for v in c.naked().transformation)) for c in g.components]
    anchors = [(a.name, repr(a.x), repr(a.y)) for a in g.anchors]
    return (repr(g.width), contours, comps, anchors)


def font_state(f):
    return {
        "glyphs": dict((name, glyph_state(f[name])) for name in f.keys()),
        "kerning": dict(f.kerning.items()),
        "groups": dict((k, list(v)) for k, v in f.groups.items()),
    }


def diff_state(a, b):
    out = []
    for key in ("kerning", "groups"):
        if a[key] != b[key]:
            ka_, kb_ = a[key], b[key]
            for k in sorted(set(ka_) | set(kb_), key=str):
                if ka_.get(k) != kb_.get(k):
                    out.append((key, k, ka_.get(k), kb_.get(k)))
    for name in sorted(set(a["glyphs"]) | set(b["glyphs"])):
        if a["glyphs"].get(name) != b["glyphs"].get(name):
            out.append(("glyph", name))
    return out


# ------------------------------------------------------------------ engine
def load_engine():
    for folder in (LIB,):
        path = os.path.join(folder, kb.DYLIB_NAME)
        if os.path.exists(path):
            return kb.Engine(path)
    raise SystemExit("no engine library: build the extension first (./build.sh)")


def solve_whole(engine, snap, harness=None, glyph_opts=None):
    job = engine.prepare(snap.packer, snap.upm)
    job.wait(120.0)
    context = job.take()
    job.free()
    spring, repulsion = 1.0, 3.86
    params = kb.make_params(spring=spring, repulsion=repulsion, coupling=1.0, threshold=5.0 * snap.upm / 1000.0,
                            budget=30000, fit_frozen=bool(glyph_opts))
    job = engine.solve(context, params, None, glyph_opts=glyph_opts,
                       harness=harness.engine_arg() if harness is not None else None)
    job.wait(300.0)
    result = job.take()
    job.free()
    return context, result


def read(font):
    t = time.perf_counter()
    snap = ks.SnapshotReader(font).read_all()
    return snap, (time.perf_counter() - t) * 1000.0


# ------------------------------------------------------------------ tests
def test_snapshot(engine):
    print("\n== the synthetic font: what the snapshot works out")
    f = synthetic_font()
    snap, _ms = read(f)
    infos = snap.infos
    check(set(["A", "H", "O", "n", "o", "a", "aacute", "f_i", "a.sc", "zero.tf", "period"]) <= set(snap.names),
          "letters, figures, punctuation and unencoded glyphs are in the spacing set")
    check("acutecomb" not in snap.names and "space" not in snap.names, "marks and the space are not spaced")
    check(infos["a.sc"].category == "Letter" and infos["a.sc"].case == ks.GSSmallcaps, "a.sc is a small-cap letter")
    check(infos["f_i"].category == "Letter" and infos["f_i"].case == ks.GSLowercase, "f_i is a lowercase letter")
    check(infos["zero.tf"].category == "Number", "zero.tf is a figure")
    check("zero.tf" in snap.fixed, "zero.tf keeps its advance (tabular)")
    check(set(("zero", "one", "nine")) <= snap.fixed or len(set(int(infos[n].width) for n in ("zero", "one"))) == 1,
          "figures of one width keep their advance")
    check("aacute" in snap.aligned, "aacute is spaced as an aligned composite")
    spec = snap.specs[snap.index["aacute"]]
    check(spec.lsb_rule == kb.RULE_FOLLOW_SAME and spec.lsb_glyph == snap.index["a"] and
          spec.rsb_rule == kb.RULE_FOLLOW_SAME and spec.rsb_glyph == snap.index["a"], "aacute's sides follow a")
    spec = snap.specs[snap.index["f_i"]]
    check("f_i" not in snap.aligned and spec.lsb_rule == kb.RULE_FREE and spec.rsb_rule == kb.RULE_FREE,
          "f_i (a ligature of components) is spaced as one shape")
    spec = snap.specs[snap.index["Ograve"]]
    check(spec.lsb_rule == kb.RULE_FOLLOW_SAME and spec.lsb_glyph == snap.index["O"] and abs(spec.lsb_value) < 1e-9,
          "Ograve's metrics key (=O) follows O")
    check(infos["O"].right_group == "O" and infos["O"].left_group == "O" and infos["T"].right_group == "T",
          "kerning groups read from public.kern1 / public.kern2")
    check(snap.composites_of(["a"]) >= {"aacute"}, "aacute is known to draw a")
    check(abs(infos["O"].lsb - 60.0) < 1e-6 and abs(infos["O"].rsb - 60.0) < 1e-6, "ink sidebearings of a curve (O: 60/60)")
    from fontTools.pens.boundsPen import BoundsPen
    pen = BoundsPen(f.naked().layers.defaultLayer)
    f.naked()["aacute"].draw(pen)
    check(abs(infos["aacute"].bounds[0] - pen.bounds[0]) < 1e-6, "decomposed composite measured like fontTools' BoundsPen")
    toks = ks.tokenize("Ha /aacute/f_i .", snap)  # "/f_i " — the space ends the name and is consumed
    check(toks == ["H", "a", ks.SPACE, "aacute", "f_i", "period"], "sample text with /name escapes")
    return f, snap


def _pen_x(glyph, layer):
    b = ks.contour_bounds(ks.draw_contours(glyph, layer))
    return (b[0], b[2]) if b is not None else None


def odd_shapes_font():
    """Glyphs that take ink_x off its fast path: mirrored, rotated, scaled
    and nested components, a TrueType contour of off-curve points only,
    quadratic and cubic curves reaching past their on-curve points, an open
    contour, a closed contour of one point."""
    f = fontshell.RFont()
    f.info.unitsPerEm = 1000
    g = f.newGlyph("bowl")
    g.width = 600
    _bowl(g.getPen(), 300, 300, 250, 300)
    g = f.newGlyph("quad")  # control points past the on-curve ones
    g.width = 600
    pen = g.getPen()
    pen.moveTo((100, 0))
    pen.qCurveTo((-40, 150), (60, 420), (300, 500))
    pen.lineTo((520, 0))
    pen.closePath()
    g = f.newGlyph("offcurves")  # a TrueType circle without on-curve points
    g.width = 600
    pen = g.getPen()
    pen.qCurveTo((50, 50), (550, 50), (550, 550), (50, 550), None)
    pen.closePath()
    g = f.newGlyph("cubic")  # a cubic whose extreme lies between its points
    g.width = 600
    pen = g.getPen()
    pen.moveTo((200, 0))
    pen.curveTo((-80, 100), (-80, 500), (200, 600))
    pen.lineTo((450, 300))
    pen.closePath()
    g = f.newGlyph("open")
    g.width = 300
    pen = g.getPen()
    pen.moveTo((10, 10))
    pen.lineTo((200, 300))
    pen.endPath()
    g.appendContour(f["bowl"].contours[0])  # and one closed contour
    g = f.newGlyph("single")
    g.width = 300
    pen = g.getPen()
    pen.moveTo((40, 40))
    pen.closePath()
    for name, t in (("mirrored", (-1, 0, 0, 1, 700.5, 0)), ("rotated", (0.866, 0.5, -0.5, 0.866, 120, -30)),
                    ("scaled", (0.7, 0, 0, 1.3, 33.25, 0)), ("moved", (1, 0, 0, 1, 37.125, -12.5))):
        g = f.newGlyph(name)
        g.width = 800
        g.appendComponent("quad").transformation = t
        g.appendComponent("cubic", offset=(300.1, 0))
    g = f.newGlyph("nested")
    g.width = 900
    g.appendComponent("moved", offset=(11.3, 0))
    g.appendComponent("rotated", offset=(-7, 5))
    g = f.newGlyph("missing")
    g.width = 500
    g.appendComponent("notInFont")
    g.appendComponent("offcurves", offset=(1.5, 0))
    return f


def test_ink_x(fonts):
    """ink_x, the fast ink measure Apply and Revert use, gives the pen's
    numbers (the snapshot measures the same way): to the last bit, but
    where the pen's curve solving leaves a last-bit error on an extreme that
    is an on-curve point (ink_x reads that point: 45, not 44.99999999999999)."""
    print("\n== ink_x against the pen")
    for label, f in [("odd shapes", odd_shapes_font())] + fonts:
        layer = f.naked().layers.defaultLayer
        exact = near = 0
        far = []
        for name in layer.keys():
            a, b = _pen_x(layer[name], layer), ks.ink_x(layer[name], layer)
            if a == b:
                exact += 1
            elif a is not None and b is not None and max(abs(a[0] - b[0]), abs(a[1] - b[1])) <= 1e-9:
                near += 1
            else:
                far.append((name, a, b))
        check(not far, "%s: ink_x gives the pen's ink for all %d glyphs (%d to the last bit, %d within 1e-9; "
                       "off: %s)" % (label, exact + near + len(far), exact, near, far[:3]))


def italic_font(degrees=12.0):
    """The synthetic font slanted: every outline sheared about the baseline,
    the italic angle in its info (a UFO's is counter-clockwise: negative for
    a font that leans right)."""
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    from fontTools.pens.transformPen import TransformPen
    f = synthetic_font()
    t = math.tan(math.radians(degrees))
    layer = f.naked().layers.defaultLayer
    for name in list(f.keys()):
        g = f[name]
        if not g.contours:
            continue  # composites lean with their components
        rec = DecomposingRecordingPen(layer)
        g.draw(rec)
        g.clearContours()
        rec.replay(TransformPen(g.getPen(), (1, 0, t, 1, 0, 0)))
    f.info.italicAngle = -degrees
    return f


def test_slant(engine):
    """An italic is measured along its italic angle: the engine sees its
    outlines sheared upright about half the x-height, and every result comes
    back in the font's frame (a whole-font Apply then puts every glyph where
    the result says, on the slanted ink). An upright font is measured as it
    is: no frame at all."""
    print("\n== measuring along the italic angle")
    up = read(synthetic_font())[0]
    check(up.slant == 0.0 and up.packer.frame is None, "an upright font is measured as it is (no frame)")
    f = italic_font(12.0)
    snap = read(f)[0]
    check(abs(snap.slant - math.tan(math.radians(12.0))) < 1e-12 and snap.slant_pivot == 250.0,
          "a 12° italic is measured along its angle, about half the x-height (slant %.4f, pivot %g)"
          % (snap.slant, snap.slant_pivot))
    o = snap.index["o"]
    sl, sr = snap.frame[o]
    check(abs(sl) > 1.0 or abs(sr) > 1.0, "the frame moves o's sides (%+.1f, %+.1f)" % (sl, sr))
    upright = ks.SnapshotReader(f, along_slant=False).read_all()
    check(upright.slant == 0.0 and upright.packer.frame is None, "Along the italic angle off: measured upright")
    _context, result = solve_whole(engine, snap)
    bad = [snap.names[i] for i, (a, b) in enumerate(zip(result.metrics, result.engine_metrics))
           if a.valid and (abs(a.lsb - (b.lsb - snap.frame[i][0])) > 1e-9 or abs(a.rsb - (b.rsb - snap.frame[i][1])) > 1e-9
                           or a.advance != b.advance)]
    check(not bad, "every result side comes back in the font's frame, the advance unchanged (%s)" % bad[:4])
    result.close()
    _context.close()
    apply_and_revert(engine, italic_font(12.0), "the synthetic font at 12°")


def apply_and_revert(engine, f, label, harness=False, groups=None):
    print("\n== %s: whole font, Apply, read back, Revert" % label)
    before = copy.deepcopy(font_state(f))
    snap, read_ms = read(f)
    plan_h = None
    if harness:
        plan_h = kh.Plan(snap, 0.0, 1.0)
    opts = groups.opts_for(snap.names) if groups is not None else None
    t = time.perf_counter()
    context, result = solve_whole(engine, snap, plan_h, opts)
    solve_ms = (time.perf_counter() - t) * 1000.0
    frozen = groups.frozen_names() if groups is not None else None
    layer = f.naked().layers.defaultLayer
    composites = [n for n in layer.keys() if len(layer[n].components)]
    shapes = dict((n, outline(layer, n)) for n in composites)
    t = time.perf_counter()
    p = ka.plan(snap, result, True, frozen=frozen)
    plan_ms = (time.perf_counter() - t) * 1000.0
    t = time.perf_counter()
    summary, point = ka.apply(f, snap, p)
    apply_ms = (time.perf_counter() - t) * 1000.0
    print("      %d glyphs read in %.0f ms, solved in %.0f ms (%d entries), planned in %.0f ms, applied in %.0f ms"
          % (len(snap.names), read_ms, solve_ms, result.entry_count, plan_ms, apply_ms))
    check(summary["ok"] and not summary.get("error"),
          "%s: Apply reads back as planned (%d kerning pairs, %d removed, %d group sides, %d glyphs re-spaced; "
          "examples %s)" % (label, summary["kerning_entries"], summary["entries_removed"], summary["group_sides_set"],
                            summary["glyphs_respaced"], summary["examples"][:3]))
    # every spacing glyph (rules, followers and frozen glyphs included) now
    # sits where the result puts it, on the ink, within a unit
    layer = f.naked().layers.defaultLayer
    off = []
    frozen = frozen or set()
    for i, name in enumerate(snap.names):
        m = result.metrics[i]
        if not m.valid:
            continue
        lsb, rsb, _w = ks.ink_metrics(layer[name], layer)
        if lsb is None:
            continue
        if name in frozen:
            want_l, want_r = snap.infos[name].lsb, snap.infos[name].rsb
        else:
            want_l, want_r = m.lsb, m.rsb
        spec = snap.specs[i]
        if spec.flags & kb.GLYPH_FIXED_ADVANCE:
            if abs(lsb - want_l) > 1.01:
                off.append((name, "LSB", round(lsb, 1), round(want_l, 1)))
            continue
        if abs(lsb - want_l) > 1.01 or abs(rsb - want_r) > 1.01:
            off.append((name, round(lsb, 1), round(want_l, 1), round(rsb, 1), round(want_r, 1)))
    check(not off, "%s: every glyph is where the result puts it (%d off, e.g. %s)" % (label, len(off), off[:4]))
    # composites stay rigid: every composite's outline only moves as a whole
    # (a mark keeps its place on its base)
    bent = [n for n in composites if shifted_only(shapes[n], outline(layer, n)) is None]
    check(not bent, "%s: %d composites keep their shape (%d bent, e.g. %s)" % (label, len(composites), len(bent),
                                                                              bent[:5]))
    if "aacute" in snap.aligned:
        a, aac = f["a"], f["aacute"]
        check(abs(f["aacute"].width - a.width) < 1e-6 and abs(aac.components[0].transformation[4]) < 1e-6,
              "%s: aacute moved with a (same advance, a at the origin)" % label)
        acute_x = aac.components[1].transformation[4]
        a_moved = a.contours[0].points[0].x - float(before["glyphs"]["a"][1][0][0][0])
        acute_was = float(before["glyphs"]["aacute"][2][1][1][4])
        check(abs(acute_x - (acute_was + a_moved)) < 1e-6,
              "%s: the accent moved with a's outline (%+g)" % (label, a_moved))

    if point is None:
        check(False, "%s: Apply kept no revert point" % label)
        return
    counts = point.restore(overwrite=False)
    after = font_state(f)
    diff = diff_state(before, after)
    check(not diff, "%s: Revert puts every glyph, the kerning and the groups back exactly (%d differences, e.g. %s)"
          % (label, len(diff), diff[:4]))
    check(point.differences()[0] == 0, "%s: the revert point finds nothing left to restore" % label)
    result.close()
    context.close()
    return counts


def script_font(broken=False):
    """A connected script: a–z, each a body with an entry stroke before its
    origin and an exit stroke past its advance, which overlap the next
    letter's (40 units); a period and two capitals that do not join; the
    designer kerned one join pair (c d −6) and a letter before the period."""
    f = fontshell.RFont()
    f.info.familyName, f.info.styleName = "Kinetikern Script Test", "Regular"
    f.info.unitsPerEm, f.info.ascender, f.info.descender = 1000, 750, -250
    f.info.capHeight, f.info.xHeight = 700, 500
    for k, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        w = 240 + 7 * ((k * 5) % 11)  # bodies of different widths
        g = f.newGlyph(ch)
        g.unicodes = [ord(ch)]
        g.width = 200 + w
        p = g.getPen()
        _box(p, -20, 0, 100, 40)  # entry stroke
        _box(p, 100, 0, 170, 500)
        _box(p, 170, 430, 30 + w, 500)
        _box(p, 30 + w, 0, 100 + w, 500)
        _box(p, 100 + w, 0, 220 + w, 40)  # exit stroke
    for name, uni, width, boxes in (("period", ".", 260, [(80, 0, 180, 100)]),
                                    ("H", "H", 700, [(80, 0, 170, 700), (530, 0, 620, 700), (170, 320, 530, 400)]),
                                    ("O", "O", 760, [(80, 0, 680, 80), (80, 620, 680, 700), (80, 0, 160, 700),
                                                     (600, 0, 680, 700)])):
        g = f.newGlyph(name)
        g.unicodes = [ord(uni)]
        g.width = width
        p = g.getPen()
        for b in boxes:
            _box(p, *b)
    f.kerning[("c", "d")] = -6
    f.kerning[("e", "period")] = -12
    if broken:
        f.kerning[("o", "r")] = 60  # r 20 units clear of o's exit stroke: a broken join
    f.lib["public.glyphOrder"] = list(f.keys())
    return f


def test_keep_joins(engine):
    """Keep joins: every join of the script holds through Apply, read back
    from the font by ink contact; join pairs keep the font's kerning; Revert
    is exact. Space joined letters breaks some (counted)."""
    print("\n== a connected script: Keep joins")
    if not engine.features & kb.FEATURE_JOIN_CHECK:
        check(False, "this engine build has no join checker")
        return
    f = script_font()
    before = copy.deepcopy(font_state(f))
    snap, _ms = read(f)
    n = len(snap.names)
    kinds = bytearray(n)
    for i, name in enumerate(snap.names):
        if snap.specs[i].group in (kb.GROUP_LOWERCASE, kb.GROUP_UPPERCASE):
            cp = snap.infos[name].unicode
            kinds[i] = kb.JOINKIND_LOWER if cp is not None and 0x61 <= cp <= 0x7A else kb.JOINKIND_UPPER
    current = [(kb.ENTRY_GLYPH_GLYPH, snap.index[a], snap.index[b], float(v)) for (a, b), v in f.kerning.items()]
    bands = engine.detect_joins(snap.packer, snap.upm, 500.0, kinds, current)
    check(sum(1 for l, r in bands if l or r) >= 26, "the detector finds the joins (%d glyphs)" % sum(1 for l, r in bands if l or r))

    def solve(keep):
        job = engine.prepare(snap.packer, snap.upm, joins=bands, join_kinds=kinds, current=current, keep_joins=keep)
        job.wait(120.0)
        ctx = job.take()
        job.free()
        params = kb.make_params(threshold=5.0, budget=30000, fit_frozen=keep)
        job = engine.solve(ctx, params, None)
        job.wait(300.0)
        res = job.take()
        job.free()
        return ctx, res

    ctx, res = solve(True)
    if engine.features & kb.FEATURE_JOIN_DECORATED:
        check(not engine.join_decorated(ctx), "a script's figures stand apart: not a decoration")
    st, sides = engine.join_check(ctx, res)
    check(st["joins"] >= 26 * 26 and st["broken"] == 0,
          "Keep joins keeps every join (%d of %d, %d kept sides, Looseness matched %+.2f)"
          % (st["kept"], st["joins"], st["kept_sides"], res.fitted_looseness or 0.0))
    cd = res.value(snap.index["c"], snap.index["d"])
    check(abs(cd + 6.0) < 1e-6, "the join pair c d keeps the font's kerning (%g)" % cd)
    az = [snap.index[ch] for ch in "abcdefghijklmnopqrstuvwxyz"]
    joined = [(snap.names[d["left"]], snap.names[d["right"]]) for d in engine.join_pairs(ctx, [(a, b) for a in az for b in az]) if d["joins"]]
    check(len(joined) == 26 * 26, "as drawn every a–z pair joins (%d)" % len(joined))
    period, e = snap.index["period"], snap.index["e"]
    m = res.metrics
    gap = m[e].rsb + m[period].lsb + res.value(e, period)
    check(gap >= 0.0, "a period after a letter keeps clear of its exit stroke (%+.1f)" % gap)
    p = ka.plan(snap, res, True)
    summary, point = ka.apply(f, snap, p)
    check(summary["ok"] and not summary.get("error"), "Apply reads back as planned")
    # the font read again: every join still touches (ink contact)
    snap2, _ms = read(f)
    job = engine.prepare(snap2.packer, snap2.upm, joins=bands, join_kinds=kinds, current=[
        (kb.ENTRY_GLYPH_GLYPH, snap2.index[a], snap2.index[b], float(v)) for (a, b), v in f.kerning.items()
        if a in snap2.index and b in snap2.index], keep_joins=True)
    job.wait(120.0)
    ctx2 = job.take()
    job.free()
    idx = snap2.index
    after = engine.join_pairs(ctx2, [(idx[a], idx[b]) for a, b in joined])
    lost = [(snap2.names[d["left"]], snap2.names[d["right"]]) for d in after if not d["joins"]]
    check(not lost, "after Apply every join still touches, read back from the font (%d of %d lost, e.g. %s)"
          % (len(lost), len(joined), lost[:5]))
    check(f.kerning.get(("c", "d")) == -6, "after Apply the font still kerns c d by -6 (%s)" % f.kerning.get(("c", "d")))
    point.restore(overwrite=False)
    diff = diff_state(before, font_state(f))
    check(not diff, "Revert puts the script back exactly (%d differences, e.g. %s)" % (len(diff), diff[:4]))
    for x in (res, ctx, ctx2):
        x.close()
    # Space joined letters: the bodies spaced, the joins counted
    ctx, res = solve(False)
    st, _sides = engine.join_check(ctx, res)
    print("      Space joined letters: %d of %d joins kept, %d broken" % (st["kept"], st["joins"], st["broken"]))
    check(st["kept"] + st["broken"] == st["joins"], "Space joined letters: the checker counts every join")
    res.close()
    ctx.close()


def _stub_vanilla():
    """A stand-in for vanilla (no AppKit here): the Joins window's controls
    keep what is set on them."""
    import types
    stub = types.ModuleType("vanilla")

    class Control(object):
        def __init__(self, *args, **kwargs):
            self.value, self.selection = None, []

        def set(self, value):
            self.value = value

        def get(self):
            return self.value if self.value is not None else 0

        def getSelection(self):
            return self.selection

        def bind(self, *args):
            pass

        def open(self):
            pass

        def close(self):
            pass

        def makeKey(self):
            pass

    for name in ("FloatingWindow", "TextBox", "SegmentedButton", "Button", "List"):
        setattr(stub, name, type(name, (Control,), {}))
    sys.modules["vanilla"] = stub


def test_joins_window(engine):
    """The Joins window's rows on a script with a broken join (its UI stubbed):
    the broken pair with the kerning that mends it, nothing broken by Keep
    joins, drawing advice, and a proof in context."""
    print("\n== the Joins window")
    if not engine.features & kb.FEATURE_JOIN_CHECK:
        check(False, "this engine build has no join checker")
        return
    _stub_vanilla()
    import kk2_joins_window as kj
    f = script_font(broken=True)
    snap, _ms = read(f)
    n = len(snap.names)
    kinds = bytearray(n)
    for i, name in enumerate(snap.names):
        if snap.specs[i].group in (kb.GROUP_LOWERCASE, kb.GROUP_UPPERCASE):
            cp = snap.infos[name].unicode
            kinds[i] = kb.JOINKIND_LOWER if cp is not None and 0x61 <= cp <= 0x7A else kb.JOINKIND_UPPER
    current = [(kb.ENTRY_GLYPH_GLYPH, snap.index[a], snap.index[b], float(v)) for (a, b), v in f.kerning.items()]
    bands = engine.detect_joins(snap.packer, snap.upm, 500.0, kinds, current)
    job = engine.prepare(snap.packer, snap.upm, joins=bands, join_kinds=kinds, current=current, keep_joins=True)
    job.wait(120.0)
    ctx = job.take()
    job.free()
    job = engine.solve(ctx, kb.make_params(threshold=5.0, fit_frozen=True), None)
    job.wait(300.0)
    res = job.take()
    job.free()

    class Window(object):
        pass

    kw = Window()
    kw.snapshot, kw.context, kw.result, kw.engine = snap, ctx, res, engine
    kw.joins, kw.join_kinds, kw.join_note = bands, kinds, "26 of 29 letters join"
    kw.join_check = engine.join_check(ctx, res)
    kw._result_kind = "whole"
    kw._keeps_joins = lambda: True
    kw.joins_window_closed = lambda: None

    class Host(object):
        lines = None

        def open_proof(self, lines):
            Host.lines = lines

    win = kj.JoinsWindow(kw, host=Host())
    broken = [r for r in win.findings if r["finding"].startswith("broken")]
    check(len(broken) == 1 and broken[0]["what"] == "o r",
          "Findings: the one broken join, o r (%s)" % [(r["what"], r["value"], r["note"]) for r in win.findings][:4])
    check(broken and broken[0]["value"] == "20.0 apart" and "kern -25 joins it" in broken[0]["note"],
          "…20 units apart, and the kerning that joins it (%s; %s)" % (broken[0]["value"] if broken else "–",
                                                                     broken[0]["note"] if broken else "–"))
    check(not win.under, "Under the preview: Keep joins breaks nothing (%d rows)" % len(win.under))
    check("Keep joins" in win.text and "kept" in win.text, "the summary says what the preview keeps (%r)" % win.text.split("\n")[-1][:90])
    win.w.list.selection = [0]
    win.view = kj.FINDINGS
    win._show()
    win.openProof()
    check(Host.lines == [[("n", "o", "r", "n")]], "Open Proof sets the pair in context (%s)" % Host.lines)
    win.view = kj.ADVICE
    win._show()
    check(all(r["what"].split()[-1] in ("left", "right") for r in win.advice),
          "Drawing advice: %d kept sides off the font's rhythm by 5 units or more, e.g. %s"
          % (len(win.advice), [(r["what"], r["value"]) for r in win.advice[:3]]))

    # Spacing QA's rule on crafted findings: a consistent joiner's exceptions
    # are broken (a hairline gap nearly touches); a partly connected hand's
    # non-joins are style, except between two strongly joining sides
    az = win._letters()
    nan = float("nan")
    per = snap.upm / 1000.0

    def row(a, b, joins, gap=nan):
        return {"left": a, "right": b, "joins": joins, "joins_after": joins, "crossing": False,
                "crossing_after": False, "fragile": False, "fix_crosses": False, "close": 50.0 if joins else nan,
                "open": 20.0 if joins else nan, "gap": nan if joins else gap, "fix": nan if joins else -gap - 5.0,
                "height": 100.0 if joins else nan, "delta": 0.0}

    class Crafted(object):
        detail = []

        def __getattr__(self, name):
            return getattr(engine, name)

        def join_pairs(self, context, pairs, result=None, scope=None):
            return Crafted.detail

    def consistent(a, b):
        if (a, b) == (az[0], az[1]):
            return row(a, b, False, 20.0 * per)
        if (a, b) == (az[2], az[3]):
            return row(a, b, False, 2.0 * per)
        return row(a, b, True)

    name = lambda a, b: "%s %s" % (snap.names[a], snap.names[b])
    found = lambda what: [r["what"] for r in win.findings if r["finding"].startswith(what)]
    kw.engine = Crafted()
    Crafted.detail = [consistent(a, b) for a in az for b in az]
    win.refresh()
    check(found("broken") == [name(az[0], az[1])] and found("nearly touch") == [name(az[2], az[3])]
          and not found("not joined"),
          "a consistent joiner: its exception is broken, a 2-unit gap nearly touches (%s; %s)"
          % (found("broken"), found("nearly touch")))
    apart = set((a, b) for a in az[13:] for b in az[:10]) | {(az[10], az[11])}
    Crafted.detail = [row(a, b, (a, b) not in apart, 30.0 * per) for a in az for b in az]
    win.refresh()
    check(found("broken") == [name(az[10], az[11])] and len(found("not joined")) == 130
          and "partly connected hand" in win.text,
          "a partly connected hand (19 %% of its joining pairs apart): the non-join between strong sides broken, "
          "130 listed as style (%d broken, %d listed)" % (len(found("broken")), len(found("not joined"))))
    kw.join_decorated = True
    Crafted.detail = [row(a, b, (a, b) != (az[0], az[1]), 20.0 * per) for a in az for b in az]
    win.refresh()
    check(found("the line does not meet") == [name(az[0], az[1])],
          "a design whose glyphs touch by construction: where the line does not meet (%s)"
          % found("the line does not meet"))
    kw.engine, kw.join_decorated = engine, False
    res.close()
    ctx.close()


def test_glyphs_categories():
    """The snapshot's categories against Glyphs' own glyph database (when
    glyphsLib is here): the encoded glyphs of the scripts and the
    punctuation and symbols Kinetikern2 spaces."""
    try:
        import glyphsLib.glyphdata as gd
    except ImportError:
        print("\n== (glyphsLib is not installed: categories not compared with Glyphs' glyph database)")
        return
    import unicodedata
    print("\n== categories against Glyphs' glyph database")
    gd.get_glyph("a")
    data = gd.GLYPHDATA
    agree = total = 0
    off = []
    for name, attrs in data.names.items():
        uni = attrs.get("unicode")
        if not uni:
            continue
        cp = int(uni, 16)
        western = cp <= 0x058F or 0x1C80 <= cp <= 0x1CBF or 0x1D00 <= cp <= 0x2BFF or 0xA720 <= cp <= 0xA7FF or \
            0xAB30 <= cp <= 0xAB6F or 0xFB00 <= cp <= 0xFB06
        if not western or unicodedata.category(chr(cp)) == "Cn":
            continue
        g = gd.get_glyph(name)
        if g.category is None:
            continue
        total += 1
        mine = ks.glyph_category(name, cp)[0]
        if mine == g.category:
            agree += 1
        elif len(off) < 8:
            off.append((name, g.category, mine))
    check(agree >= 0.995 * total, "the categories of %d of %d encoded Latin, Greek, Cyrillic, punctuation and "
          "symbol glyphs are Glyphs' (e.g. %s)" % (agree, total, off[:5]))


def test_groups(engine):
    print("\n== spacing groups: frozen capitals keep everything")
    f = synthetic_font()
    before = font_state(f)
    snap, _ms = read(f)
    groups = kg.GroupSet()
    caps = groups.add_group("Capitals", kg.MODE_FREEZE)
    capitals = [n for n in snap.names if snap.infos[n].case == ks.GSUppercase]
    groups.assign(capitals, caps.gid)
    opts = groups.opts_for(snap.names)
    context, result = solve_whole(engine, snap, None, opts)
    p = ka.plan(snap, result, True, frozen=groups.frozen_names())
    summary, point = ka.apply(f, snap, p)
    after = font_state(f)
    moved = [n for n in capitals if before["glyphs"][n] != after["glyphs"][n]]
    check(not moved, "frozen glyphs keep their outlines and advances (%d changed: %s)" % (len(moved), moved[:5]))
    keys = set(capitals) | set(k for k in before["groups"] if any(c in before["groups"][k] for c in capitals))
    changed = [k for k in set(before["kerning"]) | set(after["kerning"])
               if k[0] in keys and k[1] in keys and before["kerning"].get(k) != after["kerning"].get(k)]
    check(not changed, "the kerning between frozen glyphs stays (%d changed)" % len(changed))
    point.restore(overwrite=True)
    check(not diff_state(before, font_state(f)), "Revert after a frozen Apply puts everything back")
    result.close()
    context.close()


def test_by_category(engine):
    """Groups by category: figures, punctuation, symbols and each script's
    letters but Latin get groups of their own, at the main settings; a second
    run adds nothing; a Looseness set on Punctuation opens the punctuation
    and leaves the letters' spacing nearly as it was."""
    print("\n== spacing groups by category")
    f = synthetic_font()
    f.newGlyph("percent").unicodes = [ord("%")]
    _box(f["percent"].getPen(), 60, 0, 600, 700)
    f["percent"].width = 660
    for name, cp in (("zhe-cy", 0x0436), ("de-cy", 0x0434), ("ef-cy", 0x0444), ("alpha", 0x03B1), ("beta", 0x03B2),
                     ("gamma", 0x03B3)):
        g = f.newGlyph(name)
        g.unicodes = [cp]
        g.width = 560
        _box(g.getPen(), 60, 0, 500, 500)
    snap, _ms = read(f)
    groups = kg.GroupSet()
    painted = groups.add_group("My figures")
    groups.assign(["zero", "one"], painted.gid)
    added = kg.by_category(groups, kg.snapshot_entries(snap))
    names = [n for n, _k in added]
    member = lambda n: groups.group_of(n).name if groups.group_of(n) is not None else None
    check(names[:3] == ["Figures", "Punctuation", "Symbols"] and "Cyrillic" in names and "Greek" in names,
          "groups by category: Figures, Punctuation, Symbols, then each script (%s)" % names)
    check(member("zero") == "My figures" and member("two") == "Figures" and member("period") == "Punctuation"
          and member("percent") == "Symbols" and member("zhe-cy") == "Cyrillic" and member("beta") == "Greek",
          "each glyph in the group of its kind, the painted ones where they were")
    check(all(member(n) is None for n in ("H", "a", "o", "aacute", "a.sc")), "Latin letters keep the main settings")
    check(all(abs(g.looseness) < 1e-9 and abs(g.force - 100.0) < 1e-9 for g in groups.groups),
          "new groups start at the main settings")
    check(sum(k for _n, k in kg.by_category(groups, kg.snapshot_entries(snap))) == 0, "a second run adds nothing")
    _c0, plain = solve_whole(engine, snap, None, groups.opts_for(snap.names))
    punct = next(g for g in groups.groups if g.name == "Punctuation")
    groups.update(punct.gid, looseness=0.6)
    _c1, opened = solve_whole(engine, snap, None, groups.opts_for(snap.names))
    i = snap.index
    grew = [n for n in ("period", "comma") if opened.metrics[i[n]].advance > plain.metrics[i[n]].advance + 1.0]
    letters = max(abs(opened.metrics[i[n]].advance - plain.metrics[i[n]].advance) for n in ("H", "O", "n", "o", "a"))
    check(grew == ["period", "comma"] and letters < 2.0,
          "Looseness +0.6 on Punctuation opens the period and comma (letters move %.1f units at most)" % letters)
    for r in (plain, opened, _c0, _c1):
        r.close()


def test_conflict(engine):
    print("\n== Revert keeps what was changed after Apply")
    f = synthetic_font()
    snap, _ms = read(f)
    context, result = solve_whole(engine, snap)
    p = ka.plan(snap, result, True)
    summary, point = ka.apply(f, snap, p)
    # the user moves H by hand after Apply
    h = f["H"]
    h.moveBy((7, 0))
    restorer = point.restorer(overwrite=False)
    while not restorer.step(1.0):
        if restorer.waiting:
            check(restorer.conflicts["glyphs"] >= 1, "Revert notices the glyph changed after Apply")
            restorer.resolve(overwrite=False)
    check(restorer.counts["kept_glyphs"] >= 1, "…and keeps that change when asked to")
    result.close()
    context.close()


def underline_font():
    """a–z, 0–9 and a period, each a body over an underline drawn exactly from
    its origin to its advance: every glyph touches its neighbours and none
    overlaps them; the figures share one advance."""
    f = fontshell.RFont()
    f.info.familyName, f.info.styleName = "Kinetikern Underline Test", "Regular"
    f.info.unitsPerEm, f.info.ascender, f.info.descender = 1000, 750, -250
    f.info.capHeight, f.info.xHeight = 700, 500
    glyphs = [(ch, ord(ch), 300 + 10 * (k % 7), (50, 0, 250 + 10 * (k % 7), 500))
              for k, ch in enumerate("abcdefghijklmnopqrstuvwxyz")]
    figures = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
    glyphs += [(name, 0x30 + k, 560, (60, 0, 500, 700)) for k, name in enumerate(figures)]
    glyphs.append(("period", ord("."), 240, (70, 0, 170, 100)))
    for name, uni, width, body in glyphs:
        g = f.newGlyph(name)
        g.unicodes = [uni]
        g.width = width
        p = g.getPen()
        _box(p, *body)
        _box(p, 0, -120, width, -80)  # the underline, edge to edge
    f.lib["public.glyphOrder"] = list(f.keys())
    return f


def test_decorated(engine):
    """An underline drawn exactly from edge to edge: the detector finds no
    overlap, the decoration test finds the glyphs touch by construction, and
    Keep joins keeps every side that touches — figures and the period too —
    so no part of the line breaks."""
    print("\n== a design whose glyphs touch by construction")
    if not engine.features & kb.FEATURE_JOIN_DECORATED:
        check(False, "this engine build has no decoration test")
        return
    snap, _ms = read(underline_font())
    n = len(snap.names)
    kinds = bytearray(n)
    for i, name in enumerate(snap.names):
        if snap.specs[i].group in (kb.GROUP_LOWERCASE, kb.GROUP_UPPERCASE):
            cp = snap.infos[name].unicode
            kinds[i] = kb.JOINKIND_LOWER if cp is not None and 0x61 <= cp <= 0x7A else kb.JOINKIND_UPPER
    bands = engine.detect_joins(snap.packer, snap.upm, 500.0, kinds, [])
    check(not any(left or right for left, right in bands), "the detector finds no joins: nothing overlaps")
    check(engine.detect_decorated(snap.packer, snap.upm, kinds, []),
          "the decoration test finds the glyphs touch by construction")
    job = engine.prepare(snap.packer, snap.upm, joins=bands, join_kinds=kinds, current=[], keep_joins=True)
    job.wait(120.0)
    ctx = job.take()
    job.free()
    check(engine.join_decorated(ctx), "the join checker agrees")
    bits = engine.join_sides(ctx)
    both = kb.JOINSIDE_LEFT_KEPT | kb.JOINSIDE_RIGHT_KEPT
    kept = sum(1 for i in range(n) if bits[i] & both == both)
    check(kept == n, "every glyph keeps both sides, the figures and the period too (%d of %d)" % (kept, n))
    job = engine.solve(ctx, kb.make_params(threshold=5.0, fit_frozen=True), None)
    job.wait(300.0)
    res = job.take()
    job.free()
    st, _sides = engine.join_check(ctx, res)
    check(st["joins"] > 0 and st["broken"] == 0,
          "Keep joins keeps the line whole (%d of %d touching pairs kept)" % (st["kept"], st["joins"]))
    res.close()
    ctx.close()


def flush_font():
    """A script whose strokes meet exactly flush: each letter's exit stroke
    ends at its advance and the next letter's entry stroke starts at its
    origin, so the letters touch without overlapping."""
    f = fontshell.RFont()
    f.info.familyName, f.info.styleName = "Kinetikern Flush Test", "Regular"
    f.info.unitsPerEm, f.info.ascender, f.info.descender = 1000, 750, -250
    f.info.capHeight, f.info.xHeight = 700, 500
    for k, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        w = 240 + 7 * ((k * 5) % 11)
        g = f.newGlyph(ch)
        g.unicodes = [ord(ch)]
        g.width = 200 + w
        p = g.getPen()
        _box(p, 0, 0, 100, 40)  # entry stroke, from the origin
        _box(p, 100, 0, 170, 500)
        _box(p, 170, 430, 30 + w, 500)
        _box(p, 30 + w, 0, 100 + w, 500)
        _box(p, 100 + w, 0, 200 + w, 40)  # exit stroke, to the advance
    g = f.newGlyph("period")
    g.unicodes = [ord(".")]
    g.width = 260
    _box(g.getPen(), 80, 0, 180, 100)
    f.lib["public.glyphOrder"] = list(f.keys())
    return f


def test_flush_joins(engine):
    """Letters that join by touching, without overlapping: the detector finds
    no joins, the touching rule finds the font connected, and Keep joins
    keeps every join."""
    print("\n== a script whose strokes meet flush")
    if not engine.features & kb.FEATURE_JOIN_CONTACT:
        check(False, "this engine build has no touching rule")
        return
    snap, _ms = read(flush_font())
    n = len(snap.names)
    kinds = bytearray(n)
    for i, name in enumerate(snap.names):
        if snap.specs[i].group in (kb.GROUP_LOWERCASE, kb.GROUP_UPPERCASE):
            cp = snap.infos[name].unicode
            kinds[i] = kb.JOINKIND_LOWER if cp is not None and 0x61 <= cp <= 0x7A else kb.JOINKIND_UPPER
    bands = engine.detect_joins(snap.packer, snap.upm, 500.0, kinds, [])
    check(not any(left or right for left, right in bands), "the detector finds no joins: nothing overlaps")
    joining, measured = engine.detect_contact(snap.packer, snap.upm, kinds, [])
    check(measured == 26 and joining == 26, "the touching rule: %d of %d letters join" % (joining, measured))
    check(not engine.detect_decorated(snap.packer, snap.upm, kinds, []), "not a decoration: there are no figures")
    job = engine.prepare(snap.packer, snap.upm, joins=bands, join_kinds=kinds, current=[], keep_joins=True)
    job.wait(120.0)
    ctx = job.take()
    job.free()
    job = engine.solve(ctx, kb.make_params(threshold=5.0, fit_frozen=True), None)
    job.wait(300.0)
    res = job.take()
    job.free()
    st, _sides = engine.join_check(ctx, res)
    check(st["joins"] >= 26 * 26 and st["broken"] == 0,
          "Keep joins keeps every flush join (%d of %d)" % (st["kept"], st["joins"]))
    res.close()
    ctx.close()


def main():
    engine = load_engine()
    print("engine %s, features %d, %d threads" % (engine.version, engine.features, engine.default_threads))
    try:
        f, _snap = test_snapshot(engine)
        test_glyphs_categories()
        test_ink_x([(os.path.basename(path), load_font(path)) for path in sys.argv[1:]])
        test_slant(engine)
        apply_and_revert(engine, f, "synthetic font")
        apply_and_revert(engine, synthetic_font(), "synthetic font with the designer harness", harness=True)
        test_groups(engine)
        test_by_category(engine)
        test_conflict(engine)
        test_keep_joins(engine)
        test_joins_window(engine)
        test_decorated(engine)
        test_flush_joins(engine)
        for path in sys.argv[1:]:
            apply_and_revert(engine, load_font(path), os.path.basename(path))
            apply_and_revert(engine, load_font(path), os.path.basename(path) + " with the designer harness",
                             harness=True)
    except Exception:
        traceback.print_exc()
        FAILURES.append("an exception")
    print("\n%s: %d failure%s" % ("PASSED" if not FAILURES else "FAILED", len(FAILURES),
                                   "" if len(FAILURES) == 1 else "s"))
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
