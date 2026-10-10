# Kinetikern2 for RoboFont

The Kinetikern2 spacing and kerning plugin for Glyphs 3, ported to
RoboFont 4. It runs the same engine (`../engine`, built into the extension
as a universal library for Apple silicon and Intel) and has the same
window, the same controls and the same tools:

- the live proof panes (the font as it is, and Kinetikern2's spacing and
  kerning of the sample text), the Looseness, SDF kerning intensity and
  ignore-threshold sliders, Threads, Max pairs, Replace existing kerning;
- Apply to the glyphs of the sample text or to the whole font, with
  progress and Cancel, never stalling RoboFont, and Revert Last Apply;
- the designer harness and its window, with its Conventions (text faces,
  display, handwriting);
- Connected script (joins learned from the font's own spacing: letters
  that overlap, or that touch without overlapping), on by default, with
  **Keep joins** (every joining side keeps its sidebearing, a glyph with a
  fixed advance both of them when either is kept, and every pair of two
  joining sides keeps the font's kerning: every join stays as drawn, the
  rest is spaced around the letters) or **Space joined letters**. A design
  whose glyphs touch by construction (a line, a grid or an effect through
  every glyph: underline, charted and guide-line fonts) keeps every side
  that touches, figures and punctuation included;
- the **Joins…** window: the join checker's findings as drawn, judged as
  Spacing QA judges them (broken joins with the kerning that would join
  them, strokes that nearly touch, a partly connected hand's other
  non-joins listed as style, fragile and crossing joins; in a decorated
  design, where the line does not meet), what the preview does to the
  joins, drawing advice for the kept sides, proofs in the Space Center;
- Spacing Groups (frozen glyphs, glyphs spaced looser or tighter, with
  more or less kerning) and its window, with **By Category**: figures,
  punctuation, symbols and the letters of each script but Latin in groups
  of their own, each with its own Looseness and kerning force (Latin
  letters keep the main sliders);
- italics measured along their italic angle (**Along the 12° italic angle**:
  the engine sees the outlines sheared upright about half the x-height);
- the Pairs window (the font's pairs, loosest to tightest, against
  Kinetikern2);
- an unattended self-test inside RoboFont (`build.sh --verify`).

## Install

Double-click `Kinetikern2.roboFontExt` (RoboFont asks to install it), then
choose **Extensions ▸ Kinetikern2…** with a font open. RoboFont 4.0 or later;
the extension carries its own engine, nothing else to install.

For development, `./build.sh --install` links the extension into RoboFont's
plugins folder instead of copying it. RoboFont loads extensions when it
starts: restart it to load the extension (or a new build of it).

## How it differs from Glyphs

RoboFont works on UFOs, so the port does what Glyphs does with the UFO's
own means:

- **Fonts, not masters.** A UFO is one master. The window's **Font** popup
  lists the open fonts; Kinetikern2 reads, previews and applies to the one
  chosen. For a family, choose each master in turn (each UFO has its own
  groups and kerning).
- **Kerning groups** are the UFO's: a glyph's left side is its
  `public.kern2` group, its right side its `public.kern1` group. New groups
  are named after the glyph they grew from (`public.kern1.T`), with `.kk2`
  added when that name is taken. Kerning pairs are written with the UFO's
  keys and precedence (glyph–glyph, glyph–group, group–glyph, group–group).
- **Glyph categories.** Glyphs looks a glyph's category up in its glyph
  database; a UFO has none. The port takes it from the glyph's code point
  (or, for an unencoded glyph, from the name it extends: `a.sc` → a,
  `f_f_i` → f, `Jacute.sc` → Jacute, a composite's first component) with
  Glyphs' own exceptions to the Unicode categories (spacing accents are
  marks, % & @ § ¶ are symbols, the fraction slash is a figure). On the
  encoded Latin, Greek, Cyrillic, punctuation and symbol glyphs of Glyphs'
  glyph database it gives Glyphs' category for more than 99.5 %. A font's
  `public.openTypeCategories` marks are marks.
