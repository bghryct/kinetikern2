# Connected scripts: ugly combinatorial pairs, a deep dive

9 October 2026. Follows `connected-scripts.md` (the join mode as built on
8 October). Question: how do we make sure Kinetikern2 does not produce ugly
combinatorial pairs in connected scripts, and what should stay manual?

Numbers are in units per 1000 em. "The model" is Kinetikern2 with its
designer harness, as Spacing QA's reports have it at each family's best-fit
Looseness. How everything was measured is in section 2.1 and the appendix.

## Summary

- **A join fixes where the next letter sits.** In a joining pair the strokes
  have to meet, so the pair's offset (advance of the first letter plus the
  kern) is set by the drawing, inside a small window. There are 52
  sidebearings for up to 676 joins. Any change to a joining side moves every
  join on that side, and two moved sides add up on their pair:
  ΔS(a, b) = Δrsb(a) + Δlsb(b) + Δkern(a, b). Breaks come in whole rows and
  columns of the pair table. That is the "combinatorial" in ugly
  combinatorial pairs.
- **The window is small.** Across 85 joining scripts on Google Fonts
  (45,665 joins), a join breaks when its pair opens by a median 22 units
  (a family's 10th percentile: median 13). Closing is usually safe: 89 % of
  joins survive 100 units of tightening. The other 11 % are mostly angled
  joins, where the exit stroke passes in front of the entry when tightened.
  Today's model changes joining sides by 32 units RMS, which is 1.5 times
  the window.
- **What today's join mode does.** At each family's fitted Looseness it
  breaks a median 11 % of a family's joins (8,097 joins in all). With the
  overall tightness set to suit the joins (the median change taken out), it
  breaks a median 21 % (9,603). It breaks 10 % or more in 68 of the 85
  families. It also creates 6,861 crossings or collisions, where the strokes
  enclose new white or a body pushes into a counter. Of the breaks at the
  best tightness, 79 % come from side changes alone. 17 % come from pairs
  that join in the font but are not join pairs for the engine, which then
  kerns them apart. 3 % come from dropped designer kerning.
- **Any per-side spacing breaks some joins.** Freezing the 5 worst sides
  (of about 51) halves the breaks. Getting to zero means freezing nearly
  every joining side and keeping the font's own kerning on join pairs. If
  every join is held inside half of its window, the sidebearings can keep a
  median 29 % of the model's side-to-side variation (53 % with the full
  window). Without the join mode the model breaks 80–99 % of joins, so the
  mode was necessary. But it spaces bodies, and the designers spaced the
  connection points. Measuring along the slant does not change this
  (section 2.5).
- **The designers' own joins are mostly sound.** Only 506 of 44,641
  expected joins are broken in the fonts themselves (1.1 %, in 39 of 85
  families). They cluster after high exits (w, o, v, c) and before
  m, r, s, z, f. 2.3 % of joins are fragile, with less than 5 units of room.
  Where designers fix combinations, they use ligatures (Dancing Script:
  o_r, v_r, b_r, w_r) or calt (Style Script: 112 pairs joined only by calt;
  Kristi: after f, o, t, v and w).
- **calt-built scripts are invisible today.** TypeTogether's 104 Playwrite
  families join only through calt. Zero-width connector glyphs (313 of them,
  13 exit classes × 29 entry classes, 342 lowercase forms) are inserted
  between letter forms. With calt off, as Spacing QA and the detector see
  them, they do not join. Only 12 of the 85 joining scripts use calt on a–z
  pairs, and none of the 355 handwriting families uses cursive attachment
  (`curs`) for Latin.
- **Recommendation.**
  1. First, build a join checker: pair-level, shaped with calt in a word
     context, exact contact, gap, window and crossings, grouped by side, with
     proof strings. It goes in Spacing QA (library metric "joins kept") and
     in the plugin (before Apply and on the preview).
  2. Make "Keep joins" the conservative default of the Connected script
     setting. Joining sides keep their sidebearings. Pairs that join keep
     the font's kerning. The model spaces and kerns everything else
     (punctuation, figures, capitals that do not join, letter–punctuation
     pairs). What it wanted for the joining sides is reported as drawing
     advice. The engine already has the parts: `SideRule::Fixed`,
     `GlyphOpt::frozen`, `fit_frozen`.
  3. Keep the tolerance mode and the calt suggestions as later, opt-in work.
  4. Drawing the joins, choosing alternates and writing calt stay with the
     designer.

---

## 1. What makes an ugly combinatorial pair

### 1.1 A join fixes where the next letter sits

Between text letters the spacing decides how much white there is, and the
outlines do not care. Between joining letters the outlines decide. The exit
stroke of `a` has to meet the entry stroke of `b`, so the pair's offset

    offset(a, b) = advance(a) + kern(a, b)

is set by the drawing, inside a window [−close, +open]. Within that window
the strokes still meet. A right sidebearing serves all 26 partners at once,
and so does a left sidebearing. The basic a–z has 52 sidebearings and up to
676 joins. When every join is drawn to meet at kern 0, the joins fix every
joining sidebearing up to one shared constant: move all right sides by +c and
all left sides by −c and nothing visible changes. Nothing else moves without
moving joins.

The data suggests designers build scripts this way: their joining
sidebearings follow the connection points more closely than the bodies. The
model without its join mode measures from the stroke tips. Its pair changes
spread about a third less than with the join mode, which measures from the
bodies (IQR 15–27 against 24–43 units, section 2.5).

### 1.2 Side changes add up pair by pair

For a pair the engine treats as a join (both facing sides have join bands),
Kinetikern2 sets no kerning (`PAIR_JOIN` in `pass2.rs`: `solve`, `verify`
and `finish` all return 0 when `a.joins(b)`). The change in the pair's
offset is therefore exactly

    ΔS(a, b) = Δrsb(a) + Δlsb(b) − designer kern(a, b)

