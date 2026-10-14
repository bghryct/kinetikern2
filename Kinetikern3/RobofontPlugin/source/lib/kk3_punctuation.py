# encoding: utf-8
"""
kk3_punctuation — punctuation by convention (the window's Punctuation switch).

Kinetikern3 reads every glyph by its shape, and punctuation is where that
reading and designers part most: measured against 1,594 Google Fonts
families, pairs with a punctuation mark sit about 1.7 times further from
their designers than pairs of letters. Two things account for most of it:

- each mark's sides: designers space a period, a hyphen, quotes or a
  parenthesis by conventions of their own, relative to the letters around
  them, that the shapes alone do not give;
- kerning, mark by mark: designers tuck a period or a comma well under a T,
  V, Y or r (about 0.7 of what the shapes suggest), but hardly kern a
  hyphen, a dash or a quote (about a third), and kern two marks side by
  side hardly at all.

With the switch on, the engine moves each punctuation side in Pass 1 by a
blend of the model's own reading of the mark and the font's letter rhythm
(c + m·s/50 + r·ρ/50 units per 1000 em: s the side's sidebearing as the model
sets it, ρ the median sidebearing of the lowercase), learned for text,
display and handwriting faces; and Pass 2 kerns each side of a mark with its
own share (the table's "kern"), a pair the product of its two facing sides',
every other mark or symbol "kern_scale" per side. Metrics keys, auto-aligned
composites, frozen glyphs and kept joins keep their rules, as everywhere.

The table (kk3_punctuation.json) is learned by tools/kk3_punctuation_learn.py
from Spacing QA's measurements; Spacing QA uses the same table.
"""

from __future__ import division, print_function, unicode_literals

import json
import os
import unicodedata

import kk3_bridge as kb
import kk3_harness as kh

TABLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kk3_punctuation.json")
NAN = float("nan")

_table = None
_by_code = None
_cache = {}

# A character Unicode makes canonically the same as a mark (a one-character decomposition) is that mark under
# another code point: the Greek question mark is a semicolon, the ano teleia a middle dot.
CANONICAL_TWINS = {0x037E: 0x003B, 0x0387: 0x00B7}
# Proportional lining figures under a suffix (one.lf, one.pnum), as a font has them when its default figures are
# tabular or oldstyle: the shapes the figures' conventions were learned on. Oldstyle and tabular forms keep theirs.
LINING_SUFFIXES = ("lf", "pnum", "plf")
# Superscripts, subscripts and ordinals (the table's small_forms) go with the figures: the Figures switch. A
# figure's superscript or subscript under a suffix (one.sups, one.sinf) takes its encoded form's conventions. A set
# of them that shares one width (at least 3, as a tabular design has) keeps its widths: the conventions leave it.
FIGURE_NAMES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
SUPERIORS = tuple(n + "superior" for n in FIGURE_NAMES)
INFERIORS = tuple(n + "inferior" for n in FIGURE_NAMES)
SUPS_SUFFIXES = ("sups", "superior")
SINF_SUFFIXES = ("sinf", "subs", "inferior")
# Old-style figures under a suffix (one.osf, one.onum, one.oldstyle; the tabular .tosf keeps its widths) and the precomposed
# fractions (½, by character) go with the figures too: the table's feature_forms, the Figures switch. The fraction
# slash of a master with numerators and denominators sits between them (HarfBuzz sets a fraction typed with U+2044
# so): it takes "fraction.frac", designers' slash in a fraction, under the Punctuation switch; the numerators and
# denominators themselves (one.numr, one.dnom) take theirs, under the Figures switch.
OSF_SUFFIXES = ("osf", "onum", "oldstyle")
NUMR_SUFFIXES = ("numr", "numerator")
DNOM_SUFFIXES = ("dnom", "denominator")
FRACTION_SLASH = 0x2044
# Small caps (a.sc, a.smcp, A.c2sc, and accented ones by their letter: aacute.sc) take the small caps'
# conventions, under a switch of their own.
SC_SUFFIXES = ("sc", "smcp", "c2sc")


