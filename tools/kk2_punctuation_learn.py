#!/usr/bin/env python3
"""Learns punctuation by convention (the plugins' Punctuation switch, Spacing QA's comparison): for each
style (text, display and handwriting faces), punctuation mark and side, a shift

    c_style + m · s/50 + r · ρ/50   (units per 1000 em)

where s is the side's own sidebearing as Kinetikern2 sets it (Pass 1) and ρ the font's letter rhythm (the
median Pass-1 sidebearing of its lowercase): a blend of the model's reading of the mark and the font's own
rhythm, in the proportion designers follow for that mark. And per mark and side, the share of Pass 2's
kerning that designers keep: they tuck a period well under a T, V or Y but hardly kern a hyphen or a quote.
A pair takes the product of its two facing sides' shares (letters keep all of theirs), so two marks side
by side are barely kerned; marks and symbols outside the table keep KERN_SCALE on each side.

Data: one JSON line per font (Spacing QA's `punctuation-data`, run over the fonts of the Google Fonts
library and, as a check never used for fitting, other fonts): the engine's Pass-1 sidebearings, and for
every ordered pair of the GF Latin Kernel's scored glyphs the designer's gap, the model's sidebearing gap,
the interaction contrast, and its kerning under the variants "v1;default;default,p=0.3/inf/0/1". A pair's
kerning at any other share follows from its contrast through Pass 2's response (`Knobs::shape` at the
defaults), and from the two measured values where a floor decides it.

The two parts are fitted in turn (three rounds). Each font's residuals are split by median polish; a
punctuation side's shift is what its pairs with letters leave after the letters' terms, fitted on the
families Google Fonts' reviewers rate well spaced (/Quality/Spacing >= 70) — style intercepts and shared
slopes per mark and side, robustly and with ridge on the slopes. A side's share is the one, on a grid of
0.05, with the least mean absolute difference from what designers do with its pairs with letters. Both
are cross-validated by family, and checked on the other fonts.

The rest of the punctuation (--more-data: `punctuation-data --all-punctuation`, the kernel's glyphs and
every other mark Latin text uses — guillemets, low quotes, ¡ ¿, § ¶ † ‡, ‰, primes…) is learned the same
way against the letters of that run, and added to the table without changing the kernel's 36 marks. A
mark too few families have takes the conventions of its twin (ALIASES: a non-breaking hyphen the
hyphen's), and the table lists every mark's characters (`marks`) for the plugins to find them by.

Superscripts, subscripts and ordinals (--small-data: `punctuation-data --small-forms`) are learned the same
way and added to a finished table (--base) without changing anything in it. Their sides only: the engine kerns
figures and letters fully, as the model does, so they keep no share of their own. A design whose superscripts (or
subscripts) share one width is not learned on, and the plugins keep such a set as it is.

usage: kk2_punctuation_learn.py --data 'DIR/*.jsonl' --reports SpacingQA/data/reports
                                --families families.csv --out kk2_punctuation.json
                                [--more-data 'DIR2/*.jsonl' | --small-data 'DIR3/*.jsonl' --base kk2_punctuation.json]
"""
import glob, json, numpy as np, datetime, hashlib
import argparse, csv, os
ap = argparse.ArgumentParser()
ap.add_argument("--data", required=True, help="glob of the measurement JSON lines")
ap.add_argument("--reports", required=True, help="Spacing QA's reports (a family's category)")
ap.add_argument("--families", required=True, help="google/fonts tags CSV (/Quality/Spacing)")
ap.add_argument("--min-quality", type=float, default=70)
ap.add_argument("--out", required=True)
ap.add_argument("--more-data", default=None, help="glob of punctuation-data --all-punctuation's JSON lines")
ap.add_argument("--small-data", default=None, help="glob of punctuation-data --small-forms' JSON lines")
ap.add_argument("--feature-data", default=None, help="glob of punctuation-data --feature-forms' JSON lines")
ap.add_argument("--base", default=None, help="the finished table --small-data or --feature-data adds to")
args = ap.parse_args()
if (args.small_data or args.feature_data) and (args.more_data or not args.base or (args.small_data and args.feature_data)):
    ap.error("--small-data or --feature-data takes --base, and no --more-data")
human = {}
for row in csv.reader(open(args.families, encoding="utf-8")):
    if len(row) == 4 and row[2] == "/Quality/Spacing" and not row[1]:
        human[row[0]] = float(row[3])
meta = {}
for p in glob.glob(os.path.join(args.reports, "*.json")):
    r = json.load(open(p))
    f = r.get("font") or {}
    meta[os.path.basename(p)[:-5]] = {"category": f.get("category") or "", "quality": human.get(f.get("family")),
                                      "tabular": bool((r.get("settings") or {}).get("tabular"))}
PUNCT = ["period", "comma", "colon", "semicolon", "exclam", "question", "hyphen", "quotesingle", "quotedbl",
         "parenleft", "parenright", "slash", "ampersand", "quoteright", "percent", "asterisk", "at", "bracketleft", "backslash",
         "bracketright", "grave", "braceleft", "bar", "braceright", "copyright", "registered", "degree", "periodcentered", "endash",
         "emdash", "quoteleft", "quotedblleft", "quotedblright", "bullet", "ellipsis", "trademark"]
LET = [chr(c) for c in range(65, 91)] + [chr(c) for c in range(97, 123)]
NAMES = LET + PUNCT
AT = {n: i for i, n in enumerate(NAMES)}
N, NP = len(NAMES), len(PUNCT)
STYLE_OF = {"Sans Serif": "text", "Serif": "text", "Display": "display", "Handwriting": "handwriting"}
STYLES = ["text", "display", "handwriting"]
SI = {s: i for i, s in enumerate(STYLES)}
KERN_SCALE = 0.3    # the share of every mark side the data say nothing about (variant 2's)
GRID = np.round(np.arange(0, 1.21, 0.05), 2)
ROUNDS = 3
# Pass 2's response at the defaults (per 1000 em): coupling × COUPLING_CALIBRATION, dead zone min_kern,
# soft clip at max_negative_kern / max_positive_kern
COUPLING, DEAD, NEG, POS = 0.8, 15.0, 100.0, 60.0
# the pairs every kerning lesson starts with: a period or comma under an overhang
OVERHANG = [AT[c] for c in "TVWYPFrvwy"]
LOW_MARKS = [AT["period"], AT["comma"]]


def shape(raw, share):
    """Pass 2's value before the floors (`Knobs::shape`, no tail)."""
    d = share * COUPLING * raw
    s = np.sign(d) * np.maximum(np.abs(d) - DEAD, 0)
    return np.where(s < 0, NEG * (np.exp(s / NEG) - 1), POS * (1 - np.exp(-s / POS)))


def polish(M, iters=8):
    R = M.copy(); row = np.zeros(M.shape[0]); col = np.zeros(M.shape[1])
    with np.errstate(all="ignore"):
        for _ in range(iters):
            r = np.nanmedian(R, axis=1); r = np.where(np.isfinite(r), r, 0); row += r; R -= r[:, None]
            c = np.nanmedian(R, axis=0); c = np.where(np.isfinite(c), c, 0); col += c; R -= c[None, :]
    return row, col