(the general form, for any pair, is Δrsb(a) + Δlsb(b) + Δkern(a, b)).

Two unremarkable side changes add up on their pair. Example: Dancing Script.

- The model opens t's right side by 19.7 and u's left side by 24.8. "tu"
  opens by 44.5 against a window of 19, and the join breaks with a 25-unit
  gap.
- The same t breaks with every letter whose left side was opened (h, k, l…).
  The same u breaks with every letter whose right side was opened
  (c, i, z, l…).
- The 163 broken pairs (of 650 joins) line up by row: c, i, t 18 each, z 17,
  l 15, e 13, f 12. They line up by column too: u 15, h, k, l 11 each.
- The rows and columns are the sides with the largest changes: c +22.2,
  t +19.7, z +19.0, i +16.7 on the right; u +24.8, t +22.9, l +21.3,
  b +21.4 on the left.

Predicting a break as "ΔS outside the pair's window" agrees with the actual
outlines for 648 of Dancing Script's 650 joins. The window describes the
mechanism, and a checker can use it without redoing the geometry.

### 1.3 How much room a join has

The second letter was moved right (opening) and left (closing) until the
inks no longer touch. Over the 85 joining scripts:

| Measure | Value | Notes |
|---|---:|---|
| Opening window, median of the family medians | 22 | a family's 10th percentile: median 13. Wider in fat-stroked scripts with deep overlaps: Sacramento 35, Satisfy 49, Damion 65, Pacifico 79 |
| Joins that survive 100 units of closing (all 45,665 joins) | 89 % | |
| Fragile joins: opening window under 5 (all joins) | 2.3 % | Parisienne 23 %, Montez 18 %, Passions Conflict 17 % |
| Joins that only touch: window under 1 | 330 | |
| Model's change per joining side, RMS (family median) | 32 | |
| Spread of the model's pair changes, IQR (family median) | 33 | |

A side change that is small for a text face is a whole window for a script.

### 1.4 Heights and angles

Two kinds of geometry make a join fragile in a way no single sidebearing
can fix.

**High exits.** Most letters leave a join low, around 0.3–0.5 of the
x-height. A few leave it high. Measured as the median height where a
letter's joins touch, the high exits (at least 0.6 x-height) are:

| Letter | Families where it exits high / where it joins |
|---|---|
| v | 30 / 68 |
| f | 25 / 81 |
| o | 22 / 70 |
| w | 22 / 64 |
| b | 16 / 57 |

t, d, s and r follow at about 10. A high exit meeting a low entry either
misses it or lands somewhere else: on the shoulder, or across a hairline.
The designers' own broken joins sit there.

- By first letter: w 52, o 44, v 33, c 32, g 30.
- By second letter: m 44, r 30, s 30, z 29, f 28.

Birthstone Bounce's "or" is an example. o leaves at the x-height, r's lead-in
rises from the baseline, and they never meet (11 units apart). Moving one of
them sideways does not fix a height mismatch. The cure is a different glyph:
an alternate or a ligature. Its sibling Birthstone has the same broken "or"
(34 units apart) in its default forms and replaces it with an o_r ligature.

**Angled joins.** When an exit stroke meets a diagonal entry hairline at an
angle, the two touch only within a narrow horizontal range. Dancing Script's
"or" is an example: the model closes it by 35 units. The r's hairline then
passes in front of the o's exit loop and misses it by 3.4 units, which is a
dangling tip, not a tighter join. Its closing window is 30 units. 11 % of
all joins (5,101) break when closed by less than 100 units.

### 1.5 Where today's join mode breaks joins

The join mode as built (`joins.rs`, `engine.rs` `prepare_glyph`, `pass2.rs`)
works like this. Pass 1 spaces each joining side's body, which is the side's
profile with its join band left out (`SdfProfile::masked`). The join stroke
overhangs. A pair whose facing sides both join gets no kerning and no floors.
Measured over 85 families at the best overall tightness, 9,603 joins break.
They come from three sources:

| Cause | Breaks | Share |
|---|---:|---:|
| Side changes alone (ΔS = Δrsb + Δlsb outside the window) | 7,633 | 79 % |
| The pair joins in the font, but not both its sides have join bands. The engine treats it as an ordinary pair and kerns it apart with the no-overlap floors. The model kerns 14 % of the fonts' joins. | 1,663 | 17 % |
| The designer kerned the join pair and the join mode resets it to 0 | 307 | 3 % |

The detector decides joins per side, by majority: a side joins where it
overlaps at least half of the a–z partners. A letter that joins most
partners but not all, or a partner that only some letters reach, leaves
pairs that join in the drawing but are spaced as if they did not.

Two smaller findings:

- **The join band is a single span of heights,** from the lowest to the
  highest join height (`joins.rs`: `let (lo, hi) = (*on.first()?,
  *on.last()?)`). Masking it removes the body's edge over that whole span.
  When the model is far from the font, bodies collide. At their fitted
  Looseness, Alex Brush's "ba" (ΔS −101) pushes a's body into b's bowl, and
  "ad" (ΔS −52) puts d's bowl over a's exit. Great Vibes at −6 runs n's
  first stroke through a. The 6,861 crossings counted in section 2.4
  include collisions like these as well as strokes that cross.
- **The self-test checks boxes, not ink.** Its connected stage
  (`kk2_selftest.py`, `connected_on_ready`) counts a join pair as
  "overlapping" when rsb + lsb + kern < 0. 127 of the 163 joins the model
  breaks in Dancing Script still pass that test, because the overhanging
  strokes keep the boxes overlapped after the inks have parted. The engine
  test `a_connected_script_overlaps_at_its_joins` has the same blind spot.

