#!/usr/bin/env python3
"""Learns the designer harness: where Kinetikern2 consistently spaces
differently from designers, as corrections the plugin can apply after a solve.

The data are Spacing QA reports: for each ordered pair of the 88 scored
glyphs of the GF Latin Kernel (letters, punctuation and the symbols spaced by
their shape; not figures, the symbols fonts often draw at the figure width, or
the underscore), the designer's gap minus the bare model's — Kinetikern2
without the harness — at the font's best-fit Looseness, with the font's
overall offset removed (units per 1000 em; + = the designer is looser).
Reports store the bare model beside the harness (Spacing QA's `bare`); a
report checked without the harness gives its residuals as they are. Glyphs
are matched by name: a font without some of them adds to the pairs it has. Observations are the reference fonts at Regular (the library scan)
and, for variable families, at other weights (`spacingqa check --weight W
--json`). Reference fonts: text faces (Sans Serif, Serif), upright,
proportional, Latin primary, within the model's range, and rated at or above
--min-quality by Google Fonts' reviewers (google/fonts' human
/Quality/Spacing tag).

Each observation's residuals are split by median polish into a term for the
right side of each left glyph, the left side of each right glyph, and what is
left of each pair. Side terms are fitted robustly (Huber) as
    a + b*L + c*w + d*L*w
with L the font's best-fit Looseness and w = ln(stem / 85), the stem of `I`
(else `l`) per 1000 em at that weight, each family weighted once. Pair terms
are fitted after the side terms, on what they leave of each pair, and kept
where the median is large (8 units) and 60 % of the observations agree on its
sign. (Fitted before the side terms, a pair the side terms move a lot, such
as a period before v, kept no correction when designers disagree on kerning
it.) Half the families are held out to check how much closer to the
designers the model gets.

Display and handwriting faces space punctuation more openly than text faces.
For each of those categories (--categories), the families rated at or above
--min-quality give a correction of the punctuation alone, on top of the text
table: its sides, and pairs with punctuation. Their letters keep the text
table's corrections.

usage: kk2_harness_learn.py --reports SpacingQA/data/reports --weights DIR
                            --families families.csv --out kk2_harness.json
"""
import argparse
import csv
import datetime
import glob
import hashlib
import json
import math
import os
import re
import sys
from collections import defaultdict

import numpy as np

STEM_REF = 85.0
# a pair correction: at least this large (units per 1000 em), and this share
# of the observations on its side of zero
PAIR_MIN = 8.0
PAIR_AGREE = 0.6
PUNCT = ["period", "comma", "colon", "semicolon", "exclam", "question", "hyphen", "quotesingle", "quotedbl",
         "parenleft", "parenright", "slash", "ampersand", "quoteright",
         # the rest of the kernel's scored punctuation and symbols
         "percent", "asterisk", "at", "bracketleft", "backslash", "bracketright", "grave", "braceleft", "bar",
         "braceright", "copyright", "registered", "degree", "periodcentered", "endash", "emdash", "quoteleft",
         "quotedblleft", "quotedblright", "bullet", "ellipsis", "trademark"]
LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + [chr(c) for c in range(ord("a"), ord("z") + 1)]
# the scored glyphs of the kernel, in Spacing QA's order (crate::glyphset)
NAMES = LETTERS + PUNCT