fonts = []
for f in sorted(glob.glob(args.data)):
    for line in open(f):
        try: r = json.loads(line)
        except Exception: continue
        m = meta.get(r["slug"])
        names = r["names"]; idx = np.array([AT.get(n, -1) for n in names])
        P = np.array(r["pairs"], float)
        a, b = idx[P[:, 0].astype(int)], idx[P[:, 1].astype(int)]
        ok = (a >= 0) & (b >= 0)
        P, a, b = P[ok], a[ok], b[ok]
        raw, vdef, vk = P[:, 4], P[:, 6], P[:, 7]
        # a floor decided the value where it differs from the response at either share
        floor = np.where(vk > shape(raw, KERN_SCALE) + 0.6, vk, np.where(vdef > shape(raw, 1.0) + 0.6, vdef, -np.inf))
        sb = np.array(r["sb"], float); groups = np.array(r["groups"], int)
        lc = np.concatenate([sb[groups == 2, 0], sb[groups == 2, 1]]); lc = lc[np.isfinite(lc)]
        rho = np.median(lc) if len(lc) >= 6 else np.nan
        Bp = np.full((NP, 2), np.nan)
        for j, p in enumerate(PUNCT):
            k = [i for i, n in enumerate(names) if n == p]
            if k: Bp[j] = sb[k[0]]
        cat = m["category"] if m else "System"
        q = (m["quality"] if m and m["quality"] is not None else np.nan)
        fonts.append(dict(slug=r["slug"], cat=cat, style=STYLE_OF.get(cat, "text"), q=q, a=a, b=b, dg=P[:, 2], mg=P[:, 3],
                          raw=raw, floor=floor, v1=P[:, 5], vdef=vdef, rho=rho, Bp=Bp))
F = len(fonts)
print(F, "fonts")
style = np.array([fo["style"] for fo in fonts]); cat = np.array([fo["cat"] for fo in fonts])
q = np.array([fo["q"] for fo in fonts], float); rho = np.array([fo["rho"] for fo in fonts]); Bp = np.stack([fo["Bp"] for fo in fonts])
sysf = cat == "System"
ref = (q >= args.min_quality) & ~sysf


def values(fo, K):
    """The kerning with shares K[mark, side] (None: the defaults; letters keep the defaults either way)."""
    if K is None: return fo["vdef"]
    a, b = fo["a"], fo["b"]
    s = np.ones(len(a)); pa, pb = a >= 52, b >= 52
    s[pb] *= K[b[pb] - 52, 0]
    s[pa] *= K[a[pa] - 52, 1]
    return np.where(pa | pb, np.maximum(shape(fo["raw"], s), fo["floor"]), fo["vdef"])


def shifted(fo, sh):
    dl = np.zeros(N); dr = np.zeros(N); dl[52:] = sh[:, 0]; dr[52:] = sh[:, 1]
    return dr[fo["a"]] + dl[fo["b"]]


def side_targets(K, idx):
    """The shift each punctuation side would need in each font (median polish, letters as partners)."""
    SIDE = np.full((F, NP, 2), np.nan)
    for i in idx:
        fo = fonts[i]
        M = np.full((N, N), np.nan); M[fo["a"], fo["b"]] = fo["dg"] - (fo["mg"] + values(fo, K)); M = M - np.nanmean(M)
        row, col = polish(M)
        for j, p in enumerate(PUNCT):
            pi = AT[p]
            l = M[:52, pi] - row[:52]; rr = M[pi, :52] - col[:52]
            if np.isfinite(l).sum() >= 10: SIDE[i, j, 0] = np.nanmedian(l)
            if np.isfinite(rr).sum() >= 10: SIDE[i, j, 1] = np.nanmedian(rr)
    return SIDE


def fit_sides(SIDE, idx, lam=2.0):
    """Coefficients per mark and side: style intercepts, shared slopes on s/50 and ρ/50 (ridge on the slopes)."""
    coef = {}
    for j in range(NP):
        for sd in (0, 1):
            X = np.column_stack([(style[idx] == s).astype(float) for s in STYLES] + [Bp[idx, j, sd] / 50.0, rho[idx] / 50.0])
            y = SIDE[idx, j, sd]
            ok = np.isfinite(y) & np.all(np.isfinite(X), 1)
            if ok.sum() < 30: continue
            Xo, yo = X[ok], y[ok]
            # Huber-ish: reweighting rounds against outliers
            w = np.ones(len(yo))
            R_ = np.diag([0, 0, 0, lam, lam])
            for _ in range(3):
                A = Xo.T @ (Xo * w[:, None]) + R_
                beta = np.linalg.solve(A, Xo.T @ (w * yo))
                res = yo - Xo @ beta
                s = 1.4826 * np.median(np.abs(res)) + 1e-6
                u = np.abs(res) / (1.345 * s)
                w = np.where(u <= 1, 1.0, 1.0 / u)
            coef[(j, sd)] = beta
    return coef


def predict(coef, idx):
    out = np.zeros((F, NP, 2))
    for (j, sd), beta in coef.items():
        X = np.column_stack([(style[idx] == s).astype(float) for s in STYLES] + [Bp[idx, j, sd] / 50.0, rho[idx] / 50.0])
        out[idx, j, sd] = np.nan_to_num(X @ beta)
    return np.clip(out, -80, 80)


def fit_shares(shifts, idx, K):
    """Per mark and side, the grid share with the least mean |what designers do − the kerning| over its pairs
    with letters, the sides shifted and each font's offset removed."""
    acc = np.zeros((NP, 2, len(GRID))); cnt = np.zeros((NP, 2))
    for i in idx:
        fo = fonts[i]
        sh = shifted(fo, shifts[i])
        off = np.nanmean(fo["dg"] - (fo["mg"] + sh + values(fo, K)))
        t = fo["dg"] - fo["mg"] - sh - off
        a, b = fo["a"], fo["b"]
        for side, sel, mk in ((0, (a < 52) & (b >= 52), b), (1, (a >= 52) & (b < 52), a)):
            k = np.where(sel)[0]
            if not len(k): continue
            E = np.abs(t[k, None] - np.maximum(shape(fo["raw"][k, None], GRID[None, :]), fo["floor"][k, None]))
            j = mk[k] - 52
            np.add.at(acc[:, side], j, E)
            np.add.at(cnt[:, side], j, 1)
    K = np.full((NP, 2), KERN_SCALE)
    enough = cnt >= 500
    K[enough] = GRID[np.argmin(acc, axis=2)][enough]
    return K


def learn(sel):
    idx = np.where(sel)[0]
    K = np.full((NP, 2), KERN_SCALE); coef = {}
    for _ in range(ROUNDS):
        coef = fit_sides(side_targets(K, idx), idx)
        K = fit_shares(predict(coef, idx), idx, K)
    return coef, K


def errors(i, K, shifts):
    """One font's |offset-free difference| per pair, and which pairs have punctuation / are overhang pairs."""
    fo = fonts[i]
    d = fo["dg"] - (fo["mg"] + values(fo, K))
    if shifts is not None: d = d - shifted(fo, shifts[i])
    e = np.abs(d - np.nanmean(d))
    a, b = fo["a"], fo["b"]
    return e, (a >= 52) | (b >= 52), np.isin(a, OVERHANG) & np.isin(b, LOW_MARKS)