### 1.6 How designers deal with it

Four patterns show up in the 85 joining scripts and in the Playwrite
families:

1. **Draw to a common connection.** Most scripts (73 of 85 have no calt on
   a–z pairs) rely on every exit meeting every entry. Entries are often long
   diagonal hairlines from the baseline that accept exits at any height.
   That is why Great Vibes' "on" works: o's loop crosses n's upstroke
   halfway up. The cost is that the join point slides along the hairline
   when the spacing changes.
2. **Ligatures for the worst combinations.** Dancing Script has
   o_r, v_r, b_r, w_r, e_e, l_l, t_h. Great Vibes has o_b, o_r, o_x, e_m,
   e_n. Birthstone has b_r, b_s, b_v, b_w, o_r, o_v. 395 a–z pairs are
   ligated across the 85 families.
3. **Contextual alternates.** In a word context, calt joins 112 extra pairs
   in Style Script (after b, o, p and v), 44 in Edu NSW ACT Cursive, 65 in
   WindSong, and 10 in Kristi (after f, o, t, v and w). calt also makes
   deliberate pen lifts. Pacifico swaps in a b without an exit stroke before
   x. A checker has to tell a lift from a break.
4. **A connector system.** Playwrite: letters come in initial, medial and
   final forms with exit and entry classes. A zero-width connector glyph
   (GDEF mark class, positioned by GPOS) is inserted between every two
   letters. There are 313 connectors in Playwrite AU VIC, covering 13 exit
   classes × 29 entry classes, with 342 lowercase forms. The join is
   drawn once per exit/entry combination, which is the combinatorial
   problem solved by hand. These joins have much more room: an opening
   window of 55–63 units against 22 for drawn-to-join scripts, and at least
   100 for closing.

---

## 2. Measurements on Google Fonts

### 2.1 Method

- **Fonts.** The cached Google Fonts files in `SpacingQA/data/fonts/`, at
  each report's location (wght 400).