- **Metrics keys** are honoured where a UFO carries Glyphs' (exported by
  glyphsLib: `com.schriftgestaltung.Glyphs.glyph.leftMetricsKey` and the
  like); RoboFont does not evaluate them, so Apply writes the sides they
  give.
- **Composites.** An accented glyph built the way Glyphs aligns one (its
  base at the origin, marks on it, the base's advance) follows its base, as
  an aligned composite does in Glyphs: Apply puts it where Glyphs'
  alignment would. Every other composite is spaced as one shape and stays
  rigid: when a base glyph moves, the components drawing it move back, so
  nothing slides apart. A ligature built from components (`f_i`) is spaced
  as one shape too; its components keep the places the designer gave them
  (Glyphs would align them).
- **Undo.** RoboFont's undo is per glyph and does not cover kerning and
  groups, so it does not cover an Apply either: **Revert Last Apply** puts
  back everything an Apply changed, every coordinate to the last bit (a
  move there and back is not always exact in floating point, 509.521 + 3 − 3
  being 509.52099999999996, which a .glif would keep: Revert writes the
  saved values back instead). It asks first when something was changed
  since, and can keep those changes.
- **Redrawing.** RoboFont redraws its whole font overview whenever glyphs
  change, drawing the cells of the changed glyphs anew. Apply and Revert
  hold the notifications of the glyphs they edit and post them in batches
  sized from what the previous batches cost RoboFont (6 to 60 glyphs, about
  0.15 s of redrawing each, at least every half second), so RoboFont
  redraws a few times a second instead of after every slice of work.
- **Italics.** A font that leans by its italic angle (`italicAngle` in the
  UFO's info, 3° or more, under 60°) is measured along it, as in Glyphs:
  the engine sees the outlines sheared upright about half the x-height, and
  its sidebearings come back in the font's frame (sidebearings and kerning
  are horizontal offsets, which a shear keeps). Apply, Revert and the
  proofs work on the outlines as drawn. The switch is in the progress row,
  disabled for an upright font.
- **Looking at a pair** opens it in a Space Center (Glyphs: an Edit tab).
- The window's settings are kept in RoboFont's extension defaults; the
  spacing groups in the font's lib
  (`com.mirkovelimirovic.Kinetikern2.spacingGroups`), saved with the UFO.

## Build

```bash
./build.sh                  # the engine (universal) and the extension
./build.sh --native         # the engine for this Mac's architecture only (see below)
./build.sh --no-engine      # keep the engine already in the extension
./build.sh --engine DIR     # build the engine from DIR (default: ../engine)
./build.sh --test           # the headless tests too
./build.sh --install        # link the extension into RoboFont's plugins folder
./build.sh --verify [font]  # the self-test in a temporary second RoboFont (with --groups, --connected;
                            # --profile: what ran on the main thread during Apply and Revert)
```

A universal build needs rustup's Rust with both macOS targets
(`rustup target add x86_64-apple-darwin`). On Apple silicon `--native`
builds arm64 only, which RoboFont 4.4 cannot load: it is an Intel app and
runs under Rosetta. Use the default universal build for it. `source/lib`
holds the extension's modules; `build.sh` assembles
`Kinetikern2.roboFontExt` from them (its `lib` is a build output: edit
`source/lib`).

An older engine without the connected-script mode, the designer harness
or spacing groups still works; the extension leaves out what it lacks. The
harness's label then says *unavailable: rebuild the extension (build.sh)*;
for Connected script the same note shows only in the tooltip of the Joins…
button, and the checkbox is disabled; spacing groups are not applied.

## Modules

The modules that know nothing of the app are the Glyphs plugin's, nearly
unchanged: `kk2_bridge` (the engine), `kk2_harness` (and its table,
`kk2_harness.json`), `kk2_groups`, `kk2_proof` (the proof panes) and the
Pairs, Harness and Spacing Groups windows. `kk2_joins_window` (the Joins
window) is the same file in both: a host object opens its proofs, the Space
Center here and an Edit tab in Glyphs. What is RoboFont's:

| Module | |
|---|---|
| `kk2_host` | the fonts that are open, names, preferences, messages, Space Center |
| `kk2_args` | the parameters of unattended runs (`build.sh --verify`), read from the argument domain only; keeps the extension's folder on `sys.path` |
| `kk2_snapshot` | a UFO read into engine input: outlines decomposed with a pen, ink measured exactly from the points (curve extremes solved only where a curve reaches past its on-curve points), categories, groups, composites, metrics keys |
| `kk2_apply` | plan, Apply and Revert on the UFO (defcon underneath, notifications held glyph by glyph and posted in batches, kerning written in one batch, Revert exact to the last bit) |
| `kk2_window` | the main window, with the Font popup |
| `kk2_selftest` | the in-RoboFont self-test |
| `kk2_menu`, `kk2_startup` | the menu item; the startup hook that starts the self-test in a test instance only |

## Tests

`tests/test_headless.py` runs everything but the windows outside RoboFont
(python3 with fontParts, defcon and fontTools; the extension's engine;
glyphsLib if installed), on synthetic fonts and on any fonts given:

```bash
python3 tests/test_headless.py Lato-Regular.ttf GreatVibes-Regular.ttf
```

It checks what the snapshot works out (categories, groups, aligned
composites, metrics keys, sample-text escapes: `test_snapshot`) and, with
glyphsLib installed, the categories against Glyphs' glyph database
(`test_glyphs_categories`); the ink measure Apply and Revert use against
the pen (`test_ink_x`), on odd shapes (mirrored, rotated, scaled and nested
components, a contour of off-curve points only, curves reaching past their
points) and on every glyph of the fonts given; an italic measured along its
angle (the frame, Apply on the slanted ink, an exact Revert: `test_slant`).
On each font it solves the whole font with and without the designer
harness, applies, reads back (every glyph where the result puts it, every
composite only moved as a whole, accents moving with their base) and
reverts (every outline, component, anchor, advance, kerning pair and group
exactly as before, to the last bit and the number type: a .glif writes 600
and 600.0 differently: `apply_and_revert`). Then spacing groups (frozen
glyphs keep everything: `test_groups`), groups by category (each script but
Latin, and a Looseness on Punctuation that opens the punctuation only:
`test_by_category`), a Revert that keeps a change made after Apply
(`test_conflict`), and connected scripts: a synthetic script whose every
join Keep joins keeps through Apply, read back from the font by ink
contact, with the font's kerning of a join pair and an exact Revert
(`test_keep_joins`); the Joins window's rows with its interface stubbed (a
broken join with the kerning that joins it, a hairline gap that nearly
touches, a partly connected hand whose other non-joins are listed as style,
and where the line does not meet in a design whose glyphs touch by
construction: `test_joins_window`); an underline drawn exactly from edge to
edge, which the decoration test finds and Keep joins keeps whole, every
glyph keeping both sides (`test_decorated`); and a script whose strokes
meet exactly flush, which the touching rule finds connected
(`test_flush_joins`); and a hand that joins only in part, whose stem letters
carry exit strokes, which the test for hands that join in part finds
connected while letters that touch at the top do not (`test_partly_joins`).