def score(idx, K, shifts, v1=False):
    o_all, o_p, o_l, o_o = [], [], [], []
    for i in idx:
        if v1:
            fo = fonts[i]; d = fo["dg"] - (fo["mg"] + fo["v1"]); e = np.abs(d - np.nanmean(d))
            isp = (fo["a"] >= 52) | (fo["b"] >= 52); ov = np.isin(fo["a"], OVERHANG) & np.isin(fo["b"], LOW_MARKS)
        else:
            e, isp, ov = errors(i, K[i] if K is not None and K.ndim == 3 else K, shifts)
        o_all.append(np.nanmean(e)); o_p.append(np.nanmean(e[isp])); o_l.append(np.nanmean(e[~isp]))
        if ov.any(): o_o.append(np.nanmean(e[ov]))
    return [float(np.nanmedian(x)) if len(x) else float("nan") for x in (o_all, o_p, o_l, o_o)]


rng = np.random.default_rng(3)
fold = rng.integers(0, 5, F)
cv_shift = np.zeros((F, NP, 2)); cv_K = np.zeros((F, NP, 2))
for k in range(5):
    coef, K = learn(ref & (fold != k))
    sel = np.where((fold == k) & ~sysf)[0]
    cv_shift[sel] = predict(coef, sel)[sel]; cv_K[sel] = K
    print("fold", k, "period", K[0], "comma", K[1], flush=True)
full, Kf = learn(ref)
sysi = np.where(sysf)[0]
cv_shift[sysi] = predict(full, sysi)[sysi]; cv_K[sysi] = Kf
lines = []
for c in ["Sans Serif", "Serif", "Display", "Handwriting", "System"]:
    idx = np.where(cat == c)[0]
    v1 = score(idx, None, None, v1=True); dflt = score(idx, None, None); conv = score(idx, cv_K, cv_shift)
    line = (f"{c:12s} n={len(idx):4d}  shape error (all / punctuation pairs / letter pairs / a period or comma under an "
            f"overhang): v1 {v1[0]:.2f}/{v1[1]:.2f}/{v1[2]:.2f}/{v1[3]:.1f}  → new defaults {dflt[0]:.2f}/{dflt[1]:.2f}/{dflt[2]:.2f}/{dflt[3]:.1f}"
            f"  → + punctuation by convention (held out) {conv[0]:.2f}/{conv[1]:.2f}/{conv[2]:.2f}/{conv[3]:.1f}")
    print(line, flush=True); lines.append(line)
table = {"text": {}, "display": {}, "handwriting": {}}
for (j, sd), beta in full.items():
    for si, s in enumerate(STYLES):
        table[s].setdefault(PUNCT[j], [[None] * 3, [None] * 3])[sd] = [round(float(beta[si]), 2), round(float(beta[3]), 3), round(float(beta[4]), 3)]
kern = {p: [round(float(Kf[j, 0]), 2), round(float(Kf[j, 1]), 2)] for j, p in enumerate(PUNCT)}
today = datetime.date.today().isoformat()
kernel_id = today + "-" + hashlib.sha1(json.dumps([table, kern], sort_keys=True).encode()).hexdigest()[:8]

# ---------------------------------------------------------------- the rest of the punctuation
# the kernel's marks (glyphset.rs) and the rest (glyphset::PUNCTUATION_MORE): their characters
KERNEL_CHARS = {"period": ".", "comma": ",", "colon": ":", "semicolon": ";", "exclam": "!", "question": "?",
                "hyphen": "-", "quotesingle": "'", "quotedbl": '"', "parenleft": "(", "parenright": ")", "slash": "/",
                "ampersand": "&", "quoteright": "’", "percent": "%", "asterisk": "*", "at": "@",
                "bracketleft": "[", "backslash": "\\", "bracketright": "]", "grave": "`", "braceleft": "{",
                "bar": "|", "braceright": "}", "copyright": "©", "registered": "®", "degree": "°",
                "periodcentered": "·", "endash": "–", "emdash": "—", "quoteleft": "‘",
                "quotedblleft": "“", "quotedblright": "”", "bullet": "•", "ellipsis": "…",
                "trademark": "™"}
MORE = [("exclamdown", "¡"), ("questiondown", "¿"), ("guillemetleft", "«"), ("guillemetright", "»"),
        ("guilsinglleft", "‹"), ("guilsinglright", "›"), ("quotesinglbase", "‚"),
        ("quotedblbase", "„"), ("quotereversed", "‛"), ("quotedblreversed", "‟"), ("section", "§"),
        ("paragraph", "¶"), ("dagger", "†"), ("daggerdbl", "‡"), ("perthousand", "‰"),
        ("pertenthousand", "‱"), ("minute", "′"), ("second", "″"), ("tripleprime", "‴"),
        ("brokenbar", "¦"), ("hyphentwo", "‐"), ("nonbreakinghyphen", "‑"), ("figuredash", "‒"),
        ("horizontalbar", "―"), ("onedotenleader", "․"), ("twodotenleader", "‥"),
        ("hyphenationpoint", "‧"), ("triangularbullet", "‣"), ("exclamdbl", "‼"),
        ("interrobang", "‽"), ("overline", "‾"), ("referencemark", "※"), ("asterism", "⁂"),
        ("fraction", "⁄"), ("apostrophemod", "ʼ"), ("numero", "№"),
        # the kernel's symbols that sit with figures (not scored there): conventions of their own as well
        ("numbersign", "#"), ("dollar", "$"), ("plus", "+"), ("less", "<"), ("equal", "="), ("greater", ">"),
        ("asciicircum", "^"), ("asciitilde", "~"), ("cent", "¢"), ("sterling", "£"), ("yen", "¥"),
        ("multiply", "×"), ("divide", "÷"), ("euro", "€"), ("minus", "−")]
# the sides that face figures in text (5 %, $5, 5′ 10″, ½, № 5, 2 + 2): learned against the figures; every other
# side against the letters
FIGURE_SIDES = {n: (True, True) for n in ("numbersign", "dollar", "plus", "less", "equal", "greater", "asciicircum",
                                         "asciitilde", "cent", "sterling", "yen", "multiply", "divide", "euro", "minus",
                                         "minute", "second", "tripleprime", "fraction")}
FIGURE_SIDES.update({"perthousand": (True, False), "pertenthousand": (True, False), "numero": (False, True)})
FIG = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
# figures by convention: proportional figures (a tabular design keeps its widths), each side learned against the
# figures, with no kerning share (Pass 2 kerns figures as the model does)
MORE += [(n, chr(0x30 + k)) for k, n in enumerate(FIG)]
FIGURE_SIDES.update({n: (True, True) for n in FIG})
# a mark too few families have takes the conventions of its twin: the same role and nearly the same shape
ALIASES = {"hyphentwo": "hyphen", "nonbreakinghyphen": "hyphen", "figuredash": "endash", "horizontalbar": "emdash",
           "apostrophemod": "quoteright", "onedotenleader": "period", "twodotenleader": "ellipsis",
           "hyphenationpoint": "periodcentered", "triangularbullet": "bullet", "exclamdbl": "exclam",
           "interrobang": "question", "pertenthousand": "perthousand", "quotereversed": "quoteleft",
           "quotedblreversed": "quotedblleft", "tripleprime": "second", "asterism": "asterisk",
           "referencemark": "asterisk"}
MIN_FAMILIES = 30      # reference families with the mark, to learn it on its own
# superscripts, subscripts and ordinals (glyphset::SMALL_FORMS): a figure's small forms against the letters they
# follow in text (a footnote¹, H₂O), an ordinal's left side against the figures (1º, 2ª); an inferior with too few
# families takes its superior's conventions (the same shape, lower)
SUP = ["zerosuperior", "onesuperior", "twosuperior", "threesuperior", "foursuperior", "fivesuperior", "sixsuperior",
       "sevensuperior", "eightsuperior", "ninesuperior"]
