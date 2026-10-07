#!/usr/bin/env python3
"""
kk2_fonts — load a TrueType/OpenType font with fontTools into Kinetikern2
engine inputs (and v1 inputs, for comparisons), the way the plugin snapshots
a Glyphs master: spacing set = glyphs whose character is a letter, number,
punctuation or symbol; script, kerning eligibility, composite base, rhythm
group and tabular figures per glyph.

Run tools with Glyphs' Python (fontTools comes from the Glyphs repositories):

    GPY="$HOME/Library/Application Support/Glyphs 3/Repositories/GlyphsPythonPlugin/Python.framework/Versions/3.11/bin/python3"
"""

from __future__ import print_function

import os
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
KK2 = os.path.dirname(HERE)
ROOT = os.path.dirname(KK2)
RESOURCES = os.path.join(KK2, "plugin", "Kinetikern2.glyphsPlugin", "Contents", "Resources")
V1_RESOURCES = os.path.join(ROOT, "plugin", "Kinetic SDF Kerning.glyphsPlugin", "Contents", "Resources")
GLYPHS_REPOS = os.path.expanduser("~/Library/Application Support/Glyphs 3/Repositories")
for p in (RESOURCES, os.path.join(GLYPHS_REPOS, "fonttools", "Lib")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fontTools.ttLib import TTCollection, TTFont  # noqa: E402
from fontTools import unicodedata as fud  # noqa: E402

import kk2_bridge as kb  # noqa: E402

# KK2_DYLIB points the tools at another build (e.g. engine/target/release) without installing it
DYLIB = os.environ.get("KK2_DYLIB") or os.path.join(RESOURCES, kb.DYLIB_NAME)
V1_DYLIB = os.path.join(V1_RESOURCES, "libkinetic_kerning.dylib")

# Unicode blocks of symbols that are spaced but never kerned
TECHNICAL_BLOCKS = (
    (0x2190, 0x21FF), (0x2200, 0x22FF), (0x2300, 0x23FF), (0x2400, 0x243F), (0x2440, 0x245F), (0x2460, 0x24FF),
    (0x2500, 0x257F), (0x2580, 0x259F), (0x25A0, 0x25FF), (0x2600, 0x26FF), (0x2700, 0x27BF), (0x27C0, 0x27EF),
    (0x27F0, 0x27FF), (0x2800, 0x28FF), (0x2900, 0x297F), (0x2980, 0x29FF), (0x2A00, 0x2AFF), (0x2B00, 0x2BFF),
    (0xE000, 0xF8FF), (0x1F000, 0x1FAFF),
)
CHARS = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
         ".,:;!?-'\"()/&")


def technical(cp):
    return any(a <= cp <= b for a, b in TECHNICAL_BLOCKS)


def rhythm_group(ch):
    cat = unicodedata.category(ch)
    if cat in ("Lu", "Lt"):
        return kb.GROUP_UPPERCASE
    if cat in ("Ll", "Lm", "Lo"):
        return kb.GROUP_LOWERCASE
    if cat == "Nd":
        return kb.GROUP_FIGURES
    return kb.GROUP_OTHER


def open_font(path, index=0):
    if path.lower().endswith((".ttc", ".otc")):
        return TTCollection(path).fonts[index]
    return TTFont(path)


def tt_contours(font, name):
    glyf = font["glyf"]
    g = glyf[name]
    coords, ends, flags = g.getCoordinates(glyf)
    contours = []
    start = 0
    for end in ends:
        idx = list(range(start, end + 1))
        contour = []
        for k, i in enumerate(idx):
            x, y = coords[i]
            if not flags[i] & 1:
                kind = 2
            else:
                kind = 0 if flags[idx[k - 1]] & 1 else 3
            contour.append((float(x), float(y), kind))
        contours.append(contour)
        start = end + 1
    return contours