## Verified (9 October 2026, RoboFont 4.4 on an Apple M1, under Rosetta)

Inside RoboFont (`./build.sh --verify FONT --groups`), on a copy of Owners
XXWide Medium (a UFO of 419 glyphs with 76 kerning groups and 850 pairs):
**PASSED** in 34 s. Extensions ▸ Kinetikern2… opened the window (and brought
the same window back when chosen again); the preview in 1.9 s; a whole-font
run cancelled at 35 % stopped in 3 ms; the whole font in 2.7 s (30,000
entries), applied in 2.1 s: read back 200 kerning entries and every group and
sidebearing, 0 mismatches; Revert (2.8 s) put back the kerning (850 pairs),
the groups and every outline coordinate of all 419 glyphs exactly. The
designer harness moved every side and pair exactly as planned (text faces and
Display conventions), applied and reverted exactly. The Spacing Groups
window, driven like a user, 19 of 19 steps; 126 frozen capitals and the
kerning between them untouched; the Pairs window measured 145,161 pairs.
Longest main-thread stall while Apply worked: 169 ms, Revert 267 ms (limit
500 ms).

On a copy of Great Vibes (`--connected`; 1,973 glyphs, 1,161 of them
composites, TrueType curves), at 15:49: **PASSED**. The whole font in
33.3 s on 7 threads; Apply 2.36 s (36,495 kerning entries, 0 mismatches),
Revert 2.51 s, every outline coordinate of the 1,838 spacing glyphs exactly
as before; the designer harness exact. The connected-script stage, with
Keep joins: 1,613 of 1,625 letters join; 56,207 of 56,207 joins kept, 3,140
kept sides, the Looseness matched to them −0.55; the 1,444 sample pairs of
two kept sides keep the font's kerning; a period after a joining letter
keeps its distance (closest: A, +12 units per 1000 em between the inks);
after Apply all 999 joins between the sample's letters still touch, read
back from the font; Revert exact; Space joined letters breaks 27,147.
Longest main-thread stall: 228 ms (Revert).