- **Shaping.** HarfBuzz (uharfbuzz 0.56). Every ordered a–z pair is shaped
  twice: with `liga clig dlig hlig calt rclt rlig` off (Spacing QA's frame),
  and with the default features on, which is what a browser shows. For the
  calt families it was also shaped inside a word ("n" + pair + "n").
- **Geometry.** Exact outlines with skia-pathops booleans.
  - **Join.** A pair joins when the union of the two letters' ink has fewer
    pieces than the two letters alone (touching counts).
  - **Gap.** The closest distance between the inks of a pair that does not
    join.
  - **Window.** How far the second letter can move right (opening) or left
    (closing) before the join breaks: the first break, found in 2- and
    4-unit steps and refined to 0.25.
  - **Crossing.** The union has an enclosed white shape, or a changed
    counter, that neither letter has alone: crossed strokes or a collision.
  - **Contact height.** The centroid of the overlap.
- **Model.** From each Spacing QA report (`detail.glyphs[].best`,
  `detail.kerning.best` against `designer`), applied as the change of each
  pair's offset ΔS. "As fitted" uses ΔS as it is. "Best tightness" takes the
  median ΔS over the font's joins out first, which is the single overall
  tightness that suits the joins best. Fresh `spacingqa check` runs
  reproduce the stored reports exactly (Dancing Script: identical sides).
- **Families.**
  - 85 joining scripts: the 106 families Spacing QA reports as connected
    (`join_sides`), less 18 that overlap by design or are not primarily
    Latin (Flow ×3, Honk, Rubik ×4, Zilla Slab Highlight, Montserrat
    Underline, Redacted Script, Faster One, Galada, Pattaya, Sirivennela,
    Taprom, Fasthand, Freehand), less 3 guide-line variants (Betania Patmos
    GDL ×2, Edu AU VIC WA NT Guides).
  - 16 Playwrite families.
  - 6 families checked again with `--no-joins` and with `--slant`
    (13 checker runs in all).
- **Joining sides and expected joins.** A side counts as joining when it
  joins at least half of its a–z partners in the font. Expected joins are
  pairs of two joining sides.

### 2.2 The library: who joins, and how

| | Families |
|---|---:|
| Reported connected by Spacing QA (`join_sides` > 0) | 106 |
| … joining scripts after removing overlap-by-design, non-Latin and guide variants | 85 |
| … with calt substitutions between a–z letters | 12 |
| … with ligatures between a–z letters | 53 |
| … with cursive attachment (`curs`) | 0 |
| Handwriting families with calt (all 355) | 148 (104 of them Playwrite) |
| Handwriting families with `curs` | 1 (Playpen Sans Arabic) |
| Playwrite families found connected by the detector | 0 of 104 |

### 2.3 The designers' own joins

| | Count |
|---|---:|
| a–z pairs in the 85 families | 57,460 |
| … that join (calt off) | 45,665 (79 %) |
| Expected joins (both sides join most partners) | 44,641 |
| … broken in the font itself | 506 (1.1 %); none in 46 families |
| … of those, joined by calt or a ligature | 53 |
| … of those, that touch with a kern within 80 units | 467 (median −10; 90 % within 38) |
| … that touch with a kern of 10 units or less, at the letter's usual join height, without crossing | 226 |
| Crossings in the fonts' own joins (calt off) | 1,426 pairs |

Families with the most broken joins:

| Family | Broken joins | Examples |
|---|---:|---|
| Dr Sugiyama | 78 | am, an, ar, av |
| Meie Script | 42 | bm, bp, cb |
| Edu NSW ACT Cursive | 42 | fa, fc, fd (34 joined by calt) |
| Dawning of a New Day | 36 | cf, cj, ck |
| Mr De Haviland | 32 | ba, bc, bd |
| Birthstone Bounce | 25 | od, oe, oo, or |
| Cedarville Cursive | 24 | dg, fg, fh |
| League Script | 23 | br, bs, bt |
| Lavishly Yours | 23 | gc, ge, gi |
| Comforter | 22 | oa, oc, od |

Some of these are deliberate pen lifts after descenders (g, j, y) or
before x and z. A checker should list them, not judge them.

### 2.4 The model's joins

All 85 families:

| | As fitted | Best tightness |
|---|---:|---:|
| Joins broken, median per family | 11 % (p75 28 %, max 85 %) | 21 % (IQR 11–30 %, max 60 %) |
| Joins broken, total | 8,097 | 9,603 |
| Families with 10 % or more broken | | 68 |
| Crossings and collisions made | 6,861 (62 families with 10 or more) | |

Selected families (joins of the 676 a–z pairs; windows and changes per
1000 em):

| Family | Looseness | Joins | Broken in font | calt/liga pairs | Opening window (median / p10) | Broken by model, as fitted | Broken by model, best tightness | Crossings made |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Dancing Script | −0.66 | 650 | 0 | 9 | 20 / 16 | 163 (25 %) | 121 (19 %) | 1 |
| Pacifico | −0.47 | 663 | 13 | 651 | 79 / 61 | 103 (16 %) | 116 (17 %) | 76 |
| Great Vibes | −6.00 | 600 | 0 | 16 | 16 / 10 | 23 (4 %) | 206 (34 %) | 248 |
| Sacramento | −0.61 | 676 | 0 | 2 | 35 / 32 | 60 (9 %) | 107 (16 %) | 19 |
| Satisfy | −1.23 | 559 | 1 | 0 | 49 / 29 | 3 (1 %) | 7 (1 %) | 65 |
| Cookie | −0.59 | 627 | 0 | 1 | 40 / 25 | 82 (13 %) | 3 (0 %) | 19 |
| Allura | −1.88 | 467 | 0 | 3 | 22 / 16 | 56 (12 %) | 133 (28 %) | 48 |
| Alex Brush | −1.66 | 412 | 0 | 3 | 22 / 14 | 37 (9 %) | 86 (21 %) | 94 |
| Parisienne | −1.73 | 586 | 0 | 2 | 20 / 0 | 108 (18 %) | 121 (21 %) | 17 |
| Lobster | −0.42 | 461 | 0 | 487 | 42 / 41 | 9 (2 %) | 10 (2 %) | 16 |
| Style Script | −0.76 | 440 | 16 | 251 | 13 / 7 | 109 (25 %) | 104 (24 %) | 0 |
| Pinyon Script | −6.00 | 676 | 0 | 2 | 17 / 6 | 18 (3 %) | 275 (41 %) | 142 |
| Clicker Script | −0.64 | 648 | 3 | 4 | 22 / 1 | 321 (50 %) | 145 (22 %) | 0 |
| Rochester | −0.89 | 650 | 0 | 0 | 22 / 18 | 207 (32 %) | 93 (14 %) | 0 |
| Birthstone | −0.16 | 451 | 0 | 18 | 14 / 8 | 251 (56 %) | 69 (15 %) | 5 |
| Petit Formal Script | −0.59 | 365 | 0 | 28 | 17 / 16 | 309 (85 %) | 144 (39 %) | 0 |
| Grand Hotel | 0.12 | 600 | 0 | 2 | 59 / 55 | 26 (4 %) | 27 (4 %) | 26 |
| Comforter | 0.02 | 336 | 22 | 24 | 18 / 6 | 241 (72 %) | 40 (12 %) | 5 |

How to read the table:

- Families at −6 (Great Vibes, Pinyon Script) look safe as fitted only
  because the model crams them: their joins hold but cross (248 and 142
  crossings).
- Fat-stroked scripts with deep overlaps (Satisfy, Cookie, Grand Hotel,
  Lobster) have wide windows and few breaks.
- Hairline scripts have narrow windows and many breaks: Petit Formal Script
  and Pinyon Script (17), Parisienne (20), Italianno (13 and 54 % broken at
  the best tightness; not in the table).

### 2.5 Join mode, no join mode, measuring along the slant

Six families, checked again with `spacingqa check --no-joins` and `--slant`:

| Family | Mode | Looseness | Shape error | Broken, as fitted | Broken, best tightness | Pair-change IQR | Crossings |
|---|---|---:|---:|---:|---:|---:|---:|
| Dancing Script | join mode | −0.66 | 42.4 | 25 % | 19 % | 24 | 1 |
| | no join mode | −1.85 | 49.2 | 96 % | 9 % | 20 | 0 |
| | join mode + slant (12°) | −0.51 | 32.5 | 71 % | 21 % | 27 | 1 |
| Sacramento | join mode | −0.61 | 44.1 | 9 % | 16 % | 28 | 19 |
| | no join mode | −1.31 | 53.9 | 97 % | 1 % | 15 | 0 |
| | join mode + slant | −0.64 | 44.2 | 3 % | 14 % | 31 | 23 |
| Great Vibes | join mode | −6.00 | 87.4 | 4 % | 34 % | 43 | 248 |
| | no join mode | −6.00 | 83.4 | 97 % | 24 % | 27 | 0 |
| | join mode + slant (22°) | −1.86 | 90.8 | 25 % | 28 % | 22 | 31 |
| Allura | join mode | −1.88 | 76.8 | 12 % | 28 % | 30 | 48 |
| | no join mode | −6.00 | 80.0 | 99 % | 9 % | 19 | 0 |
| | join mode + slant (19°) | −0.92 | 86.2 | 24 % | 22 % | 29 | 12 |
| Pacifico | join mode | −0.47 | 48.2 | 16 % | 17 % | 30 | 76 |
| | no join mode | −6.00 | 68.1 | 80 % | 0 % | 22 | 0 |
| Alex Brush | join mode | −1.66 | 71.1 | 9 % | 21 % | 30 | 94 |
| | no join mode | −6.00 | 76.8 | 96 % | 10 % | 20 | 0 |

(Pacifico and Alex Brush have no measurable slant; `--slant` leaves them as
they are.)

Three things follow.

- **The join mode was necessary.** Without it the no-overlap rule pulls
  80–99 % of joins apart, and no Looseness can fix that: the fit sits at −6
  and the floors are absolute.
- **The join mode moves the reference for joining sides from the connection
  points to the bodies.** With the overall offset taken out, the stroke-tip
  model (no join mode) breaks fewer joins (0–24 %) than the body model
  (16–34 %), because its side changes are more uniform (IQR 15–27 against
  24–43). The designers' joining sidebearings follow the connection points.
  Measuring from the bodies adds the bodies' irregularity to every join.
- **Measuring along the slant does not help the joins.** It changes the
  overall tightness: Great Vibes −6.00 → −1.86, Allura −1.88 → −0.92. At the
  best tightness, though, it breaks 14–28 % of joins, against 16–34 %
  without it, and Dancing Script gets slightly worse (19 % → 21 %). Joins are
  horizontal offsets, which a shear keeps. Slant only changes what the model
  thinks of the bodies.

### 2.6 calt-built scripts: Playwrite

Sixteen Playwrite families, every a–z pair shaped with calt on (the default
features):

| Family | Pairs touching, calt off | Joined, calt on | … through a connector | Opening window (median / p10) | Model's side changes applied to the calt forms: joins broken |
|---|---:|---:|---:|---:|---:|
| Playwrite AR | 176 | 675 | 675 | 57 / 49 | 171 |
| Playwrite AT | 181 | 672 | 672 | 62 / 51 | 160 |
| Playwrite AU NSW | 0 | 494 | 494 | 60 / 54 | 0 |
| Playwrite AU QLD | 33 | 516 | 468 | 60 / 55 | 38 |
| Playwrite AU SA | 1 | 494 | 494 | 61 / 51 | 0 |
| Playwrite AU TAS | 1 | 468 | 468 | 60 / 51 | 0 |
| Playwrite AU VIC | 33 | 555 | 520 | 60 / 53 | 8 |
| Playwrite BE VLG | 143 | 675 | 675 | 58 / 52 | 95 |
| Playwrite BE WAL | 208 | 675 | 675 | 56 / 49 | 199 |
| Playwrite BR | 175 | 675 | 675 | 57 / 49 | 177 |
| Playwrite CZ | 179 | 675 | 675 | 60 / 54 | 140 |
| Playwrite DE LA | 152 | 675 | 675 | 61 / 54 | 102 |
| Playwrite FR Trad | 169 | 675 | 675 | 56 / 49 | 212 |
| Playwrite GB J | 0 | 675 | 675 | 55 / 49 | 0 |
| Playwrite IT Moderna | 0 | 675 | 675 | 55 / 49 | 0 |
| Playwrite US Trad | 197 | 675 | 675 | 63 / 55 | 152 |

The model column assumes each alternate moves like its base letter,
connectors not at all. It is an estimate: Spacing QA does not space the
alternates.

- Nearly every join goes through a connector glyph. The exceptions are the
  letters that touch directly in AU VIC (35) and AU QLD (48).
- The Australian models leave several letters unjoined on the right by
  design (AU NSW: b f g j p s y).
- Where the default forms touch their neighbours (AR, AT, BR, US Trad,
  FR Trad, BE WAL), the model without joins pulls them apart, and even the
  connectors' wide windows break (95–212 joins).
- Where the default forms never touch (AU NSW, SA, TAS, GB J, IT Moderna),
  the connectors absorb the model's changes.

Kinetikern2 sees none of these joins today. The detector reads calt-off
spacing. The connectors are marks in the compiled fonts (GDEF class 3). If
they are marks in the source too, the plugin does not space them
(`kk2_snapshot.py` spaces letters, numbers, punctuation and symbols only),
but it does space the letter forms they attach to.

### 2.7 What the numbers say

1. **The joins pin the joining sidebearings.** The model's wish for the
   joining sides (32 units RMS) is not a spacing change the font can take.
   It is a statement about the drawing: this letter's body sits too close
   to, or too far from, its connection point. If the model is right about
   the bodies (not yet validated for scripts), the fix is to lengthen or
   shorten that letter's exit or entry stroke, not to move its sidebearing.
2. **Breaks are mostly per side, so they can be reported per side.** Five
   sides account for half of a family's breaks, nine for three quarters.
   "u's left side +25 breaks 15 joins" is a more useful report than 15
   separate pairs.
3. **Holding joins inside their windows leaves little of the model's
   intent.** Projected onto the windows (Dykstra's algorithm on the 26 + 26
   side changes, every join kept within half its window), the median family
   keeps 29 % of the model's side-to-side variation (IQR 20–48 %); with the
   full window, 53 %.
4. **The designers' windows are only partly additive by side.** A
   two-way fit (row + column) explains a median 74 % of the variation in the
   opening window (IQR 54–88 %). The rest is pair-level geometry, which
   sidebearings cannot reach.
5. **Height mismatches need glyphs, not numbers.** A small kern gives a
   clean touch for 226 of the 506 broken joins. The rest need large kerns
   that move the bodies, or touch at the wrong height, or need an alternate
   or a ligature.

---

## 3. Approaches

### (a) Leave joins to the designer: keep joining sides, space the rest

**What.** With Connected script on, every side that joins keeps its
sidebearing. Every pair that joins in the font (calt on, in a word) keeps
its kerning. The model spaces and kerns the rest: punctuation, figures,
capitals that do not join, and the non-join contexts of letters (letter +
period, letter + quote, letter + figure, a non-joining capital + lowercase).
The Looseness is fitted to the kept sides, so the rest of the font matches
the letters' tightness.

**Evidence.** Zero join breaks by construction, against a median 21 % of
joins broken today at the best tightness. The plugin already does this for
right-to-left letters (Arabic, Syriac…), for Devanagari, Bengali, Gurmukhi
and the other scripts in `JOINING_SCRIPTS`, and for box drawing
(`kk2_snapshot.py`, `keeps_sidebearings`: both sides fixed). The engine has
the parts:

- `SideRule::Fixed` per side.
- `GlyphOpt::frozen`: keep sidebearings and never kern a pair of two frozen
  glyphs.
- `fit_frozen`: the Looseness moved to the frozen glyphs' own tightness.
- Apply leaves frozen glyphs' sidebearings, groups and the kerning between
  them alone (`kk2_apply.py`).

**Pros.** Safe, simple, explainable. It matches how scripts are built
(connection points at the sidebearings). The model still contributes where
it is reliable: punctuation, figures, capitals and letter–punctuation
kerning are where scripts are usually weakest and least looked at.

**Cons.**

- The letters' own rhythm is not touched. For a script whose joins are
  drawn unevenly, the tool reports but does not fix.
- A brand-new script whose joins are not yet drawn to meet gets nothing for
  its letters. But then there are no joins to keep yet, and the designer has
  to set the connection points first anyway.
- A side that joins only some partners needs per-pair handling: keep the
  kerning of exactly the pairs that join.

**Effort.** 1–2 days: per-side Fixed rules from the join bands, frozen-pair
kerning kept for join pairs, `fit_frozen` on, UI note, self-test update.

**Risk.** Low.

### (b) Joins as constraints: hold each join within a tolerance

**What.**

- Measure each join's window on the font (exact geometry; section 2.1).
- Let Pass 1 propose side changes, then project them onto
  {lo(a, b) ≤ Δrsb(a) + Δlsb(b) + Δkern(a, b) ≤ hi(a, b)} for every join.
  Use a small QP or Dykstra over 52 sides for a–z, a few hundred with
  alternates.
- Kern the pairs that remain infeasible, or report them.

**Evidence.**

- The windows are small (median 22 opening).
- With every join kept inside half its window, the median family keeps
  29 % of the model's side variation; with the full window, 53 %.
- Full windows are not a safe target. A join at the edge of its window
  touches by a hair (fragile at small sizes and after hinting), and an
  angled join closed to its limit dangles.
- The windows are only 74 % additive, so some pairs always need kerning.

**Pros.** Joins provably kept, and some of the model's rhythm survives.

**Cons.**

- Most of the model's intent is lost anyway.
- Complex: the windows need exact geometry, the solve interacts with
  classes, the Looseness fit and Pass 2, and calt alternates and connectors
  need the same treatment.
- The tolerance is a judgement call.

**Effort.** 1–2 weeks with evaluation.

**Risk.** Medium. Worth trying only after (a) and (c) exist, as an opt-in.

### (c) Pair-level detection and reporting: the join checker

**What.** For every pair of letters, shaped as the reader sees it, decide
whether it joins. If it does not, record the gap and decide between a break
(both forms have join strokes) and a pen lift (calt chose a form without
one). Record the window, crossings, and the contact height against the
letter's usual join height. Do this for the font as it is and for any
proposed spacing (ΔS against the windows, no new geometry needed). Group
the findings by side and give proof strings.

**Evidence.**

- Window predictions match the geometry for 99.7 % of Dancing Script's
  joins.
- The designers' own fonts have 506 broken joins, 1,055 fragile ones and
  1,426 crossings.
- The model's breaks are explained by a handful of sides.
- The current self-test's box test misses 78 % of breaks (127 of 163).

**Pros.** No risk to the font. Useful with any spacing mode, including
fully manual spacing. It is the only way to make "we don't get ugly pairs"
checkable. It works for every build: default forms, ligatures, calt
alternates and connectors.

**Cons.**

- calt needs shaping, which Spacing QA already does with harfrust. The
  plugin needs a route; see section 4.1.
- "Ugly" beyond broken, crossed and wrong-height is taste.

**Effort.**

- Spacing QA: 3–4 days, plus a day to review the library run.
- Plugin: 3–4 days (engine export plus pane), plus 1–2 for calt.

**Risk.** Low.

### (d) Kerning only where needed, or none

**What.** Two different things:

1. Suggest a kern for the font's own broken joins.
2. Let the model move joining sides and kern every join back.

**Evidence.**

- (1): 226 of 506 broken joins touch cleanly with a kern of 10 units or
  less at the right height. Most others need 10–38 units, which also moves
  the bodies, or an alternate.
- (2): with the pair-change spread at 33 against windows of 22, nearly
  every join would need a kern. That is about 650 pairs for a–z, times the
  accented forms, unless kerned by class. Class kerning only works because
  it undoes the side changes exactly. It is approach (a) with extra steps
  and more ways to go wrong (a missing class member breaks a join).

**Recommendation.** (1) yes, as a suggestion inside the checker, with the
contact height shown. (2) no.

### (e) Contextual alternates suggestions

**What.** From the checker's contact heights, sort exits into classes (low,
high, very high: f and t crossbars) and do the same for entries. List the
combinations that break or touch at the wrong height. Name the letters that
need a high-entry alternate after the high-exit class, and the high exits
that need a low-exit alternate before certain entries. Optionally write a
calt skeleton:

    @exit_high = [b o v w];
    lookup high_entry {
        sub @exit_high [r s e a]' by [r.hi s.hi e.hi a.hi];
    } high_entry;