def table():
    """The learned conventions (kk3_punctuation.json), read once."""
    global _table
    if _table is None:
        with open(TABLE_PATH) as f:
            _table = json.load(f)
    return _table


def by_code():
    """{code point: mark} of every mark the table knows: its `marks` (each
    mark's characters), else the GF Latin Kernel's punctuation (an older
    table); plus the marks' canonical twins (CANONICAL_TWINS)."""
    global _by_code
    if _by_code is None:
        marks = table().get("marks")
        if marks:
            _by_code = dict((int(cp), name) for name, cps in marks.items() for cp in cps)
        else:
            _by_code = dict((cp, k) for cp, k in kh.CODE_KEY.items() if k in kh.PUNCTUATION)
        for cp, twin in CANONICAL_TWINS.items():
            if twin in _by_code and cp not in _by_code:
                _by_code[cp] = _by_code[twin]
    return _by_code


def figure_marks():
    """The figures the table has conventions for (figures by convention)."""
    return frozenset(table().get("figures", ()))


def feature_marks():
    """The old-style figures, precomposed fractions and the slash in a fraction the table has conventions for."""
    return frozenset(table().get("feature_forms", ()))


_fraction_forms = {}


def has_fraction_forms(snap):
    """A master with numerators and denominators (at least 5 of each: one.numr or one.numerator, one.dnom or
    one.denominator), as a fraction typed with U+2044 gets them."""
    key = (id(snap), getattr(snap, "generation", 0))
    hit = _fraction_forms.get(key)
    if hit is None:
        tails = [n.rpartition(".")[2] for n in snap.names if "." in n]
        hit = (sum(tails.count(t) for t in NUMR_SUFFIXES) >= 5 and
               sum(tails.count(t) for t in DNOM_SUFFIXES) >= 5)
        if len(_fraction_forms) > 16:
            _fraction_forms.clear()
        _fraction_forms[key] = hit
    return hit


def small_caps_marks():
    """The small caps the table has conventions for (the Small caps switch)."""
    return frozenset(n for n in feature_marks() if n.endswith(".sc"))


def has_small_caps():
    return available() and bool(small_caps_marks())


def _letter_of(snap, name):
    """The basic Latin letter a glyph's name stands for (a, A, aacute → a), lowercase, or None."""
    info = snap.infos.get(name)
    ch = chr(info.unicode) if info is not None and info.unicode is not None else (name if len(name) == 1 else None)
    if ch is None:
        return None
    base = unicodedata.normalize("NFD", ch)[0]
    return base.lower() if "a" <= base.lower() <= "z" else None


def small_marks():
    """The superscripts, subscripts and ordinals the table has conventions for (the Figures switch)."""
    return frozenset(table().get("small_forms", ()))


def has_figures():
    return available() and bool(figure_marks())


def resolve(mark):
    """The mark whose conventions `mark` follows: itself, or its twin when too
    few families have it to learn its own (the table's `aliases`)."""
    return table().get("aliases", {}).get(mark, mark) if mark is not None else None


def available():
    return os.path.exists(TABLE_PATH)


def style_of(style):
    """The table's style for a harness style key ("text", "display", "handwriting")."""
    styles = table().get("styles", {})
    return style if style in styles else "text"