Later the same day (`--groups`): Owners again **PASSED**, its Spacing
Groups stage now with By Category (21 of 21 steps); a copy of Freight Micro
Light Italic (a UFO at −12°, measured along its angle) **PASSED**, its
Spacing Groups stage run before By Category was added (19 of 19 steps):
Apply and Revert exact, no frozen glyph moved, the Looseness fitted to its
frozen italic capitals −0.24 (−0.38 measured upright). Glyphs 3.5.1 with
the same changes: Arial and Playfair Display Italic **PASSED** (Playfair's
capitals: −6.00, the limit, measured upright; −0.06 along the angle).

The spacing zones (the engine's, from the base letters since 9 October; see
the Glyphs plugin's README): in a font with many accented letters, an f's
hook and the accents of î ï ĩ used to set those sides. Kinetikern2's a–z
pairs against the designers' (units per 1000 em, the f row: f before every
letter): Arial +34.9 → +3.6, Georgia +61.6 → +19.5, Verdana +47.4 → +15.7;
with italics measured along the angle too, Playfair Display Italic +109 → +7,
Source Serif 4 Italic +106 → +12, EB Garamond Italic +22 → −3.

Headless (`tests/test_headless.py`), Apply, read-back and exact Revert with
and without the designer harness on: the synthetic test font; the UFOs of
Owners XXWide Medium, Friendship Upright, MirandaHand, Superpolenta Book
Extended (UFO 2) and Amsterdam; Lato and Great Vibes (TrueType, their GPOS
kerning as UFO groups and pairs). A UFO whose glyphs have neither code points
nor names that say what they are (a scan's `cluster006.alt04`) has nothing
to space: the window says so. In the evening the suite PASSED on its
synthetic fonts, connected scripts included: `test_keep_joins` (676 of 676
joins through Apply, read back by ink contact; the join pair c d kept at
−6; Revert exact), `test_joins_window` (a consistent joiner, a partly
connected hand, a decorated design), `test_decorated` (an underline drawn
edge to edge: the detector finds nothing, the decoration test finds it, all
37 glyphs keep both sides, 1,248 of 1,248 touching pairs kept) and
`test_flush_joins` (26 of 26 letters join by touching, 676 of 676 joins
kept), with `test_by_category`, `test_conflict` and, with glyphsLib,
`test_glyphs_categories` (5,320 of 5,323 categories as Glyphs has them).

`build.sh --verify` runs the self-test inside RoboFont, in a temporary
second instance started with its parameters on the command line (nothing is
written to your preferences; your own RoboFont is never touched): it opens a
copy of the font, chooses Extensions ▸ Kinetikern2… as a click does, waits
for the preview, cancels a whole-font run, runs one, applies it, reads the
font back, reverts and compares, then the designer harness (and with
`--groups` the spacing groups and the Pairs window, with `--connected` a
connected script), measuring the longest main-thread stall throughout.