SUB = ["zeroinferior", "oneinferior", "twoinferior", "threeinferior", "fourinferior", "fiveinferior", "sixinferior",
       "seveninferior", "eightinferior", "nineinferior"]
SMALL = (list(zip(SUP, "⁰¹²³⁴⁵⁶⁷⁸⁹")) + list(zip(SUB, "₀₁₂₃₄₅₆₇₈₉")) +
         [("ordfeminine", "ª"), ("ordmasculine", "º")])
if args.small_data:
    MORE = SMALL
    FIGURE_SIDES = {"ordfeminine": (True, False), "ordmasculine": (True, False)}
    ALIASES = dict(zip(SUB, SUP))
    args.more_data = args.small_data
# old-style figures, small caps and precomposed fractions (--feature-data: what a font reaches through onum, smcp,
# and by character): an old-style figure against the letters around it (in 1987, 10am), a small cap against the
# capitals (Caps and Small Caps), a fraction's left side against the figure before it (1½); sides only, like the
# small forms; an old-style set of one width keeps its widths. The fraction slash between a numerator and a
# denominator (HarfBuzz sets a fraction typed with U+2044 so) is learned below, as "fraction.frac".
OSF = [n + ".osf" for n in FIG]
SCAPS = [chr(c) + ".sc" for c in range(97, 123)]
VULGAR = [("onehalf", "½"), ("onequarter", "¼"), ("threequarters", "¾"), ("onethird", "⅓"), ("twothirds", "⅔"),
          ("oneeighth", "⅛"), ("threeeighths", "⅜"), ("fiveeighths", "⅝"), ("seveneighths", "⅞")]
FEATURE = [(n, None) for n in OSF + SCAPS] + VULGAR
NOSHARE = set(n for n, _c in SMALL) | set(n for n, _c in FEATURE)
if args.feature_data:
    MORE = FEATURE
    FIGURE_SIDES = {n: (True, False) for n, _c in VULGAR}
    ALIASES = {"twothirds": "onethird", "threeeighths": "oneeighth", "fiveeighths": "oneeighth",
               "seveneighths": "oneeighth", "threequarters": "onequarter"}
    args.more_data = args.feature_data
    SMALL = FEATURE  # what --base gains
FRAC_CLIP = 250.0  # designers tuck a numerator and a denominator well under the slash