def mark_of(snap, i):
    """The table's mark of glyph i: by its code point; .case punctuation by
    its base mark, proportional lining figures (one.lf) by their figure, and
    a figure's superscript or subscript form (one.sups) by the encoded one,
    an old-style figure (one.osf), a numerator (one.numr) or a denominator
    (one.dnom) by its kind, a small cap (a.sc, A.c2sc, aacute.sc) by its
    letter, and the slash of a master with numerators and denominators as
    the slash in a fraction."""
    name = snap.names[i]
    info = snap.infos.get(name)
    if info is None or info.empty:
        return None
    codes = by_code()
    k = codes.get(info.unicode) if info.unicode is not None else None
    if k is not None:
        if info.unicode == FRACTION_SLASH and "fraction.frac" in feature_marks() and has_fraction_forms(snap):
            return "fraction.frac"
        return k
    parts = name.split(".")
    if len(parts) > 1 and parts[-1] in kh.CASE_SUFFIXES:
        base = snap.infos.get(parts[0])
        if base is not None and base.unicode is not None:
            return codes.get(base.unicode)
    if len(parts) == 2 and parts[1] in LINING_SUFFIXES:
        base = snap.infos.get(parts[0])
        k = codes.get(base.unicode) if base is not None and base.unicode is not None else None
        if k is not None and resolve(k) in figure_marks():
            return k
    if len(parts) == 2 and (parts[1] in OSF_SUFFIXES or parts[1] in NUMR_SUFFIXES or parts[1] in DNOM_SUFFIXES):
        base = snap.infos.get(parts[0])
        k = codes.get(base.unicode) if base is not None and base.unicode is not None else None
        form = "osf" if parts[1] in OSF_SUFFIXES else "numr" if parts[1] in NUMR_SUFFIXES else "dnom"
        if k in FIGURE_NAMES and k + "." + form in feature_marks():
            return k + "." + form
    if len(parts) == 2 and parts[1] in SC_SUFFIXES:
        letter = _letter_of(snap, parts[0])
        if letter is not None and letter + ".sc" in small_caps_marks():
            return letter + ".sc"
    if len(parts) == 2 and (parts[1] in SUPS_SUFFIXES or parts[1] in SINF_SUFFIXES):
        base = snap.infos.get(parts[0])
        k = codes.get(base.unicode) if base is not None and base.unicode is not None else None
        if k in FIGURE_NAMES:
            small = (SUPERIORS if parts[1] in SUPS_SUFFIXES else INFERIORS)[FIGURE_NAMES.index(k)]
            if small in small_marks():
                return small
    return None


def _tabular_suffixes(snap, figs):
    """The suffixes of LINING_SUFFIXES whose figures (at least 5 of them) all
    share one width: tabular whatever their name, so their widths stay."""
    widths = {}
    for i, name in enumerate(snap.names):
        parts = name.split(".")
        if len(parts) == 2 and parts[1] in LINING_SUFFIXES:
            k = mark_of(snap, i)
            if k is not None and resolve(k) in figs:
                widths.setdefault(parts[1], []).append(int(round(snap.infos[name].width)))
    return frozenset(s for s, w in widths.items() if len(w) >= 5 and len(set(w)) == 1)


def _one_width_sets(snap):
    """The glyphs of a superscript, subscript, old-style, numerator or
    denominator set that shares one width (at least 3 superscripts or
    subscripts, 5 of the others: a tabular design, whose widths stay). The
    encoded forms are one set, those under each suffix another."""
    sets = {}
    for i, name in enumerate(snap.names):
        k = mark_of(snap, i)
        kind = ("sup" if k in SUPERIORS else "inf" if k in INFERIORS else
                k.rpartition(".")[2] if k and k.rpartition(".")[2] in ("osf", "numr", "dnom") else None)
        if kind is not None:
            info = snap.infos[name]
            key = (kind, None if info.unicode is not None else name.partition(".")[2])
            sets.setdefault(key, []).append((i, int(round(info.width))))
    out = set()
    for (kind, _suffix), members in sets.items():
        if len(members) >= (3 if kind in ("sup", "inf") else 5) and len(set(w for _i, w in members)) == 1:
            out.update(i for i, _w in members)
    return out


def share_of(mark):
    """The kerning shares (left, right) of a mark of the table (its twin's for
    an alias), or None."""
    k = table().get("kern", {}).get(resolve(mark)) if mark is not None else None
    if not k or len(k) != 2 or any(v is None for v in k):
        return None
    return (float(k[0]), float(k[1]))