def pen_contours(font, name):
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    gs = font.getGlyphSet()
    pen = DecomposingRecordingPen(gs)
    gs[name].draw(pen)
    contours, cur = [], []
    for op, args in pen.value:
        if op == "moveTo":
            cur = [(args[0][0], args[0][1], 0)]
        elif op == "lineTo":
            cur.append((args[0][0], args[0][1], 0))
        elif op == "curveTo":
            for (x, y) in args[:-1]:
                cur.append((x, y, 2))
            cur.append((args[-1][0], args[-1][1], 1))
        elif op == "qCurveTo":
            pts = list(args)
            last = pts[-1]
            for (x, y) in pts[:-1]:
                cur.append((x, y, 2))
            if last is not None:
                cur.append((last[0], last[1], 3))
        elif op in ("closePath", "endPath"):
            if cur:
                # a closed contour's start point repeats at the end
                if len(cur) > 1 and cur[-1][:2] == cur[0][:2] and cur[-1][2] in (0,):
                    cur.pop()
                contours.append([(float(x), float(y), k) for (x, y, k) in cur])
            cur = []
    return contours


class LoadedFont(object):
    def __init__(self, path, index=0, chars=None, limit=None):
        f = open_font(path, index)
        self.path = path
        self.font = f
        self.upm = f["head"].unitsPerEm
        cmap = f.getBestCmap() or {}
        tt = "glyf" in f
        seen = set()
        rows = []
        if chars is not None:
            cps = [ord(c) for c in chars if ord(c) in cmap]
        else:
            cps = sorted(cmap)
        for cp in cps:
            ch = chr(cp)
            cat = unicodedata.category(ch)
            if cat[0] not in "LNPS":
                continue
            name = cmap[cp]
            if name in seen:
                continue
            seen.add(name)
            rows.append((name, cp))
            if limit and len(rows) >= limit:
                break
        self.names = [r[0] for r in rows]
        self.cps = [r[1] for r in rows]
        index_of = dict((n, i) for i, n in enumerate(self.names))
        hmtx = f["hmtx"]
        self.contours = []
        self.bases = []
        for name in self.names:
            self.contours.append(tt_contours(f, name) if tt else pen_contours(f, name))
            base = kb.NONE
            if tt:
                g = f["glyf"][name]
                if g.isComposite() and g.components:
                    base = index_of.get(g.components[0].glyphName, kb.NONE)
            self.bases.append(base)
        self.advances = [hmtx[n][0] for n in self.names]
        figures = [i for i, cp in enumerate(self.cps) if 0x30 <= cp <= 0x39]
        self.tabular = len(figures) == 10 and len(set(self.advances[i] for i in figures)) == 1
        self.figures = set(figures)

    def specs(self, kern_all=False):
        out = []
        for i, name in enumerate(self.names):
            cp = self.cps[i]
            ch = chr(cp)
            script = fud.script(ch)
            flags = 0
            if kern_all or not technical(cp):
                flags |= kb.GLYPH_KERN
            if script in kb.RTL_SCRIPTS:
                flags |= kb.GLYPH_RTL
            if self.tabular and i in self.figures:
                flags |= kb.GLYPH_FIXED_ADVANCE
            s = kb.GlyphSpec(name, self.contours[i], self.advances[i], rhythm_group(ch), flags,
                             kb.script_code(script), self.bases[i])
            out.append(s)
        return out

    def packer(self, kern_all=False):
        p = kb.InputPacker()
        for s in self.specs(kern_all):
            p.add(s)
        return p

    def v1_geometries(self):
        sys.path.insert(0, V1_RESOURCES)
        import kinetikern_bridge as v1
        out = []
        for i, name in enumerate(self.names):
            ch = chr(self.cps[i])
            g = v1.GlyphGeometry(name, self.contours[i], self.advances[i], rhythm_group(ch),
                                 fixed_advance=bool(self.tabular and i in self.figures))
            out.append(g)
        return out


def run_job(job, label="", quiet=True):
    """Waits for a job (tools only), printing progress."""
    import time
    last = -1.0
    while True:
        state, phase, phases, frac, elapsed = job.poll()
        if state != kb.STATE_RUNNING:
            break
        if not quiet and (frac - last > 0.1):
            print("   %s phase %d/%d %3.0f%%  %.1f s" % (label, phase, phases, 100 * frac, elapsed))
            last = frac
        time.sleep(0.02)
    if state != kb.STATE_DONE:
        raise kb.EngineError("job ended in state %d: %s" % (state, job.error()))
    out = job.take()
    job.free()
    return out
