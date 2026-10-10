# encoding: utf-8
"""
kk2_harness — the designer harness.

Kinetikern2 spaces from the outlines alone. Measured against the text fonts on
Google Fonts that its reviewers rate well spaced, it does some things
consistently differently from their designers: it sets parentheses, the
slash, the question and exclamation marks and the ampersand too tight, and
the open sides of E, F, L and T (more so at light weights and in tight
settings); it sets quotes, period, comma and hyphen and the diagonal sides of
A, V, W, Y and K too loose; and some pairs, mostly of punctuation, apart from
what their two sides say. The harness corrects that after the solve, as much
as the data say (strength 100 %) or less:

- every core glyph side (A–Z, a–z, the punctuation of running text) by an
  amount that depends on the Looseness and on the weight (the stem of I, per
  1000 em), passed on to the sides that follow it (metrics keys, auto-aligned
  composites), to accented letters from their base letter, and to .case
  punctuation from its base mark;
- some pairs of those glyphs by a kerning correction.

The corrections are learned by tools/kk2_harness_learn.py from Spacing QA's
reports and live in kk2_harness.json next to this file. Glyphs outside the
core set (other scripts, figures, and the symbols outside the kernel's scored
set: $ ¢ £ ¥ € + − × ÷ = < > # ^ ~ _) are left as the model spaces them.

Display and handwriting faces space their punctuation more openly than text
faces. With their conventions (`style`), the punctuation also gets what the
designers of those categories do, on top of the text faces' corrections.
"""

from __future__ import division, print_function, unicode_literals

import json
import math
import os
import string

import kk2_bridge as kb

TABLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kk2_harness.json")
# the table's glyphs that are not letters: the punctuation and symbols of the
# GF Latin Kernel that Spacing QA scores (figures, the symbols fonts often draw
# at the figure width and the underscore have no corrections)
PUNCTUATION = {"period": ".", "comma": ",", "colon": ":", "semicolon": ";", "exclam": "!", "question": "?",
               "hyphen": "-", "quotesingle": "'", "quotedbl": '"', "parenleft": "(", "parenright": ")",
               "slash": "/", "ampersand": "&", "quoteright": "\u2019",
               "percent": "%", "asterisk": "*", "at": "@", "bracketleft": "[", "backslash": "\\",
               "bracketright": "]", "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}",
               "copyright": "\u00A9", "registered": "\u00AE", "degree": "\u00B0", "periodcentered": "\u00B7",
               "endash": "\u2013", "emdash": "\u2014", "quoteleft": "\u2018", "quotedblleft": "\u201C",
               "quotedblright": "\u201D", "bullet": "\u2022", "ellipsis": "\u2026", "trademark": "\u2122"}
# the table's glyph of a code point
CODE_KEY = dict((ord(c), c) for c in string.ascii_letters)
CODE_KEY.update((ord(ch), name) for name, ch in PUNCTUATION.items())
CASE_SUFFIXES = ("case",)
# the conventions the harness follows: (key, label, the table's category)
STYLES = [("text", "Text faces", None), ("display", "Display", "Display"), ("handwriting", "Handwriting", "Handwriting")]

_table = None


def table():
    """The learned corrections (kk2_harness.json), read once."""
    global _table
    if _table is None:
        with open(TABLE_PATH) as f:
            _table = json.load(f)
    return _table


def available_styles():
    """[(key, label)] of the conventions the table has (text faces always)."""
    cats = table().get("categories", {})
    return [(k, label) for k, label, cat in STYLES if cat is None or cat in cats]


def _style_table(style):
    for k, _label, cat in STYLES:
        if k == style and cat is not None:
            return table().get("categories", {}).get(cat)
    return None


def _flat_polygons(path):
    """An NSBezierPath as polygons of (x, y)."""
    flat = path.bezierPathByFlatteningPath()
    polys, cur = [], []
    for i in range(flat.elementCount()):
        kind, points = flat.elementAtIndex_associatedPoints_(i)
        if kind == 0:  # move to
            if len(cur) > 1:
                polys.append(cur)
            cur = [(float(points[0].x), float(points[0].y))]
        elif kind == 1:  # line to
            cur.append((float(points[0].x), float(points[0].y)))
        elif kind == 3:  # close path
            if len(cur) > 1:
                polys.append(cur)
            cur = []
    if len(cur) > 1:
        polys.append(cur)
    return polys