**Evidence.**

- High exits are v, f, o, w, b, then t, d, s, r.
- Broken joins follow w, o, v, c.
- The fonts that fix them use exactly these combinations:
  - Dancing Script: o_r, v_r, b_r, w_r.
  - Great Vibes: o_b, o_r, o_x.
  - Birthstone: b_r, b_s, b_v, b_w, o_r, o_v.
  - Kristi: alternates after f, o, t, v and w.
- Playwrite's 13 × 29 exit/entry classes are the complete version.

**Pros.** It is the real cure for height mismatches, and the data comes free
with the checker.

**Cons.** The designer still draws every alternate. Writing feature code
risks clashing with existing features. Class design (how many entry heights)
is a design decision.

**Effort.** Report-only suggestions: 1–2 days on top of the checker. Code
generation: 3–5 days.

**Risk.** Low (report), medium (writing features).

### (f) Measuring along the slant

**Evidence (section 2.5).** Slant changes the overall tightness and the
model's view of the bodies, not the joins. Breaks at the best tightness are
14–28 % against 16–34 % without it, and Dancing Script goes from 19 % to
21 %. Shape error improves for Dancing Script (42.4 → 32.5) and gets worse
for Great Vibes (87.4 → 90.8) and Allura (76.8 → 86.2).

**Recommendation.** Keep it off for joins. Under (a) it only affects
non-join contexts. Re-evaluate it there and for the drawing advice (the
body white of join pairs), where measuring a 20° script upright is
questionable.

