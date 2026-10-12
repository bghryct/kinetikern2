# Punctuation by convention: what designers do with each mark

11 October 2026. The question: can a switch make the spacing of punctuation
broadly correct, out of the model, designers' practice and whatever else the
data suggest, without breaking anything? Built the same day: the
**Punctuation** switch, the **Conventions** menu and Pass 2's kinds of pairs
(README: *Punctuation by convention*, *Conventions and Pass 2's pair
classes*).

Numbers are in units per 1000 em. "Shape error" is Spacing QA's: the mean
absolute difference between the designer's gap and the model's over the
GF Latin Kernel's 88 scored glyphs (7,744 ordered pairs), with the font's
overall offset removed, at the font's best-fit Looseness; medians over fonts.

## Data

Spacing QA's `punctuation-data` tool ran the bare model (no harness, no kept
joins) on 1,594 Google Fonts families and 30 macOS system fonts. The system
fonts are used only as a check, and stay local. For every pair of scored
glyphs the tool records the designer's gap, the model's sidebearing gap,
Pass 2's interaction contrast, and the kerning under three responses: v1's,
the new defaults, and punctuation kerned at 0.3. Kerning at any other share
follows from the contrast through `Knobs::shape`, plus the two measured
values where a floor decides. The learner
(`tools/kk2_punctuation_learn.py`) recomputes it exactly that way; on 98.6 %
of the pairs no floor decides.

The reference set is the 1,191 families rated 70 or more on google/fonts'
human `/Quality/Spacing` scale. Everything is held out by family (five folds)
and then checked on the system fonts.

## 1. Pairs with punctuation are where the model and designers part

| Fonts | Letter pairs | Pairs with punctuation |
|---|---|---|
| Sans serif (693) | 14.6 | 28.2 |
| Serif (348) | 19.0 | 31.5 |
| Display (405) | 19.5 | 26.9 |
| Handwriting (148) | 28.6 | 36.7 |

About 1.7 times as far on average. Two separate things account for most of
it: where each mark's sides sit, and how much pairs with a mark are kerned.

## 2. Each mark's sides: a blend of the model and the font's rhythm

Per font, a median polish of the residuals gives each punctuation side the
shift its pairs with letters ask for. Across fonts that shift follows
`c + m·s/50 + r·ρ/50`. Here s is the side's own sidebearing as the model
sets it and ρ is the font's letter rhythm (the median Pass-1 sidebearing of
its lowercase). The fit is a robust ridge with intercepts per style (text,
display, handwriting) and slopes shared per mark and side. A negative m
means designers set the mark's side more evenly than the model does. A
positive r means they set it relative to the letters' own rhythm.

## 3. Kerning: each mark keeps its own share

With every pair with punctuation kerned at one share, the best single
value is 0.3. Designers kern those pairs far less than the shapes suggest.
But one value is a compromise between marks that behave very differently.
The best share per mark side (letters as partners, least mean |difference|):

| Mark | Left side (after a letter) | Right side (before a letter) |
|---|---|---|
| period | 0.70 | 0.55 |
| comma | 0.65 | 0.55 |
| ellipsis | 0.60 | 0.35 |
| colon, semicolon | 0.60, 0.55 | 0.45, 0.40 |
| parenleft, parenright | 0.65, 0.50 | 0.55, 0.50 |
| hyphen | 0.35 | 0.35 |
| en, em dash | 0.30 | 0.35 |
| quotes ‘ “ / ’ ” | 0.30 / 0.40 | 0.50 |
| ' " | 0.40 | 0.45, 0.40 |
| exclam, bar, ampersand | 0.90, 0.85, 0.75 | 0.80, 0.80, 0.70 |
| degree, bullet, · | 0.05 | 0.20–0.25 |

A period after a letter, partner by partner (reference fonts; the target is
what designers do given the model's spacing and the conventions' side
shifts):

| Letter | Model's contrast | Designers | At 0.3 | At 0.7 | At 1.0 |
|---|---|---|---|---|---|
| T | −154 | −55 | −20 | −51 | −66 |
| V | −178 | −67 | −24 | −57 | −72 |
| Y | −157 | −67 | −20 | −52 | −67 |
| W | −112 | −52 | −11 | −38 | −53 |
| P | −285 | −71 | −41 | −76 | −88 |
| r | −134 | −39 | −16 | −45 | −60 |
| v, w, y | −127, −83, −122 | −45, −38, −40 | −14, −5, −13 | −43, −27, −42 | −58, −40, −56 |
| n, o, e | 0, −32, 0 | +4, −4, +3 | 0 | 0, −3, 0 | 0, −10, 0 |