def residuals_by_name(r):
    """The bare model's residual matrix over NAMES (NaN where a glyph is
    missing or a pair not measurable), or None when the report has no bare
    model (checked with the harness before reports stored it)."""
    d, s = r["detail"], r["summary"]
    glyphs = d["glyphs"]
    k = len(glyphs)
    pg = d["pair_glyphs"]
    m = len(pg)
    grid = np.array([np.nan if v is None else v / 10.0 for v in d["residuals"]], dtype=float).reshape(m, m)
    at = {name: i for i, name in enumerate(NAMES)}
    rows = np.array([at.get(glyphs[g]["name"], -1) for g in pg])
    if s.get("bare") and all(g.get("bare") or not g.get("dev") for g in glyphs):
        # from the report's designer and bare spacing, where its grid is measurable
        per = 1000.0 / float(r["font"].get("upm") or 1000.0)
        nanpair = lambda v: [np.nan, np.nan] if v is None else [np.nan if x is None else x for x in v]
        des = np.array([nanpair(g.get("designer")) for g in glyphs], dtype=float)
        bare = np.array([nanpair(g.get("bare")) for g in glyphs], dtype=float)
        KD = np.zeros((k, k))
        for a_, b_, v in d["kerning"]["designer"]:
            KD[a_, b_] = v
        KB = np.zeros((k, k))
        for a_, b_, v in d["kerning"]["best"]:
            KB[a_, b_] = v
        for a_, b_, v in d["kerning"].get("bare", []):
            KB[a_, b_] = v
        gd = des[:, 1][:, None] + des[:, 0][None, :] + KD
        gb = bare[:, 1][:, None] + bare[:, 0][None, :] + KB
        R = (gd - gb) * per - float(s["bare"]["offset"])
        grid = np.where(np.isfinite(grid), R[np.ix_(pg, pg)], np.nan)
    elif r.get("harness"):
        return None
    M = np.full((len(NAMES), len(NAMES)), np.nan)
    ok = rows >= 0
    M[np.ix_(rows[ok], rows[ok])] = grid[np.ix_(ok, ok)]
    return M


def flatten(d, steps=12):
    """SVG path (absolute M L Q C Z, as Spacing QA writes it) → polygons."""
    toks = re.findall(r"[MLQCZ]|-?\d*\.?\d+(?:e-?\d+)?", d)
    polys, cur, i, p = [], [], 0, (0.0, 0.0)
    while i < len(toks):
        t = toks[i]
        i += 1
        if t == "M":
            if cur:
                polys.append(cur)
            p = (float(toks[i]), float(toks[i + 1]))
            i += 2
            cur = [p]
        elif t == "L":
            p = (float(toks[i]), float(toks[i + 1]))
            i += 2
            cur.append(p)
        elif t == "Q":
            c, e = (float(toks[i]), float(toks[i + 1])), (float(toks[i + 2]), float(toks[i + 3]))
            i += 4
            for k in range(1, steps + 1):
                s = k / steps
                cur.append(((1 - s) ** 2 * p[0] + 2 * (1 - s) * s * c[0] + s * s * e[0],
                            (1 - s) ** 2 * p[1] + 2 * (1 - s) * s * c[1] + s * s * e[1]))
            p = e
        elif t == "C":
            c1, c2 = (float(toks[i]), float(toks[i + 1])), (float(toks[i + 2]), float(toks[i + 3]))
            e = (float(toks[i + 4]), float(toks[i + 5]))
            i += 6
            for k in range(1, steps + 1):
                s = k / steps
                u = 1 - s
                cur.append((u ** 3 * p[0] + 3 * u * u * s * c1[0] + 3 * u * s * s * c2[0] + s ** 3 * e[0],
                            u ** 3 * p[1] + 3 * u * u * s * c1[1] + 3 * u * s * s * c2[1] + s ** 3 * e[1]))
            p = e
        elif t == "Z":
            if cur:
                polys.append(cur)
            cur = []
        else:
            i -= 1  # implicit repeat of the last command: not written by Spacing QA
            break
    if cur:
        polys.append(cur)
    return polys


def stem_at_mid(d):
    """Width of the first ink run across the glyph at half its height (font
    units), the stem of I or l; None if there is none."""
    polys = flatten(d)
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
    if len(xs) < 2:
        return None
    return xs[1] - xs[0]


def stem_of(report):
    glyphs = {g["name"]: g for g in report["detail"]["glyphs"]}
    upm = float(report["font"].get("upm") or 1000.0)
    for name in ("I", "l"):
        g = glyphs.get(name)
        if g and g.get("d"):
            s = stem_at_mid(g["d"])
            if s and s > 0:
                return s * 1000.0 / upm
    return None


def polish(M, iters=10):
    E = M.copy()
    row = np.zeros(M.shape[0])
    col = np.zeros(M.shape[1])
    for _ in range(iters):
        rm = np.nanmedian(E, axis=1)
        rm = np.where(np.isfinite(rm), rm, 0.0)
        E -= rm[:, None]
        row += rm
        d = np.nanmedian(col)
        col -= d
        cm = np.nanmedian(E, axis=0)
        cm = np.where(np.isfinite(cm), cm, 0.0)
        E -= cm[None, :]
        col += cm
        d = np.nanmedian(row)
        row -= d
    return row, col, E