### (g) What others do (literature and tools)

LITERATURE_PLACEHOLDER

### Comparison

| Approach | Joins kept | Uses the model on letters | Effort | Risk |
|---|---|---|---|---|
| (a) Keep joining sides | all, by construction | no (advice only) | 1–2 days | low |
| (b) Constraints within tolerance | all inside the windows | 29–53 % of its side variation | 1–2 weeks | medium |
| (c) Join checker | verifies any mode | n/a | 1–2 weeks across Spacing QA and plugin | low |
| (d1) Kern suggestions for broken joins | fixes about 45 % of the fonts' own breaks | n/a | inside (c) | low |
| (d2) Kern every join back | all, if complete | yes | days, fragile | medium |
| (e) calt suggestions | fixes height mismatches | n/a | 1–2 days (report) | low |
| (f) Slant | no effect on joins | changes body measure | built | n/a |
| Today's join mode | 79 % at best tightness (median family) | yes | built | — |

---

## 4. Recommendation

### 4.1 Build first: the join checker

One definition, used in both places.

- **Pairs.** Every ordered pair of joining letters: the a–z bases first,
  then accented letters (as their base unless their outlines differ at the
  join), then whatever calt produces.
- **Context.** Each pair is shaped inside a word, e.g. "n" + pair + "n",
  with the default features, so word-initial and word-final forms do not
  turn up.