Designers tuck a period under an overhang at about 0.7 of the model's drive.
Where nothing overhangs, the dead zone keeps the pair at 0, as designers
do.

Two marks side by side: the best share is 0.0–0.2 (mean |difference| 27.7 at
0–0.1, 27.9 at 0.3, 31.9 at 1.0). The product of the two sides' shares gives
about that (0.3 × 0.3 = 0.09). Per style the best shares differ by up to
0.3 (a period after a letter 0.7 in text and display faces, 0.5 in
handwriting; a closing quote before a letter 0.6, 0.4 and 0.3). Shares per
style gave no better held-out result, though: handwriting, with the fewest
families, got worse. So the shares are shared.

## 4. Held out, and the pairs readers see

| Fonts | v1 | Pass 2's classes | One share (0.3) | Each mark's share |
|---|---|---|---|---|
| Sans serif, all pairs | 22.95 | 22.96 | 18.21 | 18.15 |
| … a period or comma under T V W Y P F r v w y | 30.8 | 30.8 | **39.1** | 30.5 |
| Serif, all pairs | 26.66 | 26.68 | 21.28 | 21.27 |
| … under an overhang | 37.9 | 37.8 | **43.9** | 38.5 |
| Display, all pairs | 24.13 | 24.13 | 19.76 | 19.77 |
| Handwriting, all pairs | 33.91 | 33.79 | 29.78 | 29.93 |
| macOS system fonts, all pairs | 32.21 | 32.27 | 24.83 | 24.80 |
| … under an overhang | 54.0 | 54.0 | **79.0** | 53.1 |

A total over 7,744 pairs weighs a `T.` as much as a `]@`. One share kept the
totals and made the pairs every kerning lesson starts with worse than no
convention at all. Each mark's own share keeps the totals and brings those
pairs back to where they were without it. Designers vary a lot there, from
font to font: the spread inside each partner's row is about ±30, which no
rule common to all fonts can follow.

## 5. Kinds of pairs (Pass 2's response by pair class)

The same data, split by the kinds of the two glyphs:
- v1's crevice floor kept a letter out of its neighbour's hollows. Across
  the library it puts lowercase pairs and a capital before a lowercase
  letter further from their designers, while it brings pairs with
  punctuation closer. Those two kinds go without it. Tangerine's fo: v1 +72,
  now −8. The gap is −156, against the designer's −196 (v1: −75).
- Designers kern a lowercase letter before a capital further than the
  saturating response allows. Past 20 units the value keeps growing at half
  the rate.
- How far a capital reaches over its neighbour depends on the kind of face:
  text faces as the response saturates, display faces at 0.85 past 20
  units, handwriting fully (Tangerine's Ha −59 → −87; its designer's gap is
  58 units tighter still, a Pass-1 matter: the H's right side is read in
  its cap-height zone).

Over all of Tangerine's pairs, measured along its 13.8° slant: v1 35.4,
text faces 34.9, display 34.5, handwriting 34.4; capital before lowercase
30.3 → 29.2 → 26.5 → 25.7.

## 6. The rest of the punctuation, the symbols, figures and case forms