def huber(X, y, w, k=1.345, iters=30):
    """Weighted Huber regression; rows with NaN y are left out."""
    ok = np.isfinite(y)
    X, y, w = X[ok], y[ok], w[ok]
    beta = np.linalg.lstsq(X * np.sqrt(w)[:, None], y * np.sqrt(w), rcond=None)[0]
    for _ in range(iters):
        r = y - X @ beta
        s = 1.4826 * np.median(np.abs(r - np.median(r))) or 1.0
        u = np.abs(r) / (k * s)
        hw = np.where(u <= 1, 1.0, 1.0 / np.maximum(u, 1e-12))
        ww = w * hw
        nb = np.linalg.lstsq(X * np.sqrt(ww)[:, None], y * np.sqrt(ww), rcond=None)[0]
        if np.max(np.abs(nb - beta)) < 1e-4:
            beta = nb
            break
        beta = nb
    return beta


def features(L, w):
    return np.stack([np.ones_like(L), L, w, L * w], axis=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports", required=True)
    ap.add_argument("--weights", default=None)
    ap.add_argument("--families", required=True)
    ap.add_argument("--min-quality", type=float, default=70)
    ap.add_argument("--categories", default="Display,Handwriting",
                    help="categories that get a punctuation table of their own")
    ap.add_argument("--pair-min", type=float, default=PAIR_MIN, help="a pair correction's smallest |median| (units per 1000 em)")
    ap.add_argument("--pair-agree", type=float, default=PAIR_AGREE, help="the share of observations on its side of zero")
    ap.add_argument("--pair-glyphs", choices=["all", "text"], default="all",
                    help="text: pair corrections only between letters and the punctuation of running text "
                         "(core punctuation, quotes, dashes, ellipsis); the symbols keep their side corrections")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    human = {}
    for row in csv.reader(open(a.families, encoding="utf-8")):
        if len(row) == 4 and row[2] == "/Quality/Spacing" and not row[1]:
            human[row[0]] = float(row[3])

    extra = [c.strip() for c in a.categories.split(",") if c.strip()]
    names = None
    obs = []  # (family, residual matrix, L, stem, label): text faces
    cat_obs = defaultdict(list)  # category → the same, at Regular
    # a weight checked on its own (`spacingqa check`) knows no catalog: its
    # family's library report gives the category, primary script and name
    catalog = {}

    def take(r, label, slug=None):
        nonlocal names
        s, d, f = r.get("summary"), r.get("detail"), r["font"]
        if slug is not None:
            known = catalog.get(slug)
            if known is None:
                return
            f = dict(f, category=known[0], primary_script=known[1], family=known[2])
        if not s or not d or not d.get("residuals") or f.get("italic") or f.get("monospaced"):
            return
        cat = f.get("category")
        text = cat in ("Sans Serif", "Serif")
        if not (text or (cat in extra and label == "regular")) or f.get("primary_script") not in (None, "", "Latn"):
            return
        if (human.get(f["family"]) or 0) < a.min_quality or abs(s["best_looseness"]) >= 6 - 1e-6:
            return
        names = NAMES
        stem = stem_of(r)
        if not stem:
            return
        res = residuals_by_name(r)
        if res is None:
            return
        (obs if text else cat_obs[cat]).append((f["family"], res, float(s["best_looseness"]), stem, label))

    for p in sorted(glob.glob(os.path.join(a.reports, "*.json"))):
        r = json.load(open(p))
        f = r["font"]
        catalog[os.path.basename(p)[:-5]] = (f.get("category"), f.get("primary_script"), f.get("family"))
        take(r, "regular")
    n_regular = len(obs)
    if a.weights:
        for p in sorted(glob.glob(os.path.join(a.weights, "*.json"))):
            slug = os.path.basename(p).split("@")[0]
            try:
                for r in json.load(open(p)):
                    take(r, os.path.basename(p), slug)
            except (ValueError, KeyError):
                continue
    fams = sorted({o[0] for o in obs})
    print(f"{len(obs)} observations: {n_regular} at Regular, {len(obs) - n_regular} at other weights; {len(fams)} families")
    stems = np.array([o[3] for o in obs])
    Ls = np.array([o[2] for o in obs])
    print(f"stem per 1000 em: 5th {np.percentile(stems, 5):.0f}, median {np.median(stems):.0f}, 95th {np.percentile(stems, 95):.0f};"
          f" Looseness 5th {np.percentile(Ls, 5):+.2f}, median {np.median(Ls):+.2f}, 95th {np.percentile(Ls, 95):+.2f}")

    n = len(names)
    rows, cols, Es = [], [], []
    for _, res, _, _, _ in obs:
        r, c, E = polish(res)
        rows.append(r)
        cols.append(c)
        Es.append(E)
    rows, cols, Es = np.array(rows), np.array(cols), np.array(Es)
    w_stem = np.log(stems / STEM_REF)
    fam_count = defaultdict(int)
    for o in obs:
        fam_count[o[0]] += 1
    weight = np.array([1.0 / fam_count[o[0]] for o in obs])

    X3 = np.array([o[1] for o in obs])

    TEXT = set(LETTERS) | {"period", "comma", "colon", "semicolon", "exclam", "question", "hyphen", "quotesingle",
                           "quotedbl", "parenleft", "parenright", "slash", "ampersand", "quoteright", "quoteleft",
                           "quotedblleft", "quotedblright", "endash", "emdash", "ellipsis"}
    allowed = np.ones((len(NAMES), len(NAMES)), bool)
    if a.pair_glyphs == "text":
        t = np.array([nm in TEXT for nm in NAMES])
        allowed = t[:, None] & t[None, :]

    def pair_terms(R, mask=None):
        """Pair corrections from what the side terms leave (R: observations x
        pairs): the median where it is large and its sign is shared."""
        Em = np.nanmedian(R, axis=0)
        same = np.nanmean(np.sign(R) == np.sign(Em)[None], axis=0)
        keep = (np.abs(Em) >= a.pair_min) & (same >= a.pair_agree) & allowed
        if mask is not None:
            keep &= mask
        return np.where(keep, Em, 0.0)

    def learn(sel):
        X = features(Ls[sel], w_stem[sel])
        right = np.array([huber(X, rows[sel, i], weight[sel]) for i in range(n)])
        left = np.array([huber(X, cols[sel, j], weight[sel]) for j in range(n)])
        side = (X @ right.T)[:, :, None] + (X @ left.T)[:, None, :]
        return right, left, pair_terms(X3[sel] - side)

    def predict(right, left, pairs, L, w):
        f = np.array([1.0, L, w, L * w])
        return (right @ f)[:, None] + (left @ f)[None, :] + pairs

    # the cross-check: families split in two
    fset = {fam: k % 2 for k, fam in enumerate(fams)}
    fold = np.array([fset[o[0]] for o in obs])
    pi = [names.index(p) for p in PUNCT]
    letters = [i for i, nm in enumerate(names) if len(nm) == 1 and nm.isalpha()]
    par = [names.index("parenleft"), names.index("parenright")]
    tot = defaultdict(lambda: [0.0, 0.0, 0])
    PM = np.zeros((n, n), bool)
    PM[pi, :] = True
    PM[:, pi] = True
    held_before, held_after = [], []

    def off(Ms):
        """Punctuation pairs whose median over `Ms` is 10 units or more off:
        (tighter than the designers, looser)."""
        med = np.nanmedian(np.array(Ms), axis=0)
        return int(((med >= 10) & PM).sum()), int(((med <= -10) & PM).sum())

    def add(key, before, after):
        ok = np.isfinite(before)
        t = tot[key]
        t[0] += float(np.abs(before[ok]).sum())
        t[1] += float(np.abs(after[ok]).sum())
        t[2] += int(ok.sum())

    for k in (0, 1):
        right, left, pairs = learn(fold != k)
        for i in np.where(fold == k)[0]:
            o = obs[i]
            X = o[1]
            C = predict(right, left, pairs, o[2], w_stem[i])
            Y = X - C
            held_before.append(X)
            held_after.append(Y)
            add("all pairs", X, Y)
            mask = np.zeros_like(X, bool)
            mask[pi, :] = True
            mask[:, pi] = True
            add("pairs with punctuation", X[mask], Y[mask])
            pm = np.zeros_like(X, bool)
            pm[par, :] = True
            pm[:, par] = True
            add("pairs with a parenthesis", X[pm], Y[pm])
            add("letter pairs", X[np.ix_(letters, letters)], Y[np.ix_(letters, letters)])
            if o[3] < 50:
                add("light weights (stem < 50), all pairs", X, Y)
                ef = [names.index("E"), names.index("F")]
                add("light weights: E and F before a letter", X[np.ix_(ef, letters)], Y[np.ix_(ef, letters)])
            if o[2] < -0.2:
                add("tight fonts (L < -0.2), all pairs", X, Y)
    print("\nCross-check, half the families held out (mean |designer - model|, units per 1000 em):")
    for key, (b, af, c) in tot.items():
        print(f"  {key:44} {b / c:6.1f} → {af / c:6.1f}  ({100 * (1 - af / b):.0f}% closer, {c} pairs)")
    (tb, lb), (ta, la) = off(held_before), off(held_after)
    print(f"  punctuation pairs 10 units or more tighter than the designers: {tb} → {ta}; looser: {lb} → {la}")

    right, left, pairs = learn(np.ones(len(obs), bool))
    print("\nSide corrections at Regular (stem 85), tight / standard / loose (L = -0.5 / 0 / +0.5), and at a light weight (stem 30):")
    show = ["parenleft", "parenright", "slash", "question", "exclam", "ampersand", "quotedbl", "quoteright", "period",
            "comma", "hyphen", "E", "F", "L", "T", "A", "V", "W", "Y", "K", "r", "f", "t", "bracketleft",
            "bracketright", "braceleft", "bar", "backslash", "bullet", "periodcentered", "endash", "emdash",
            "quoteleft", "quotedblleft", "ellipsis", "at", "percent", "asterisk", "trademark", "degree"]
    for nm in show:
        i = names.index(nm)
        line = []
        for side, B in (("left", left), ("right", right)):
            vals = [B[i] @ np.array([1, L, 0, 0]) for L in (-0.5, 0, 0.5)]
            light = B[i] @ np.array([1, 0, math.log(30 / STEM_REF), 0])
            line.append(f"{side} {vals[0]:+5.1f} {vals[1]:+5.1f} {vals[2]:+5.1f} | light {light:+5.1f}")
        print(f"  {nm:11} " + "    ".join(line))
    def learn_category(cobs, base):
        """The punctuation of a category, on top of the text table `base`:
        its sides (constant: most of these faces are checked at Regular
        only), then pairs with punctuation, from what is left."""
        right, left, pairs = base
        R = np.array([o[1] - predict(right, left, pairs, o[2], math.log(o[3] / STEM_REF)) for o in cobs])
        rr, cc = [], []
        for m in R:
            r, c, _ = polish(m)
            rr.append(r)
            cc.append(c)
        rr, cc = np.array(rr), np.array(cc)
        o_right, o_left = np.zeros(n), np.zeros(n)
        o_right[pi] = np.nanmedian(rr[:, pi], axis=0)
        o_left[pi] = np.nanmedian(cc[:, pi], axis=0)
        R2 = R - o_right[None, :, None] - o_left[None, None, :]
        return o_right, o_left, pair_terms(R2, PM), R

    base = (right, left, pairs)
    categories = {}
    for cat in extra:
        cobs = cat_obs.get(cat, [])
        cf = sorted({o[0] for o in cobs})
        if len(cf) < 40:
            print(f"\n{cat}: {len(cf)} families rated {a.min_quality:g} or more: too few for a table of its own")
            continue
        half = {fam: k % 2 for k, fam in enumerate(cf)}
        before, after, b_abs, a_abs, cnt = [], [], 0.0, 0.0, 0
        for k in (0, 1):
            o_r, o_l, o_p, _ = learn_category([o for o in cobs if half[o[0]] != k], base)
            for o in (o for o in cobs if half[o[0]] == k):
                X = o[1] - predict(*base, o[2], math.log(o[3] / STEM_REF))
                Y = X - o_r[:, None] - o_l[None, :] - o_p
                before.append(X)
                after.append(Y)
                ok = np.isfinite(X) & PM
                b_abs += float(np.abs(X[ok]).sum())
                a_abs += float(np.abs(Y[ok]).sum())
                cnt += int(ok.sum())
        (tb, lb), (ta, la) = off(before), off(after)
        print(f"\n{cat}: {len(cf)} families rated {a.min_quality:g} or more, half held out, on top of the text table:")
        print(f"  pairs with punctuation, mean |designer - model| {b_abs / cnt:.1f} → {a_abs / cnt:.1f}")
        print(f"  punctuation pairs 10 units or more tighter than the designers: {tb} → {ta}; looser: {lb} → {la}")
        o_r, o_l, o_p, _ = learn_category(cobs, base)
        print("  sides (+ = more room):", ", ".join(f"{names[i]} {o_l[i]:+.0f}/{o_r[i]:+.0f}" for i in pi))
        categories[cat] = (o_r, o_l, o_p, len(cf), len(cobs))

    kept = [(pairs[i, j], names[i], names[j]) for i in range(n) for j in range(n) if pairs[i, j] != 0]
    kept.sort(key=lambda t: -abs(t[0]))
    print(f"\n{len(kept)} pair corrections, the largest:", ", ".join(f"{l} {r} {v:+.0f}" for v, l, r in kept[:16]))

    if a.out:
        cats = {}
        for cat, (o_r, o_l, o_p, nf, no) in categories.items():
            cats[cat] = {
                "reference": {"observations": no, "families": nf, "min_quality": a.min_quality},
                "left": {names[j]: [round(float(o_l[j]), 3), 0.0, 0.0, 0.0] for j in pi},
                "right": {names[i]: [round(float(o_r[i]), 3), 0.0, 0.0, 0.0] for i in pi},
                "pairs": {f"{names[i]} {names[j]}": round(float(o_p[i, j]), 1)
                          for i in range(n) for j in range(n) if o_p[i, j] != 0},
            }
        out = {
            "format": "kinetikern2-harness/2",
            "glyph_set": "GF Latin Kernel (scored glyphs)",
            "about": "Where Kinetikern2 spaces differently from designers, learned from Spacing QA reports of text "
                     "fonts on Google Fonts rated well spaced by its reviewers. Units per 1000 em; + = more space "
                     "than the model. A side's correction is a + b*L + c*w + d*L*w, L the Looseness, "
                     "w = ln(stem / %g), stem = the ink width of I (else l) at half its height, per 1000 em. "
                     "Pairs are corrected after the sides. `categories` adds, for display and handwriting faces, "
                     "corrections of their punctuation on top of these." % STEM_REF,
            "stem_ref": STEM_REF,
            "looseness_range": [round(float(np.percentile(Ls, 2)), 2), round(float(np.percentile(Ls, 98)), 2)],
            "stem_range": [round(float(np.percentile(stems, 2)), 1), round(float(np.percentile(stems, 98)), 1)],
            "reference": {"observations": len(obs), "families": len(fams), "min_quality": a.min_quality},
            "pair_rule": {"min": a.pair_min, "agree": a.pair_agree, "glyphs": a.pair_glyphs},
            "glyphs": names,
            "left": {names[j]: [round(float(x), 3) for x in left[j]] for j in range(n)},
            "right": {names[i]: [round(float(x), 3) for x in right[i]] for i in range(n)},
            "pairs": {f"{l} {r}": round(float(v), 1) for v, l, r in kept},
            "categories": cats,
        }
        # which table a check used (Spacing QA records it in each report)
        digest = hashlib.sha256(json.dumps([out["left"], out["right"], out["pairs"], cats], sort_keys=True).encode()).hexdigest()
        out = {"id": f"{datetime.date.today().isoformat()}-{digest[:8]}", **out}
        json.dump(out, open(a.out, "w"), indent=1)
        print("wrote", a.out)


if __name__ == "__main__":
    main()