def learn_fraction_slash(mf, ref2, sys2, fold2, style2, rho2, base):
    """The fraction slash between a numerator and a denominator ("fraction.frac"): each side's shift
    (c_style + m·s/50 + r·ρ/50, s its own side as the model sets it) and its kerning share there, learned like a
    mark's against the numerators and denominators (their own sides as the model sets them). Held out by family,
    against the model alone and against the slash's conventions learned beside full-size figures ("fraction")."""
    idx_all = [i for i, fo in enumerate(mf) if fo.get("frac") is not None and fo["frac"]["left"]["dg"].size >= 5
               and fo["frac"]["right"]["dg"].size >= 5 and np.all(np.isfinite(fo["frac"]["s"]))]
    if not idx_all:
        return []
    S_ = np.full((len(mf), 2), np.nan)
    for i in idx_all:
        S_[i] = mf[i]["frac"]["s"]

    def side_data(i, sd):
        return mf[i]["frac"]["left" if sd == 0 else "right"]

    def value(d, share):
        return np.maximum(shape(d["raw"], share), d["floor"])

    def fit(idx, K):
        coef = {}
        for sd in (0, 1):
            X, y = [], []
            for i in idx:
                d = side_data(i, sd)
                res = d["dg"] - (d["mg"] + value(d, K[sd])) - mf[i]["off"]
                if np.isfinite(res).sum() < 5 or not np.isfinite(rho2[i]): continue
                X.append([float(style2[i] == st_) for st_ in STYLES] + [S_[i, sd] / 50.0, rho2[i] / 50.0])
                y.append(np.nanmedian(res))
            if len(y) < MIN_FAMILIES: continue
            X, y = np.array(X), np.array(y)
            present = [(X[:, k] > 0).sum() >= 8 for k in range(3)]
            for k in range(1, 3):
                if not present[k]:
                    X[:, 0] += X[:, k]; X[:, k] = 0.0
            R_ = np.diag([0 if present[0] else 2.0, 0 if present[1] else 1e6, 0 if present[2] else 1e6, 2.0, 2.0])
            w = np.ones(len(y))
            for _ in range(3):
                beta = np.linalg.solve(X.T @ (X * w[:, None]) + R_, X.T @ (w * y))
                res = y - X @ beta
                sc = 1.4826 * np.median(np.abs(res)) + 1e-6
                u = np.abs(res) / (1.345 * sc); w = np.where(u <= 1, 1.0, 1.0 / u)
            for k in range(1, 3):
                if not present[k]: beta[k] = beta[0]
            coef[sd] = beta
        return coef

    def predict(coef, i, sd):
        beta = coef.get(sd)
        if beta is None or not np.isfinite(rho2[i]): return 0.0
        x = np.array([float(style2[i] == st_) for st_ in STYLES] + [S_[i, sd] / 50.0, rho2[i] / 50.0])
        return float(np.clip(x @ beta, -FRAC_CLIP, FRAC_CLIP))

    def shares(coef, idx):
        K = np.full(2, KERN_SCALE)
        for sd in (0, 1):
            acc = np.zeros(len(GRID)); cnt = 0
            for i in idx:
                d = side_data(i, sd)
                t = d["dg"] - d["mg"] - predict(coef, i, sd) - mf[i]["off"]
                acc += np.nansum(np.abs(t[:, None] - np.maximum(shape(d["raw"][:, None], GRID[None, :]), d["floor"][:, None])), 0)
                cnt += len(t)
            if cnt >= 300: K[sd] = GRID[np.argmin(acc)]
        return K

    def learn(idx):
        K = np.full(2, KERN_SCALE); coef = {}
        for _ in range(ROUNDS):
            coef = fit(idx, K); K = shares(coef, idx)
        return coef, K

    # the numerators and denominators themselves: what each side leaves once the slash has its conventions (the
    # slash takes what a numerator's right side and its own left side share, as learned above; a numerator's left
    # side is what its pairs with the numerator before it leave, and so on)
    KINDS = (("numr", "nsb"), ("dnom", "dsb"))

    def fig_targets(i, coef, K):
        fo = mf[i]["frac"]; off = mf[i]["off"]
        T = np.full((2, 10, 2), np.nan)
        L_, R_ = fo["left"], fo["right"]
        res = L_["dg"] - L_["mg"] - off - value(L_, K[0]) - predict(coef, i, 0)
        for a in range(10):
            w = L_["a"] == a
            if w.any(): T[0, a, 1] = np.nanmedian(res[w])
        res = R_["dg"] - R_["mg"] - off - value(R_, K[1]) - predict(coef, i, 1)
        for b in range(10):
            w = R_["b"] == b
            if w.any(): T[1, b, 0] = np.nanmedian(res[w])
        for kk, key, other in ((0, "nn", 0), (1, "dd", 1)):
            d = fo[key]
            if not d["dg"].size: continue
            res = d["dg"] - d["mg"] - off - d["vdef"]
            if kk == 0:   # a numerator's left side: after numerator a, less a's right side
                for b in range(10):
                    w = (d["b"] == b) & np.isfinite(T[0, d["a"], 1])
                    if w.sum() >= 3: T[0, b, 0] = np.nanmedian(res[w] - T[0, d["a"][w], 1])
            else:         # a denominator's right side: before denominator b, less b's left side
                for a in range(10):
                    w = (d["a"] == a) & np.isfinite(T[1, d["b"], 0])
                    if w.sum() >= 3: T[1, a, 1] = np.nanmedian(res[w] - T[1, d["b"][w], 0])
        return T

    def fig_fit(idx, coef, K):
        out_ = {}
        Ts = {i: fig_targets(i, coef, K) for i in idx}
        for kk, (kind, sbkey) in enumerate(KINDS):
            for dgt in range(10):
                for sd in (0, 1):
                    X, y = [], []
                    for i in idx:
                        t = Ts[i][kk, dgt, sd]; s_ = mf[i]["frac"][sbkey][dgt, sd]
                        if not (np.isfinite(t) and np.isfinite(s_) and np.isfinite(rho2[i])): continue
                        X.append([float(style2[i] == st_) for st_ in STYLES] + [s_ / 50.0, rho2[i] / 50.0]); y.append(t)
                    if len(y) < MIN_FAMILIES: continue
                    X, y = np.array(X), np.array(y)
                    present = [(X[:, k_] > 0).sum() >= 8 for k_ in range(3)]
                    for k_ in range(1, 3):
                        if not present[k_]:
                            X[:, 0] += X[:, k_]; X[:, k_] = 0.0
                    R_ = np.diag([0 if present[0] else 2.0, 0 if present[1] else 1e6, 0 if present[2] else 1e6, 2.0, 2.0])
                    w = np.ones(len(y))
                    for _ in range(3):
                        beta = np.linalg.solve(X.T @ (X * w[:, None]) + R_, X.T @ (w * y))
                        r_ = y - X @ beta
                        sc = 1.4826 * np.median(np.abs(r_)) + 1e-6
                        u = np.abs(r_) / (1.345 * sc); w = np.where(u <= 1, 1.0, 1.0 / u)
                    for k_ in range(1, 3):
                        if not present[k_]: beta[k_] = beta[0]
                    out_[(kk, dgt, sd)] = beta
        return out_

    def fig_shift(figc, i, kk, dgt, sd):
        beta = figc.get((kk, dgt, sd)); s_ = mf[i]["frac"][KINDS[kk][1]][dgt, sd]
        if beta is None or not (np.isfinite(s_) and np.isfinite(rho2[i])): return 0.0
        x = np.array([float(style2[i] == st_) for st_ in STYLES] + [s_ / 50.0, rho2[i] / 50.0])
        return float(np.clip(x @ beta, -80, 80))

    # what the table does today in such a font: the slash's conventions learned beside full-size figures
    def today(i, sd):
        c = base["styles"].get(mf[i]["style"], base["styles"]["text"]).get("fraction")
        if not c or c[sd][0] is None or not np.isfinite(rho2[i]): return 0.0
        k_, m_, r_ = c[sd]
        return float(np.clip(k_ + m_ * S_[i, sd] / 50.0 + r_ * rho2[i] / 50.0, -80, 80))
    k_today = base["kern"].get("fraction", [base.get("kern_scale", KERN_SCALE)] * 2)
    ref_idx = [i for i in idx_all if ref2[i]]
    cv, cvf = {}, {}
    for k in range(5):
        train = [i for i in ref_idx if fold2[i] != k]
        coef, K = learn(train)
        figc = fig_fit(train, coef, K)
        for i in idx_all:
            if fold2[i] == k and not sys2[i]: cv[i], cvf[i] = (coef, K), figc
    full, Kfull = learn(ref_idx)
    figfull = fig_fit(ref_idx, full, Kfull)
    for i in idx_all:
        if sys2[i]: cv[i], cvf[i] = (full, Kfull), figfull
    out = []
    for label, sel in (("reference families", lambda i: ref2[i]), ("other families", lambda i: not ref2[i] and not sys2[i]),
                       ("macOS fonts", lambda i: sys2[i])):
        e = np.zeros(3); n_ = 0
        for i in idx_all:
            if not sel(i): continue
            coef, K = cv[i]
            for sd in (0, 1):
                d = side_data(i, sd)
                base_ = d["dg"] - d["mg"] - mf[i]["off"]
                e[0] += np.nansum(np.abs(base_ - d["vdef"]))
                e[1] += np.nansum(np.abs(base_ - today(i, sd) - value(d, k_today[sd])))
                e[2] += np.nansum(np.abs(base_ - predict(coef, i, sd) - value(d, K[sd])))
                n_ += np.isfinite(base_).sum()
        if n_:
            line = (f"fraction slash between a numerator and a denominator, held out, {label} "
                    f"({sum(1 for i in idx_all if sel(i))} fonts): model {e[0] / n_:.1f} / today's slash {e[1] / n_:.1f} / "
                    f"its own {e[2] / n_:.1f}")
            print(line); out.append(line)
        # every pair of a fraction (numerator–numerator, numerator–slash, slash–denominator, denominator–denominator)
        f = np.zeros(3); m_ = 0
        for i in idx_all:
            if not sel(i): continue
            coef, K = cv[i]; figc = cvf[i]; fo = mf[i]["frac"]; off = mf[i]["off"]
            for key in ("left", "right", "nn", "dd"):
                d = fo[key]
                if not d["dg"].size: continue
                b0 = d["dg"] - d["mg"] - off
                if key == "left":
                    k_slash = predict(coef, i, 0) + value(d, K[0])
                    figs_ = np.array([fig_shift(figc, i, 0, a, 1) for a in d["a"]])
                elif key == "right":
                    k_slash = predict(coef, i, 1) + value(d, K[1])
                    figs_ = np.array([fig_shift(figc, i, 1, b, 0) for b in d["b"]])
                else:
                    kk = 0 if key == "nn" else 1
                    k_slash = d["vdef"]
                    figs_ = np.array([fig_shift(figc, i, kk, a, 1) + fig_shift(figc, i, kk, b, 0)
                                      for a, b in zip(d["a"], d["b"])])
                f[0] += np.nansum(np.abs(b0 - d["vdef"]))
                f[1] += np.nansum(np.abs(b0 - k_slash))
                f[2] += np.nansum(np.abs(b0 - k_slash - figs_))
                m_ += np.isfinite(b0).sum()
        if m_:
            line = (f"every pair of a fraction, held out, {label}: model {f[0] / m_:.1f} / the slash's conventions "
                    f"{f[1] / m_:.1f} / and the numerators' and denominators' {f[2] / m_:.1f}")
            print(line); out.append(line)
    if full:
        for si, st_ in enumerate(STYLES):
            table[st_]["fraction.frac"] = [[round(float(full[sd][si]), 2), round(float(full[sd][3]), 3), round(float(full[sd][4]), 3)]
                                           if sd in full else [None] * 3 for sd in (0, 1)]
        kern["fraction.frac"] = [round(float(Kfull[0]), 2), round(float(Kfull[1]), 2)]
        line = "fraction.frac: " + json.dumps({"text": table["text"]["fraction.frac"], "kern": kern["fraction.frac"]})
        print(line); out.append(line)
        for kk, (kind, _sbkey) in enumerate(KINDS):
            for dgt in range(10):
                if not any((kk, dgt, sd) in figfull for sd in (0, 1)): continue
                name = FIG[dgt] + "." + kind
                for si, st_ in enumerate(STYLES):
                    table[st_][name] = [[round(float(figfull[(kk, dgt, sd)][si]), 2), round(float(figfull[(kk, dgt, sd)][3]), 3),
                                         round(float(figfull[(kk, dgt, sd)][4]), 3)] if (kk, dgt, sd) in figfull else [None] * 3
                                        for sd in (0, 1)]
                FRACTION_FIGURES.append(name)
    return out