- **Measures, per pair.**
  - joins / does not join;
  - gap, and whether it is a break or a pen lift (a form without a join
    stroke);
  - window [−close, +open];
  - crossings (new enclosed white, or a changed counter);
  - contact height against the median for that exit and that entry.
- **Fast form.** Scanline ink intervals at 1-unit steps per glyph (the
  engine's `Outline::scan` extended to return all intervals instead of
  extremes). The set of offsets at which two glyphs touch is the union, over
  heights and facing interval pairs, of open ranges (a.start − b.end,
  a.end − b.start). The window is the piece of that set around 0, read off
  in one pass with no bisection. Only crossings need a raster or boolean
  step.
- **Before Apply** (the font as it is): broken joins, fragile joins (window
  under 5 units), crossings, wrong-height contacts. Each comes with a
  suggested kern where a small one gives a clean touch, and the alternates
  listed where none does.
- **On the preview** (any spacing): ΔS(a, b) = Δrsb(a) + Δlsb(b) + Δkern(a, b)
  against each window. Breaks and closings beyond the window are listed by
  side ("u left +25: breaks 15 joins; t right +20: breaks 18").
- **Proofs.** Per flagged side, a string of its joins in context, e.g.
  "nu au cu iu tu zu"; per pair, "n" + pair + "n". Spacing QA draws them on
  the family page. The plugin opens them in an Edit tab.
- **What a report would say** for Dancing Script with today's join mode:

      Joins (a–z): 650. As drawn: 0 broken, 0 fragile, 3 crossings.
      Preview (Looseness −0.66): 163 joins break (25 %), 1 crossing.
        right sides: c +22 → 18 · t +20 → 18 · i +17 → 18 · z +19 → 17 · l +14 → 15
        left sides:  u +25 → 15 · h +19 → 11 · k +20 → 11 · l +21 → 11
        proof: ncun ntun niun nzun nlun · ntun nthn ntkn ntln
- **Spacing QA.**
  - A **joins kept** figure per family: the share of the font's joins that
    still join under the model at the best fit, plus crossings made.
  - A reason, `spacing/joins-broken`, listing the font's own broken joins.
  - Pairs shaped with calt in context, with harfrust, which is already a
    dependency.