def engine_arg(snap, style, punctuation=True, figures=True, small_caps=True):
    """(coefficients, shares, kern scale, follow) for kk3_bridge.Engine.solve:
    per glyph of the snapshot, 6 coefficients (c, m, r of its left side, then
    its right side; NaN c = no convention), 2 kerning shares (left, right;
    NaN = kern scale) and the glyph whose sides it takes (a mark's case form,
    `parenleft.case`, follows its base mark as designers space it; NONE: its
    own), cached per snapshot, style and switches. `punctuation`: the
    punctuation's and symbols' conventions, shares and case forms (off: kern
    scale 1, every mark kerned in full); `figures`: the figures' and their
    small forms' (superscripts, subscripts, ordinals), old-style figures'
    and precomposed fractions'; `small_caps`: the small caps'."""
    key = (id(snap), getattr(snap, "generation", 0), style, bool(punctuation), bool(figures), bool(small_caps))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    t = table()
    marks = t.get("styles", {}).get(style_of(style), {})
    none = (NAN, 0.0, 0.0, NAN, 0.0, 0.0)
    coefs = []
    shares = []
    figs = figure_marks()
    caps = small_caps_marks()
    smalls = small_marks() | (feature_marks() - set(["fraction.frac"]) - caps)
    follow = []
    tabular = _tabular_suffixes(snap, figs)
    one_width = _one_width_sets(snap) if smalls else frozenset()
    for i in range(len(snap.names)):
        name = snap.names[i]
        parts = name.split(".")
        base = snap.index.get(parts[0]) if punctuation and len(parts) == 2 and parts[1] in kh.CASE_SUFFIXES else None
        bm = mark_of(snap, base) if base is not None and base != i else None
        follow.append(base if bm is not None and resolve(bm) not in figs else kb.NONE)
        k = mark_of(snap, i)
        if k is not None and len(parts) == 2 and parts[1] in tabular:
            k = None  # figures under a suffix that all share one width: tabular whatever the name, kept
        if i in one_width:
            k = None  # superscripts or subscripts that share one width: kept
        on = k is not None and (small_caps if k in caps else figures if resolve(k) in figs or k in smalls
                                else punctuation)
        shares.append((share_of(k) or (NAN, NAN)) if on else (NAN, NAN))
        c = marks.get(resolve(k)) if on else None
        if not c:
            coefs.append(none)
            continue
        row = []
        for side in c:
            if side and all(v is not None for v in side):
                row.extend(float(v) for v in side)
            else:
                row.extend((NAN, 0.0, 0.0))
        coefs.append(tuple(row))
    arg = (coefs, shares, float(t.get("kern_scale", 1.0)) if punctuation else 1.0, follow)
    if len(_cache) > 16:
        _cache.clear()
    _cache[key] = arg
    return arg


# The word space designers set for an n of a given advance: the median and middle half of space / n advance at
# Regular, measured on Google Fonts (2026-10-11; text faces 1,040 families, display 405, handwriting 148).
# Kinetikern3 does not set the space (it has no outline): the window only says where the font's sits.
WORD_SPACE = {"text": (0.421, 0.395, 0.467), "display": (0.442, 0.361, 0.508), "handwriting": (0.573, 0.474, 0.699)}
WORD_SPACE_WHO = {"text": "text faces'", "display": "display faces'", "handwriting": "handwriting's"}


def word_space_note(space, n_advance, style):
    """A line for the window: the font's word space (font units) against
    what designers of its kind set for an n of `n_advance`; None without
    either."""
    if not space or not n_advance or space <= 0 or n_advance <= 0:
        return None
    k, lo, hi = WORD_SPACE.get(style, WORD_SPACE["text"])
    who = WORD_SPACE_WHO.get(style, WORD_SPACE_WHO["text"])
    span = "most %d–%d" % (round(lo * n_advance), round(hi * n_advance))
    if lo * n_advance <= space <= hi * n_advance:
        return "word space %d, as %s designers set it for an n this wide (%s)" % (round(space), who, span)
    return "word space %d: %s designers set about %d for an n this wide (%s)" % (
        round(space), who, round(k * n_advance), span)


STYLE_PHRASES = {"text": "text faces' conventions", "display": "display faces' conventions",
                 "handwriting": "handwriting's conventions"}


def describe(style):
    """A line for the window: what the switch follows."""
    phrase = STYLE_PHRASES.get(style_of(style), STYLE_PHRASES["text"])
    period = share_of("period")
    if period is None:
        return phrase
    return "%s, a period kerned %d %%" % (phrase, round(100 * period[0]))