FRACTION_FIGURES = []  # the numerators and denominators learn_fraction_slash added to the table


lines_more, marks, aliases = [], {n: [ord(c)] for n, c in KERNEL_CHARS.items()}, {}
if args.more_data:
    MN = [n for n, _ in MORE]
    MAT = {n: k for k, n in enumerate(MN)}
    NM = len(MN)
    mf = []
    for f in sorted(glob.glob(args.more_data)):
        for line in open(f):
            try: r = json.loads(line)
            except Exception: continue
            m = meta.get(r["slug"])
            names = r["names"]
            kidx = np.array([AT.get(n, -1) for n in names]); midx = np.array([MAT.get(n, -1) for n in names])
            P = np.array(r["pairs"], float)
            ga, gb = P[:, 0].astype(int), P[:, 1].astype(int)
            ka, kb, ma, mb = kidx[ga], kidx[gb], midx[ga], midx[gb]
            raw, vdef, vk = P[:, 4], P[:, 6], P[:, 7]
            floor = np.where(vk > shape(raw, KERN_SCALE) + 0.6, vk, np.where(vdef > shape(raw, 1.0) + 0.6, vdef, -np.inf))
            # the kernel's own pairs, with its shares (letters fully): the letters' terms and the font's offset
            kk = (ka >= 0) & (kb >= 0)
            a, b = ka[kk], kb[kk]
            sh = np.ones(kk.sum()); pa, pb = a >= 52, b >= 52
            sh[pb] *= Kf[b[pb] - 52, 0]; sh[pa] *= Kf[a[pa] - 52, 1]
            v = np.where(pa | pb, np.maximum(shape(raw[kk], sh), floor[kk]), vdef[kk])
            # the font's offset over the kernel's pairs, as above; then each letter's and figure's terms from a
            # polish of the letters, figures and the kernel's marks (figures at their defaults: their own terms
            # take a tabular design's widths)
            off = np.nanmean(P[kk, 2] - (P[kk, 3] + v))
            fidx = np.array([FIG.index(n) if n in FIG else -1 for n in names])
            ca = np.where(ka >= 0, ka, np.where(fidx[ga] >= 0, N + fidx[ga], -1))
            cb = np.where(kb >= 0, kb, np.where(fidx[gb] >= 0, N + fidx[gb], -1))
            cc = (ca >= 0) & (cb >= 0)
            share = np.ones(len(P))
            pa_, pb_ = (ka >= 52), (kb >= 52)
            share[pb_] *= Kf[kb[pb_] - 52, 0]; share[pa_] *= Kf[ka[pa_] - 52, 1]
            vc = np.where(pa_ | pb_, np.maximum(shape(raw, share), floor), vdef)
            NC = N + len(FIG)
            M = np.full((NC, NC), np.nan); M[ca[cc], cb[cc]] = P[cc, 2] - (P[cc, 3] + vc[cc]) - off
            row, col = polish(M)
            sb = np.array(r["sb"], float); groups = np.array(r["groups"], int)
            lc = np.concatenate([sb[groups == 2, 0], sb[groups == 2, 1]]); lc = lc[np.isfinite(lc)]
            Bm = np.full((NM, 2), np.nan)
            for k, n in enumerate(names):
                if n in MAT: Bm[MAT[n]] = sb[k]
            parts = {}
            for kind, lo, hi in (("L", 0, 52), ("F", N, NC)):
                # a partner of that kind, then a mark (its left side); a mark, then a partner (its right side)
                pm = (ca >= lo) & (ca < hi) & (mb >= 0)
                mp = (ma >= 0) & (cb >= lo) & (cb < hi)
                parts[(0, kind)] = dict(partner=ca[pm], mark=mb[pm], dg=P[pm, 2], mg=P[pm, 3], raw=raw[pm], floor=floor[pm], vdef=vdef[pm])
                parts[(1, kind)] = dict(partner=cb[mp], mark=ma[mp], dg=P[mp, 2], mg=P[mp, 3], raw=raw[mp], floor=floor[mp], vdef=vdef[mp])
            cat_ = m["category"] if m else "System"
            adv = r.get("adv")

            def one_width(group):
                w = [adv[k] for k, n in enumerate(names) if n in group] if adv else []
                return len(w) >= 3 and max(w) - min(w) < 0.5
            # a fraction typed with U+2044: the slash's pairs with the numerator before it and the denominator after
            frac = None
            if args.feature_data and "fraction" in names:
                sl = names.index("fraction")
                numr_of = np.array([FIG.index(n[:-5]) if n.endswith(".numr") and n[:-5] in FIG else -1 for n in names])
                dnom_of = np.array([FIG.index(n[:-5]) if n.endswith(".dnom") and n[:-5] in FIG else -1 for n in names])
                isn, isd = numr_of >= 0, dnom_of >= 0
                lp, rp = isn[ga] & (gb == sl), (ga == sl) & isd[gb]
                nn, dd = isn[ga] & isn[gb], isd[ga] & isd[gb]
                frac = dict(s=sb[sl], **{key: dict(dg=P[w, 2], mg=P[w, 3], raw=raw[w], floor=floor[w], vdef=vdef[w],
                                                   a=np.where(of_a[ga[w]] >= 0, of_a[ga[w]], -1),
                                                   b=np.where(of_b[gb[w]] >= 0, of_b[gb[w]], -1))
                                         for key, w, of_a, of_b in (("left", lp, numr_of, dnom_of),
                                                                    ("right", rp, numr_of, dnom_of),
                                                                    ("nn", nn, numr_of, numr_of),
                                                                    ("dd", dd, dnom_of, dnom_of))})
                # the model's sides of each numerator and denominator (NaN: the font has none)
                frac["nsb"] = np.full((10, 2), np.nan); frac["dsb"] = np.full((10, 2), np.nan)
                for k_ in np.where(isn)[0]: frac["nsb"][numr_of[k_]] = sb[k_]
                for k_ in np.where(isd)[0]: frac["dsb"][dnom_of[k_]] = sb[k_]
            mf.append(dict(slug=r["slug"], cat=cat_, style=STYLE_OF.get(cat_, "text"), tab=bool(m and m.get("tabular")),
                           stab=one_width(SUP), btab=one_width(SUB), otab=one_width(OSF),
                           q=(m["quality"] if m and m["quality"] is not None else np.nan), off=off, row=row, col=col,
                           rho=np.median(lc) if len(lc) >= 6 else np.nan, Bm=Bm, parts=parts, frac=frac))
    F2 = len(mf)
    style2 = np.array([fo["style"] for fo in mf]); cat2 = np.array([fo["cat"] for fo in mf])
    q2 = np.array([fo["q"] for fo in mf], float); rho2 = np.array([fo["rho"] for fo in mf]); Bm2 = np.stack([fo["Bm"] for fo in mf])
    sys2 = cat2 == "System"; ref2 = (q2 >= args.min_quality) & ~sys2
    fold2 = np.array([int(hashlib.sha1(fo["slug"].encode()).hexdigest(), 16) % 5 for fo in mf])
    have = np.isfinite(Bm2[:, :, 0]) | np.isfinite(Bm2[:, :, 1])
    KIND = np.array([["F" if FIGURE_SIDES.get(n, (False, False))[sd] else "L" for sd in (0, 1)] for n in MN])
    ISFIG = np.array([n in FIG for n in MN])
    # the small forms: no kerning share (the engine kerns figures and letters fully); a set of one width is kept
    ISSMALL = np.array([n in NOSHARE for n in MN])
    TABKEY = ["tab" if n in FIG else "stab" if n in SUP else "btab" if n in SUB else "otab" if n in OSF else None
              for n in MN]
    tab2 = np.array([fo["tab"] for fo in mf])
    print(f"\nthe rest of the punctuation: {F2} fonts; reference families with each mark:")
    print("  " + ", ".join(f"{n} {int((have[:, k] & ref2).sum())}" for k, n in enumerate(MN)))

    def mvalue(d, share):
        return np.maximum(shape(d["raw"], share), d["floor"])

    def sides_of(fo):
        """(side, the mark side's partners) for each partner kind; a mark side uses the kind it faces in text."""
        for (side, kind), d in fo["parts"].items():
            if not len(d["mark"]): continue
            use = KIND[d["mark"], side] == kind
            if use.any():
                yield side, {k: v[use] for k, v in d.items()}

    def m_targets(Kx, idx):
        T = np.full((F2, NM, 2), np.nan)
        for i in idx:
            fo = mf[i]
            for side, d in sides_of(fo):
                # the partner's own term: a left glyph's right side (row), a right glyph's left side (col)
                term = fo["row"] if side == 0 else fo["col"]
                res = d["dg"] - (d["mg"] + mvalue(d, Kx[d["mark"], side])) - fo["off"] - term[d["partner"]]
                for j in np.unique(d["mark"]):
                    if TABKEY[j] is not None and fo[TABKEY[j]]: continue
                    w = d["mark"] == j
                    need = 10 if KIND[j, side] == "L" else 6
                    if np.isfinite(res[w]).sum() >= need: T[i, j, side] = np.nanmedian(res[w])
        return T

    def m_fit(T, idx, lam=2.0):
        coef = {}
        for j in range(NM):
            for sd in (0, 1):
                X = np.column_stack([(style2[idx] == s).astype(float) for s in STYLES] + [Bm2[idx, j, sd] / 50.0, rho2[idx] / 50.0])
                y = T[idx, j, sd]
                ok = np.isfinite(y) & np.all(np.isfinite(X), 1)
                if ok.sum() < MIN_FAMILIES: continue
                Xo, yo = X[ok], y[ok]
                # a style with too few families takes the text faces' intercept
                present = [(Xo[:, k] > 0).sum() >= 8 for k in range(3)]
                for k in range(1, 3):
                    if not present[k]:
                        Xo[:, 0] += Xo[:, k]; Xo[:, k] = 0.0
                w = np.ones(len(yo)); R_ = np.diag([0 if present[0] else lam, 0 if present[1] else 1e6, 0 if present[2] else 1e6, lam, lam])
                for _ in range(3):
                    A = Xo.T @ (Xo * w[:, None]) + R_
                    beta = np.linalg.solve(A, Xo.T @ (w * yo))
                    res = yo - Xo @ beta
                    sc = 1.4826 * np.median(np.abs(res)) + 1e-6
                    u = np.abs(res) / (1.345 * sc)
                    w = np.where(u <= 1, 1.0, 1.0 / u)
                for k in range(1, 3):
                    if not present[k]: beta[k] = beta[0]
                coef[(j, sd)] = beta
        return coef

    def m_predict(coef, idx):
        out_ = np.zeros((F2, NM, 2))
        for (j, sd), beta in coef.items():
            X = np.column_stack([(style2[idx] == s).astype(float) for s in STYLES] + [Bm2[idx, j, sd] / 50.0, rho2[idx] / 50.0])
            out_[idx, j, sd] = np.nan_to_num(X @ beta)
        return np.clip(out_, -80, 80)

    def m_shares(shifts, idx):
        acc = np.zeros((NM, 2, len(GRID))); cnt = np.zeros((NM, 2))
        for i in idx:
            fo = mf[i]
            for side, d in sides_of(fo):
                t = d["dg"] - d["mg"] - shifts[i, d["mark"], side] - fo["off"]
                E = np.abs(t[:, None] - np.maximum(shape(d["raw"][:, None], GRID[None, :]), d["floor"][:, None]))
                np.add.at(acc[:, side], d["mark"], E)
                np.add.at(cnt[:, side], d["mark"], 1)
        Kx = np.full((NM, 2), KERN_SCALE)
        enough = cnt >= 300
        Kx[enough] = GRID[np.argmin(acc, axis=2)][enough]
        Kx[ISFIG | ISSMALL] = 1.0   # figures and their small forms kern as the model kerns them
        return Kx

    def m_learn(sel):
        idx = np.where(sel)[0]
        Kx = np.full((NM, 2), KERN_SCALE); coef = {}
        Kx[ISSMALL] = 1.0
        for _ in range(ROUNDS):
            coef = m_fit(m_targets(Kx, idx), idx)
            Kx = m_shares(m_predict(coef, idx), idx)
        return coef, Kx

    def m_errors(i, coef_shift, Kx):
        """Per mark: (sum |e| toggle off, with the kernel table's default, learned; count) over its letter pairs."""
        fo = mf[i]
        acc = np.zeros((NM, 4))
        for side, d in sides_of(fo):
            kept = np.array([TABKEY[j] is not None and bool(fo[TABKEY[j]]) for j in range(NM)])
            if kept.any():
                keep = ~kept[d["mark"]]
                d = {k: v[keep] for k, v in d.items()}
                if not len(d["mark"]): continue
            base = d["dg"] - d["mg"] - fo["off"]
            e_off = np.abs(base - d["vdef"])
            e_flat = np.abs(base - mvalue(d, np.where(ISFIG[d["mark"]] | ISSMALL[d["mark"]], 1.0, KERN_SCALE)))
            e_new = np.abs(base - coef_shift[i, d["mark"], side] - mvalue(d, Kx[d["mark"], side]))
            for k, e in enumerate((e_off, e_flat, e_new)):
                np.add.at(acc[:, k], d["mark"], np.nan_to_num(e))
            np.add.at(acc[:, 3], d["mark"], np.isfinite(e_new).astype(float))
        return acc

    cvs = np.zeros((F2, NM, 2)); cvK = np.zeros((F2, NM, 2))
    for k in range(5):
        coef, Kx = m_learn(ref2 & (fold2 != k))
        sel = np.where((fold2 == k) & ~sys2)[0]
        cvs[sel] = m_predict(coef, sel)[sel]; cvK[sel] = Kx
    full2, Kx2 = m_learn(ref2)
    si2 = np.where(sys2)[0]
    cvs[si2] = m_predict(full2, si2)[si2]; cvK[si2] = Kx2
    learned = sorted({j for (j, _sd) in full2})
    # held out, per mark: mean |difference| over its pairs with letters (fonts whose family it was not learned on)
    tot = np.zeros((NM, 4)); tot_sys = np.zeros((NM, 4))
    for i in range(F2):
        acc = m_errors(i, cvs, cvK[i])
        if sys2[i]: tot_sys += acc
        else: tot += acc
    print("\nheld out, mean |designer - model| over each mark's pairs with the letters or figures it faces (toggle off / "
          f"unknown mark at {KERN_SCALE:g} / learned):")
    for j in learned:
        n_ = tot[j, 3]
        if n_ <= 0: continue
        faces = "".join(sorted(set(KIND[j])))
        line = (f"{MN[j]:16s} {faces:2s} {int((have[:, j] & ref2).sum()):4d} families  {tot[j, 0] / n_:6.1f} / {tot[j, 1] / n_:6.1f} / "
                f"{tot[j, 2] / n_:6.1f}" + (f"   system fonts {tot_sys[j, 0] / tot_sys[j, 3]:6.1f} / {tot_sys[j, 1] / tot_sys[j, 3]:6.1f}"
                                            f" / {tot_sys[j, 2] / tot_sys[j, 3]:6.1f}" if tot_sys[j, 3] > 0 else ""))
        print(line, flush=True); lines_more.append(line)
    # a mark with a twin: its twin's conventions (applied to its own sides) against its own, held out
    def twin(t):
        """(coefficients per side, shares) of mark t: the kernel's, or learned above."""
        if t in PUNCT:
            k = PUNCT.index(t)
            return {sd: full.get((k, sd)) for sd in (0, 1)}, Kf[k]
        if t in MAT and MAT[t] in learned:
            k = MAT[t]
            return {sd: full2.get((k, sd)) for sd in (0, 1)}, Kx2[k]
        return None
    al_shift = np.zeros((F2, NM, 2)); al_K = np.full((NM, 2), KERN_SCALE)
    for n, t in ALIASES.items():
        tw = twin(t)
        if tw is None or n not in MAT: continue
        j = MAT[n]; coefs, shares = tw
        al_K[j] = shares
        for sd in (0, 1):
            beta = coefs[sd]
            if beta is None: continue
            X = np.column_stack([(style2 == s_).astype(float) for s_ in STYLES] + [Bm2[:, j, sd] / 50.0, rho2 / 50.0])
            al_shift[:, j, sd] = np.clip(np.nan_to_num(X @ beta), -80, 80)
    tot_al = np.zeros((NM, 4))
    for i in np.where(~sys2)[0]:
        tot_al += m_errors(i, al_shift, al_K)
    # each mark: its own conventions, its twin's, or none (an unknown mark, kerned at kern_scale), whichever leaves
    # its pairs closest to the designers held out
    dropped = []
    for j, n in enumerate(MN):
        if tot[j, 3] <= 0 and tot_al[j, 3] <= 0: continue
        own = tot[j, 2] / tot[j, 3] if j in learned and tot[j, 3] > 0 else np.inf
        t = ALIASES.get(n)
        alias_e = tot_al[j, 2] / tot_al[j, 3] if t is not None and twin(t) is not None and tot_al[j, 3] > 0 else np.inf
        none_e = tot[j, 1] / tot[j, 3] if tot[j, 3] > 0 else (tot_al[j, 1] / tot_al[j, 3] if tot_al[j, 3] > 0 else np.inf)
        best = min(own, alias_e, none_e)
        if not np.isfinite(best): continue
        if best == alias_e and np.isfinite(alias_e):
            aliases[n] = t
            choice = t + "'s"
        elif best == own:
            choice = "its own"
        else:
            dropped.append(n)
            choice = "none"
        if t is not None or choice == "none":
            line = (f"{n:16s} its own {own:6.1f}, " + (f"{t}'s {alias_e:6.1f}, " if t else "") +
                    f"none {none_e:6.1f} (held out) → {choice}")
            print(line); lines_more.append(line)
    for (j, sd), beta in full2.items():
        if MN[j] in aliases or MN[j] in dropped: continue
        for si, s_ in enumerate(STYLES):
            table[s_].setdefault(MN[j], [[None] * 3, [None] * 3])[sd] = [round(float(beta[si]), 2), round(float(beta[3]), 3), round(float(beta[4]), 3)]
    for j in learned:
        if MN[j] in aliases or MN[j] in dropped or ISFIG[j] or ISSMALL[j]: continue
        kern[MN[j]] = [round(float(Kx2[j, 0]), 2), round(float(Kx2[j, 1]), 2)]
    for n, c in MORE:
        if c is not None:
            marks[n] = [ord(c)]
    print("aliases:", aliases)
    if args.feature_data:
        frac_lines = learn_fraction_slash(mf, ref2, sys2, fold2, style2, rho2, json.load(open(args.base)))
        lines_more += frac_lines
