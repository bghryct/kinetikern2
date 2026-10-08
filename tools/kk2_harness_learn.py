#!/usr/bin/env python3
"""Learns the designer harness: where Kinetikern2 consistently spaces
differently from designers, as corrections the plugin can apply after a solve.

The data are Spacing QA reports: for each of the 66 x 66 scored core pairs,
the designer's gap minus Kinetikern2's at the font's best-fit Looseness, with
the font's overall offset removed (units per 1000 em; + = the designer is
looser). Observations are the reference fonts at Regular (the library scan)
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
are kept where they are large and of a consistent sign. Half the families are
held out to check how much closer to the designers the model gets.

usage: kk2_harness_learn.py --reports SpacingQA/data/reports --weights DIR
                            --families families.csv --out kk2_harness.json
"""
import argparse
import csv
import glob
import json
import math
import os
import re
import sys
from collections import defaultdict

import numpy as np

STEM_REF = 85.0
PUNCT = ["period", "comma", "colon", "semicolon", "exclam", "question", "hyphen", "quotesingle", "quotedbl",
         "parenleft", "parenright", "slash", "ampersand", "quoteright"]


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
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    human = {}
    for row in csv.reader(open(a.families, encoding="utf-8")):
        if len(row) == 4 and row[2] == "/Quality/Spacing" and not row[1]:
            human[row[0]] = float(row[3])

    names = None
    obs = []  # (family, residual matrix, L, stem, label)
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
        if f.get("category") not in ("Sans Serif", "Serif") or f.get("primary_script") not in (None, "", "Latn"):
            return
        if (human.get(f["family"]) or 0) < a.min_quality or abs(s["best_looseness"]) >= 6 - 1e-6:
            return
        pg = d["pair_glyphs"]
        g = [d["glyphs"][i]["name"] for i in pg]
        if names is None:
            names = g
        elif g != names:
            return
        stem = stem_of(r)
        if not stem:
            return
        m = len(pg)
        res = np.array([np.nan if v is None else v / 10.0 for v in d["residuals"]], dtype=float).reshape(m, m)
        obs.append((f["family"], res, float(s["best_looseness"]), stem, label))

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

    def learn(sel):
        X = features(Ls[sel], w_stem[sel])
        right = np.array([huber(X, rows[sel, i], weight[sel]) for i in range(n)])
        left = np.array([huber(X, cols[sel, j], weight[sel]) for j in range(n)])
        Em = np.nanmedian(Es[sel], axis=0)
        same = np.nanmean(np.sign(Es[sel]) == np.sign(Em)[None], axis=0)
        keep = (np.abs(Em) >= 8.0) & (same >= 0.7)
        return right, left, np.where(keep, Em, 0.0)

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

    def add(key, before, after):
        ok = np.isfinite(before)
        t = tot[key]
        t[0] += float(np.abs(before[ok]).sum())
        t[1] += float(np.abs(after[ok]).sum())
        t[2] += int(ok.sum())

    for k in (0, 1):
        right, left, pairs = learn(fold != k)
        for o, i in zip(obs, np.where(fold == k)[0]):
            X = o[1]
            C = predict(right, left, pairs, o[2], w_stem[i])
            Y = X - C
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

    right, left, pairs = learn(np.ones(len(obs), bool))
    print("\nSide corrections at Regular (stem 85), tight / standard / loose (L = -0.5 / 0 / +0.5), and at a light weight (stem 30):")
    show = ["parenleft", "parenright", "slash", "question", "exclam", "ampersand", "quotedbl", "quoteright", "period",
            "comma", "hyphen", "E", "F", "L", "T", "A", "V", "W", "Y", "K", "r", "f", "t"]
    for nm in show:
        i = names.index(nm)
        line = []
        for side, B in (("left", left), ("right", right)):
            vals = [B[i] @ np.array([1, L, 0, 0]) for L in (-0.5, 0, 0.5)]
            light = B[i] @ np.array([1, 0, math.log(30 / STEM_REF), 0])
            line.append(f"{side} {vals[0]:+5.1f} {vals[1]:+5.1f} {vals[2]:+5.1f} | light {light:+5.1f}")
        print(f"  {nm:11} " + "    ".join(line))
    kept = [(pairs[i, j], names[i], names[j]) for i in range(n) for j in range(n) if pairs[i, j] != 0]
    kept.sort(key=lambda t: -abs(t[0]))
    print(f"\n{len(kept)} pair corrections, the largest:", ", ".join(f"{l} {r} {v:+.0f}" for v, l, r in kept[:16]))

    if a.out:
        out = {
            "format": "kinetikern2-harness/1",
            "about": "Where Kinetikern2 spaces differently from designers, learned from Spacing QA reports of text "
                     "fonts on Google Fonts rated well spaced by its reviewers. Units per 1000 em; + = more space "
                     "than the model. A side's correction is a + b*L + c*w + d*L*w, L the Looseness, "
                     "w = ln(stem / %g), stem = the ink width of I (else l) at half its height, per 1000 em." % STEM_REF,
            "stem_ref": STEM_REF,
            "looseness_range": [round(float(np.percentile(Ls, 2)), 2), round(float(np.percentile(Ls, 98)), 2)],
            "stem_range": [round(float(np.percentile(stems, 2)), 1), round(float(np.percentile(stems, 98)), 1)],
            "reference": {"observations": len(obs), "families": len(fams), "min_quality": a.min_quality},
            "glyphs": names,
            "left": {names[j]: [round(float(x), 3) for x in left[j]] for j in range(n)},
            "right": {names[i]: [round(float(x), 3) for x in right[i]] for i in range(n)},
            "pairs": {f"{l} {r}": round(float(v), 1) for v, l, r in kept},
        }
        json.dump(out, open(a.out, "w"), indent=1)
        print("wrote", a.out)


if __name__ == "__main__":
    main()