- **Plugin.**
  - Contact windows from the engine through a new C export
    (`kk2_join_windows`, glyph indices and offsets in, windows and flags
    out).
  - For calt: Glyphs shapes text in the Edit view.
    `GSEditViewController.composedLayers` lists the layers after the
    features. A one-day spike should confirm it gives what we need.
    Positioned marks (Playwrite's connectors) are a later step; Spacing QA
    covers those fonts.
  - The self-test's connected stage tests ink contact instead of boxes.

### 4.2 The conservative default: "Keep joins"

Connected script on means:

1. Joining sides keep their sidebearings (`SideRule::Fixed`). A side is
   joining if it has a join band or if any pair joins through it in the
   font.
2. Pairs that join in the font keep their kerning. This is pair-level, not
   the side-level majority, which fixes the 17 % of breaks from pairs the
   engine treated as ordinary.
3. Everything else is spaced and kerned by the model as usual, with the full
   outlines. A period after an exit stroke keeps its clearance, as now.
4. The Looseness is fitted to the kept sides (`fit_frozen`).
5. For each kept side, the change the model wanted is shown as drawing
   advice. For example: "o right: −21. The body sits 21 units further from
   the next letter than the model would put it. Shorten the exit stroke by
   that much and the advance follows." This is advice, not edits.
6. The checker runs on the preview. It should report zero breaks, and
   anything else is a bug.

Keep today's body-spacing join mode as an explicit "Space joined letters"
option. Always show the checker's count next to it ("breaks 121 joins").

### 4.3 Later

- **Detection.**
  - Shape with calt on and treat zero-width marks that overlap both
    neighbours as joins, so the 104 Playwrite families are found.
  - Read Glyphs' `exit`/`entry` anchors when present. They are exact join
    points, which nothing has to learn.
  - Make join pairs pair-level everywhere.
- **calt suggestions** from the checker's classes (section 3e). Report
  first, code later.
- **Tolerance mode** (section 3b) as an opt-in experiment. Mask the join
  stroke (the ink past the body) rather than a band of heights, so bodies
  cannot collide.
- **Connector advice** in Spacing QA's handwriting table: the learner can
  learn how designers set body white at joins, from the 85 families. Its
  output belongs in the drawing advice, not in the sidebearings.

### 4.4 How to verify

- **Families.**
  - The 85 joining scripts, split in two halves by designer for anything
    that is tuned.
  - The 16 Playwrite families checked here, then all 52.
  - The eight text faces of the 8 October evaluation as controls (no joins,
    nothing may change).
- **Metrics.**
  1. Joins kept: the share of the font's joins (calt on, in a word) that
     still join after Apply. Keep joins must reach 100 % in every family.
     Today's mode is about 79 % at the median.
  2. Crossings made: 0 for Keep joins.
  3. Non-join evenness: Spacing QA's shape error on pairs with at least one
     non-joining side. It must not get worse than today's join mode. This
     is where the model still works.
  4. Checker precision: look at 50 random flagged font breaks and 50
     flagged model breaks. Expect at least 90 % real (the ones looked at
     here were: Great Vibes "bn", Birthstone Bounce "or", Comforter "oa",
     Dancing Script "tu" and "or", Alex Brush "ba" and "ad").
  5. Checker recall: shift sides by window + 2 units and confirm every
     shifted join is found.
  6. Window accuracy: windows against bisection on the real outlines. Expect
     at least 99 % (99.7 % here).
- **In Glyphs.** The self-test connected stage on copies of Dancing Script,
  Great Vibes and Pacifico: Keep joins Apply, checker says 0 breaks, Revert
  exact.

### 4.5 What stays manual

- The joins themselves: connection heights and angles, stroke lengths, and
  therefore the rhythm of joined letters. The tool can say where the body
  white is uneven. Redrawing the stroke is the designer's job.
- Which combinations get alternates or ligatures, drawing them, and the
  calt that picks them. The tool can list them and propose classes.
- Pen lifts: which letters do not join, and where. Examples are x and z in
  some hands, and descenders in school models.
- Kerning of the few broken pairs (the tool suggests values), initial and
  final forms, and swashes.

### 4.6 Effort

| Work | Days |
|---|---:|
| Spacing QA join checker, report fields, site section | 3–4 |
| Library run and review of flagged pairs | 1 |
| Keep joins in the plugin (fixed sides, kept join kerning, `fit_frozen`, advice, self-test with ink contact) | 2–3 |
| Engine export for contact windows, plugin checker pane and proof tab | 3–4 |
| calt in the plugin checker (Edit view spike, then build) | 1–3 |
| Pair-level and calt-on detection | 1–2 |
| calt suggestions (report only) | 1–2 |
| Tolerance mode (later, opt-in) | 5–10 |

---

## Appendix: how this was measured

The scripts ran in the session's scratchpad (temporary). They are short and
rebuild the numbers from Spacing QA's data:

- `pairs.py`: per family and ordered a–z pair: shaping (calt off, calt on),
  join, gap, opening and closing windows, crossings, contact height, and the
  model's ΔS and its join.
- `summarize.py`, `extra.py`, `tables.py`: family figures, windows projected
  with Dykstra's algorithm, greedy side freezing, two-way additivity.
- `kernfix.py`, `context.py`, `subst.py`, `scan_features.py`: kern
  suggestions for broken joins, calt in a word context, calt/liga counts,
  feature use across the library.
- 13 `spacingqa check` runs: Dancing Script, Great Vibes, Sacramento,
  Pacifico, Allura and Alex Brush with `--no-joins` and with `--slant`
  (`--category Handwriting --no-instances --no-baseline`), plus a default
  run of Dancing Script that matched the stored report exactly.

Caveats:

- The model is Spacing QA's, at the best fit with the harness. The plugin at
  another Looseness moves every ΔS by about the same amount, which the "best
  tightness" column already takes out.
- Joins were judged on a–z only. Accented letters inherit their bases' joins
  in most of these fonts, but not always.
- The Playwrite model figures assume alternates move like their base
  letters.
- Rendering (seams where strokes only touch, hinting at small sizes) was not
  measured. Fragile joins are where it would show.
- "Pairs the engine treats as joins" were inferred from the reports: a pair
  the model kerns is not a join pair (join pairs get 0). The detector was
  not re-run pair by pair.
- calt results for the 85 families come from shaping each pair as a
  two-letter word. That brings in word-final forms in a few fonts (WindSong,
  Momo Signature, Pacifico). The word-context check (`context.py`) was run
  on the ten families with calt and is the one quoted in section 1.6.