out = {"format": "kinetikern2-punctuation/2", "kern_scale": KERN_SCALE,
       "about": "Punctuation by convention: what the designers of well-spaced Google Fonts families do with each punctuation mark, "
                "on top of Kinetikern2's spacing. Each side moves by c + m·s/50 + r·ρ/50 units per 1000 em, where s is the side's own "
                "sidebearing as the model sets it and ρ the font's letter rhythm (the median sidebearing of its lowercase), by the "
                "conventions of text, display and handwriting faces; and each side keeps its share of the kerning its shapes "
                "suggest (kern: left, right), a pair the product of its two facing sides', every other mark kern_scale per side. "
                "marks: each mark's characters; aliases: a mark too few families have takes its twin's conventions. "
                "Learned from Spacing QA's measurements.",
       "reference": {"families": int(ref.sum()), "min_quality": args.min_quality}, "styles": table, "kern": kern,
       "marks": marks, "aliases": aliases, "figures": [n for n in FIG if n in table["text"]], "check": lines + lines_more}
out["id"] = kernel_id if not args.more_data else (
    today + "-" + hashlib.sha1(json.dumps([table, kern, marks, aliases], sort_keys=True).encode()).hexdigest()[:8])
# the kernel's 36 marks as in that table: reports checked with it are checked with these (Spacing QA has the kernel only)
out["kernel_id"] = kernel_id
if args.small_data or args.feature_data:
    # the finished table, with the small forms added and nothing else changed
    base = json.load(open(args.base))
    # small caps too (their own switch in the plugins): held out about half a unit closer with conventions of their
    # own, up to 2 (a, x); j, s and u have none (12 October 2026, at the user's request)
    small = [n for n, _c in SMALL]
    if args.feature_data and "fraction.frac" in table["text"]:
        small = small + ["fraction.frac"] + FRACTION_FIGURES
        base["kern"]["fraction.frac"] = kern["fraction.frac"]
    for s_ in STYLES:
        base["styles"].setdefault(s_, {}).update((n, v) for n, v in table[s_].items() if n in small)
    base["marks"].update((n, marks[n]) for n in small if n in marks)
    base["aliases"].update((n, t) for n, t in aliases.items() if n in small)
    key, what = (("feature_forms", "old-style figures, small caps and fractions") if args.feature_data else
                 ("small_forms", "superscripts, subscripts and ordinals"))
    base[key] = [n for n in small if n in base["styles"]["text"] or n in base["aliases"]]
    base["check"] = base["check"] + [what + " (" + today + "):"] + lines_more
    # what Spacing QA applies (the kernel's marks, the symbols and the figures) is unchanged: its reports keep
    # their stamp (qa_id) while the table's id follows the whole table
    base["qa_id"] = base.get("qa_id") or base["id"]
    base["id"] = today + "-" + hashlib.sha1(json.dumps([base["styles"], base["kern"], base["marks"], base["aliases"]],
                                                       sort_keys=True).encode()).hexdigest()[:8]
    out = base
json.dump(out, open(args.out, "w"), indent=1)
print("wrote", args.out, out["id"])
