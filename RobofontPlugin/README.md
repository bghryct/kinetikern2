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
- Connected script (joins learned from the font's own spacing);
- Spacing Groups (frozen glyphs, glyphs spaced looser or tighter, with
  more or less kerning) and its window;
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
- **Looking at a pair** opens it in a Space Center (Glyphs: an Edit tab).
- The window's settings are kept in RoboFont's extension defaults; the
  spacing groups in the font's lib
  (`com.mirkovelimirovic.Kinetikern2.spacingGroups`), saved with the UFO.

## Build

```bash
./build.sh                  # the engine (universal) and the extension
./build.sh --native         # the engine for this Mac only (RoboFont 4.4 is an Intel app: it needs x86_64)
./build.sh --no-engine      # keep the engine already in the extension
./build.sh --engine DIR     # build the engine from DIR (default: ../engine)
./build.sh --test           # the headless tests too
./build.sh --install        # link the extension into RoboFont's plugins folder
./build.sh --verify [font]  # the self-test in a temporary second RoboFont (with --groups, --connected;
                            # --profile: what ran on the main thread during Apply and Revert)
```

A universal build needs rustup's Rust with both macOS targets
(`rustup target add x86_64-apple-darwin`). `source/lib` holds the
extension's modules; `build.sh` assembles `Kinetikern2.roboFontExt` from
them (its `lib` is a build output: edit `source/lib`).

The engine must have the features the extension uses: an engine without
the connected-script mode, the designer harness or spacing groups works,
and the window says which of them it lacks.

## Modules

The modules that know nothing of the app are the Glyphs plugin's, nearly
unchanged: `kk2_bridge` (the engine), `kk2_harness` (and its table,
`kk2_harness.json`), `kk2_groups`, `kk2_proof` (the proof panes) and the
Pairs, Harness and Spacing Groups windows. What is RoboFont's:

| Module | |
|---|---|
| `kk2_host` | the fonts that are open, names, preferences, messages, Space Center |
| `kk2_snapshot` | a UFO read into engine input: outlines decomposed with a pen, ink measured exactly from the points (curve extremes solved only where a curve reaches past its on-curve points), categories, groups, composites, metrics keys |
| `kk2_apply` | plan, Apply and Revert on the UFO (defcon underneath, notifications held glyph by glyph and posted in batches, kerning written in one batch, Revert exact to the last bit) |
| `kk2_window` | the main window, with the Font popup |
| `kk2_selftest` | the in-RoboFont self-test |
| `kk2_menu`, `kk2_startup` | the menu item; the startup hook that starts the self-test in a test instance only |

## Tests

`tests/test_headless.py` runs everything but the windows outside RoboFont
(python3 with fontParts, defcon and fontTools; the extension's engine),
on a synthetic font and on any fonts given:

```bash
python3 tests/test_headless.py Lato-Regular.ttf GreatVibes-Regular.ttf
```

It checks what the snapshot works out (categories, groups, aligned
composites, metrics keys, sample-text escapes), then on each font solves
the whole font with and without the designer harness, applies, reads back
(every glyph where the result puts it, every composite only moved as a
whole, accents moving with their base) and reverts (every outline,
component, anchor, advance, kerning pair and group exactly as before, to
the last bit and the number type: a .glif writes 600 and 600.0
differently); spacing groups (frozen glyphs keep everything) and a Revert
that keeps a change made after Apply. And the ink measure Apply and Revert
use against the pen: on odd shapes (mirrored, rotated, scaled and nested
components, a contour of off-curve points only, curves reaching past their
points) and on every glyph of the fonts given.

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
composites, TrueType curves): **PASSED**. The whole font in 179 s under
Rosetta (143 s in Glyphs, natively); Apply 6.6 s (29,772 kerning entries,
3,672 sidebearings, 0 mismatches), Revert 9.2 s, every outline coordinate of
the 1,838 spacing glyphs exactly as before; the designer harness exact; the
connected-script stage: 1,613 of 1,625 letters join, no join pair kerned, a
period after a joining letter keeps its distance, Apply as previewed, Revert
exact. Longest stall: 419 ms (Revert redrawing RoboFont's font overview).
Before the batching and the faster ink measure the same Apply took 32.7 s and
Revert 21.1 s, with stalls up to 0.8 s.

Headless (`tests/test_headless.py`), Apply, read-back and exact Revert with
and without the designer harness on: the synthetic test font; the UFOs of
Owners XXWide Medium, Friendship Upright, MirandaHand, Superpolenta Book
Extended (UFO 2) and Amsterdam; Lato and Great Vibes (TrueType, their GPOS
kerning as UFO groups and pairs). A UFO whose glyphs have neither code points
nor names that say what they are (a scan's `cluster006.alt04`) has nothing
to space: the window says so.

`build.sh --verify` runs the self-test inside RoboFont, in a temporary
second instance started with its parameters on the command line (nothing is
written to your preferences; your own RoboFont is never touched): it opens a
copy of the font, chooses Extensions ▸ Kinetikern2… as a click does, waits
for the preview, cancels a whole-font run, runs one, applies it, reads the
font back, reverts and compares, then the designer harness (and with
`--groups` the spacing groups and the Pairs window, with `--connected` a
connected script), measuring the longest main-thread stall throughout.