def ink_run_at_mid(path):
    """Width of the first ink run across an outline at half its height (font
    units): the stem of I or l. None without one."""
    if path is None or not path.elementCount():
        return None
    polys = _flat_polygons(path)
    ys = [y for poly in polys for _, y in poly]
    if not ys:
        return None
    y = 0.5 * (min(ys) + max(ys)) + 0.37  # off any node
    xs = []
    for poly in polys:
        for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
            if (y0 <= y < y1) or (y1 <= y < y0):
                xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
    xs.sort()
    return xs[1] - xs[0] if len(xs) >= 2 else None


def stem_per_1000(snap):
    """The weight the harness reads: the stem of I (else l) per 1000 em, or
    None if the master has neither."""
    for ch in ("I", "l"):
        name = snap.char_map.get(ch)
        info = snap.infos.get(name) if name else None
        if info is None or info.empty:
            continue
        s = ink_run_at_mid(info.path)
        if s and s > 0:
            return s * 1000.0 / snap.upm
    return None


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


class Plan(object):
    """The harness for one snapshot at one Looseness and strength: the shift
    of every glyph side and the pair corrections, in font units. `frozen`:
    glyph indices the solve keeps as they are (the engine does not touch
    them, and neither does the plan). `kept`: (left sides, right sides), the
    glyph indices whose side a connected script keeps as drawn (Keep joins):
    those sides do not move, and a pair of two kept sides keeps its kerning."""

    def __init__(self, snap, looseness, strength, frozen=None, style="text", kept=None):
        t = table()
        extra = _style_table(style)
        self.style = style if extra is not None else "text"
        self.style_label = dict((k, label) for k, label, _c in STYLES)[self.style]
        self.table_id = t.get("id", "")
        ref = (extra or t).get("reference", {})
        self.table_families = int(ref.get("families", 0))
        self.table_observations = int(ref.get("observations", 0))
        self.strength = max(0.0, float(strength))
        lo, hi = t["looseness_range"]
        self.looseness = _clamp(float(looseness), lo, hi)
        stem = stem_per_1000(snap)
        self.stem_measured = stem
        lo, hi = t["stem_range"]
        self.stem = _clamp(stem if stem else t["stem_ref"], lo, hi)
        w = math.log(self.stem / t["stem_ref"])
        f = (1.0, self.looseness, w, self.looseness * w)
        unit = self.strength * snap.upm / 1000.0

        def corr(coefs):
            return unit * sum(a * b for a, b in zip(coefs, f))

        n = len(snap.names)
        # the core glyph of each glyph: by its code point, .case punctuation by
        # its base mark, an accented letter (not auto-aligned) by its base letter
        self.key = [None] * n
        self.exact = [False] * n
        for i, name in enumerate(snap.names):
            info = snap.infos.get(name)
            if info is None or info.empty:
                continue
            k = CODE_KEY.get(info.unicode) if info.unicode is not None else None
            if k is not None:
                self.key[i], self.exact[i] = k, True
                continue
            parts = name.split(".")
            if len(parts) > 1 and parts[-1] in CASE_SUFFIXES:
                base = snap.infos.get(parts[0])
                bk = CODE_KEY.get(base.unicode) if base is not None and base.unicode is not None else None
                if bk in PUNCTUATION:
                    self.key[i], self.exact[i] = bk, True
                    continue
            if info.components:
                base = snap.infos.get(info.components[0])
                bk = CODE_KEY.get(base.unicode) if base is not None and base.unicode is not None else None
                if bk is not None and bk not in PUNCTUATION:
                    self.key[i] = bk
        left_c, right_c = t["left"], t["right"]
        # the style's punctuation, on top of the text faces' corrections
        x_left, x_right = (extra or {}).get("left", {}), (extra or {}).get("right", {})
        own = [[0.0, 0.0] for _ in range(n)]
        for i, k in enumerate(self.key):
            if k is not None:
                own[i] = [(corr(left_c[k]) if k in left_c else 0.0) + (corr(x_left[k]) if k in x_left else 0.0),
                          (corr(right_c[k]) if k in right_c else 0.0) + (corr(x_right[k]) if k in x_right else 0.0)]

        # a side that follows another glyph's side gets that side's shift
        specs = snap.specs
        memo = {}

        def side(i, left, depth=0):
            key = (i, left)
            if key in memo:
                return memo[key]
            if depth > 32:
                return 0.0
            spec = specs[i]
            rule = spec.lsb_rule if left else spec.rsb_rule
            target = spec.lsb_glyph if left else spec.rsb_glyph
            if rule == kb.RULE_FIXED:
                v = 0.0
            elif rule == kb.RULE_FOLLOW_SAME and target != kb.NONE and target < n:
                v = side(target, left, depth + 1)
            elif rule == kb.RULE_FOLLOW_OPPOSITE and target != kb.NONE and target < n:
                v = side(target, not left, depth + 1)
            else:
                v = own[i][0 if left else 1]
            memo[key] = v
            return v

        frozen = frozenset(frozen or ())
        kept_l, kept_r = (frozenset(kept[0]), frozenset(kept[1])) if kept else (frozenset(), frozenset())
        self.sides = [[0.0, 0.0] if i in frozen else
                      [0.0 if i in kept_l else side(i, True), 0.0 if i in kept_r else side(i, False)]
                      for i in range(n)]
        # pair corrections: glyphs that are their core glyph (not accented
        # variants), both kerned
        pairs_t = dict(t["pairs"])
        for name, v in (extra or {}).get("pairs", {}).items():
            pairs_t[name] = pairs_t.get(name, 0.0) + v
        by_key = {}
        for i, k in enumerate(self.key):
            if k is not None and self.exact[i]:
                info = snap.infos.get(snap.names[i])
                if info is not None and info.kern:
                    by_key.setdefault(k, []).append(i)
        self.pairs = []
        self.pair_value = {}
        for name, v in pairs_t.items():
            a, b = name.split(" ")
            if a not in by_key or b not in by_key:
                continue
            value = unit * v
            for i in by_key[a]:
                for j in by_key[b]:
                    if (i in frozen and j in frozen) or (i in kept_r and j in kept_l):
                        continue
                    self.pairs.append((i, j, value))
                    self.pair_value[(i, j)] = value
        self.changed_sides = sum(1 for s in self.sides if abs(s[0]) >= 0.5 or abs(s[1]) >= 0.5)

    def engine_arg(self):
        """What kk2_bridge.Engine.solve takes: (sides, pairs)."""
        return self.sides, self.pairs

    def delta(self, i, j):
        """How much wider (+) the harness makes the gap of pair (i, j), font units."""
        return self.sides[i][1] + self.sides[j][0] + self.pair_value.get((i, j), 0.0)

    def candidates(self):
        """Glyph indices whose pairs the harness changes (core glyphs and their
        .case forms that the font kerns)."""
        return [i for i, k in enumerate(self.key) if k is not None and self.exact[i]]

    def is_letter(self, i):
        k = self.key[i]
        return k is not None and len(k) == 1 and k.isalpha()

    def _in_text(self, i, j):
        """A pair that running text has: not a lowercase letter before a
        capital, not two punctuation marks."""
        li, lj = self.is_letter(i), self.is_letter(j)
        if li and lj:
            return not (self.key[i].islower() and self.key[j].isupper())
        return li or lj

    def top_pairs(self, snap, count=60, kerned_only=True, which="text"):
        """The pairs the harness changes most: [(i, j, delta in font units)],
        largest change first. `which`: "text" (pairs of running text),
        "letters" (two letters, in text order), "punctuation" (a letter and a
        punctuation mark), "all"."""
        idx = [i for i in self.candidates()
               if not kerned_only or getattr(snap.infos.get(snap.names[i]), "kern", False)]
        out = []
        for i in idx:
            for j in idx:
                if which != "all" and not self._in_text(i, j):
                    continue
                if which == "letters" and not (self.is_letter(i) and self.is_letter(j)):
                    continue
                if which == "punctuation" and self.is_letter(i) == self.is_letter(j):
                    continue
                d = self.delta(i, j)
                if abs(d) >= 0.5:
                    out.append((i, j, d))
        out.sort(key=lambda t: -abs(t[2]))
        return out[:count]

    def summary(self):
        if self.strength <= 0:
            return "off"
        stem = ("stem %.0f" % self.stem_measured) if self.stem_measured else "stem not measured (no I or l)"
        style = "" if self.style == "text" else " · %s punctuation" % self.style_label
        return "%d %%%s · %s · Looseness %+.2f · %d glyph sides, %d pairs corrected" % (
            round(100 * self.strength), style, stem, self.looseness, self.changed_sides, len(self.pairs))