The same day, at the user's request ("do the guillemets and low quotes too…
do all punctuation"), the measurement was widened. `punctuation-data
--all-punctuation` reads every font with 36 more marks (`glyphset::
PUNCTUATION_MORE`: ¡ ¿ « » ‹ › ‚ „ ‛ ‟ § ¶ † ‡ ‰ ‱ ′ ″ ‴ ¦ ‐ ‑ ‒ ― ․ ‥ ‧ ‣ ‼ ‽
‾ ※ ⁂ ⁄ ʼ №) and writes every pair, figures and the kernel's unscored
symbols included. The kernel's 36 marks are learned first and kept as they
were. The rest are learned the same way, against the letters of the same
run. Four things changed:

- **Which partners.** A side that faces figures in text is learned against
  the figures: the 15 symbols that sit with them (# $ + < = > ^ ~ ¢ £ ¥ × ÷
  € −), ‰ ‱ after a figure, № before one, ′ ″ ‴ and ⁄ on both sides. Each
  figure's own deviation (a tabular design's widths included) comes out of
  the same median polish first.
- **Own, twin or none.** A rare mark's own fit can be worse than none. So
  each mark gets its own conventions, its twin's (the hyphen's for ‐, the
  question mark's for ‽), or none (the model's reading, kerned at 0.3),
  whichever is closest to the designers held out. The primes, = ¥ − and the
  one-dot leader get none; eight marks take their twin's.
- **Figures.** Across 1,151 families with proportional figures the model
  sets every figure side 6.5–25.5 units tighter than the designers (medians;
  the 1 most: +16.5 / +25.5). They are learned like the marks, against the
  figures and without a kerning share, on proportional designs only. Held
  out, over each figure's pairs with figures: the 1 37.5 → 26.8, the 2 29.1 →
  22.0, the 3 28.1 → 21.5, the 7 29.5 → 24.4. A switch of its own,
  **Figures**; tabular figures keep their widths either way.
- **Case forms.** In the 145–182 families whose `case` feature maps a mark,
  7 case forms in 10 have exactly the base mark's sidebearings, and most of
  the rest are a few units looser (median difference over all: 0). The
  engine gives a case form its base mark's sides (`Conventions::follow`).

Held out, the largest gains: + 59.3 → 26.1 (switch off → its own), > 55.0 →
27.8, × 49.9 → 29.6, ¿ 40.9 → 23.3, † 43.4 → 29.1, # 34.9 → 24.3, $ 31.8 →
23.0, ― 47.7 → 35.1. The guillemets come 24.7 → 19.1, the low quotes 26.3 →
23.8 and 24.9 → 22.0.

Two lookups needed no new measurement. The Greek question mark and the ano
teleia are canonically the semicolon and the middle dot in Unicode, so they
take those marks' conventions. Proportional lining figures under a suffix
(`one.lf`, `one.pnum`, `one.plf`) take their figure's conventions. A font has
them when its default figures are tabular or old-style. Without the lookup,
such a font got nothing from the Figures switch: its default figures keep
their widths, and the alternates have no code point. They are the shapes
the figures were learned on.

## 7. What it does not do

- Marks outside the table keep the model's reading, kerned at 0.3. That is
  the rest of Unicode's punctuation: Armenian, the Devanagari danda, CJK
  forms. (The Greek question mark and ano teleia are in the table as twins.)
- Superscripts, subscripts and ordinals: measured against the model at the
  user's request (11 October 2026, `punctuation-data --small-forms`, the same
  1,625 families) and learned like the marks, sides only (the engine kerns
  figures and letters fully). They go under the Figures switch. Designers
  keep superscripts about 0.8 times their figure's sidebearings at 0.59 times
  its size (1,372 fonts), ordinals 0.83–0.90 of a's and o's at 0.68 (1,733).
  Held out: ¹ 28.4 → 25.1, ⁷ 34.3 → 23.7, ₁ 31.4 → 25.1, ª º 25.1 / 25.0 →
  22.3 / 22.1; the rest gain under a unit to 2 units. On the macOS fonts ²
  and ³ come out about a unit worse. A set that shares one width (Arial's
  ¹ ² ³) keeps the model's spacing; `.sups` / `.sinf` forms take their
  encoded form's conventions.
- `.case` forms follow their base mark. Proportional lining figures under a
  suffix (`.lf`, `.pnum`, `.plf`) take their figure's conventions. Other
  variants (`.sc`, old-style `.osf`) are left as the model spaces them,
  because they were not measured. Tabular forms keep their widths.
- The conventions are per style, not per font. What is left is each
  designer's own taste. The designer harness, learned on top of these
  conventions, corrects what is common to well-spaced text faces.
- The word space: Kinetikern2 does not set it (it has no outline). Designers
  set it near 250 units per 1000 em in every category, and in text faces in
  step with the lowercase: about 0.42 times the n advance in sans serifs
  (median error 7.7 %) and 0.40 in serifs (9.7 %); display and handwriting
  faces are freer (about 18 %).
- The other blank spaces: designers keep them equal to what they stand for.
  Across 1,852 Google Fonts (Regular, not monospaced), the no-break space
  equals the word space in 96 % of 1,740. The punctuation space equals the
  period in 70 % of 299 (76 % within 1 % of the em), and the figure space a
  tabular figure in 67 % of 208, and in fonts without tabular figures the
  zero in 42 % (62 of 147). Kinetikern2 sets the period's and the
  proportional figures' sides with the switches on or off. So Apply now keeps
  a blank punctuation space that is exactly as wide as the period in step
  with it, and a blank figure space exactly as wide as the zero in step with
  the zero where no figure keeps its advance (11 October 2026, at the user's
  request; both plugins, revert and read-back included). Blank means no
  outline, or only a one-point dummy contour as in Arial. In the self-tests
  Alice's spaces move with its period (222 → 226) and zero (585 → 552), and
  Arial's figure space keeps 1139 with its tabular figures.
