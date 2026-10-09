# Kinetikern2

A Glyphs 3 plugin that spaces and kerns a whole font with a multi-threaded
Rust engine. It is the rebuild of *Kinetic SDF Kerning* (v1, in the folder
above this one) and keeps v1's model unchanged:

* **Pass 1, macro rhythm:** every glyph's sidebearings come from a kinetic
  equilibrium over all glyph triplets: spring tension against bounding
  repulsion, with margin white measured by DMAT disk packing.
* **Pass 2, micro SDF kerning:** every pair relaxes in a signed-distance
  contour field sampled by adaptive rays. The pair-specific part becomes
  kerning, held back by collision and crevice floors.

What is new is everything around the model:

* **Whole-font runs** are background jobs with a progress bar and Cancel.
  Glyphs never waits on the engine.
* **Class kerning:** the output is class pairs plus exceptions, sized by an
  ignore threshold and a pair budget, instead of millions of glyph pairs.
* **Apply** writes in slices, with undo registration off, and keeps its own
  **Revert Last Apply**.
* **Live preview:** the sample text stays live while all of this happens.

[AUDIT.md](AUDIT.md) explains why: it measures where v1 spent its time and
what each change bought.

**RoboFont:** [RobofontPlugin](RobofontPlugin/README.md) is a port to
RoboFont 4 with the same engine, window and tools.

All lengths below are in **units per 1000 em** unless a font's own units are
named. Arial has 2048 UPM, so its default threshold of 5/1000 em is 10.24
font units.

```
Kinetikern2/
├── build.sh                          build, test, install, verify (below)
├── engine/                           Rust crate (cdylib) → libkinetikern2.dylib
│   └── src/
│       ├── lib.rs                    C ABI: #[repr(C)] structs, kk2_* functions
│       ├── job.rs                    asynchronous jobs, progress atomics, cancel
│       ├── engine.rs                 Phase 1: profiles, DMAT, per-side rules; Pass 1
│       ├── run.rs                    a solve: pair scope, glyph pairs or classes, budget
│       ├── classes.rs                side classes, member differences
│       ├── pass2.rs                  the pair kernel and the window solver
│       └── geometry.rs, profile.rs,  v1's model (adaptive SDF profiles, DMAT,
│           dmat.rs, physics.rs       Pass 1 / Pass 2 physics)
├── plugin/Kinetikern2.glyphsPlugin/Contents/Resources/
│   ├── plugin.py                     GeneralPlugin: Filter ▸ Kinetikern2…, test hooks
│   ├── kk2_window.py                 the window; one timer drives everything
│   ├── kk2_snapshot.py               reads a master into engine input, in slices
│   ├── kk2_proof.py                  the two TextKit proofing panes
│   ├── kk2_apply.py                  plan, apply and revert, in slices
│   ├── kk2_bridge.py                 ctypes mirror of the C ABI (no Glyphs imports)
│   ├── kk2_args.py                   launch arguments of unattended test runs
│   ├── kk2_selftest.py               the in-Glyphs self-test (build.sh --verify)
│   └── libkinetikern2.dylib          built by build.sh
├── tools/                            equivalence gates, benchmark, timings, UI test
└── results/benchmark.json            the 73-font benchmark
```

## Install

Requirements:
- macOS 11 or later.
- Glyphs 3, with the *Python* and *Vanilla* modules from Plugin Manager ▸ Modules.
- Rust 1.74 or later, for building the engine.

```bash
cd Kinetikern2
./build.sh --test --install
```

Then restart Glyphs, open a font and choose **Filter ▸ Kinetikern2…**.
Kinetikern2 can be installed next to v1: every module, Objective-C class and
preference key has a name of its own.

| `build.sh` option | What it does |
|---|---|
| *(none)* | Builds the native library (arm64 on Apple Silicon) and puts it into the bundle. |
| `--test` | Runs the engine's unit tests first (`cargo test --release`). |
| `--universal` | Builds arm64 + x86_64. Needs `rustup target add x86_64-apple-darwin`. |
| `--install` | Links the bundle into `~/Library/Application Support/Glyphs 3/Plugins/`. |
| `--verify [font]` | Runs the in-Glyphs self-test in a temporary second Glyphs 3 (default font: Arial). |

The library is replaced **atomically**: the script builds into a staging file
of its own, sets the install name to `@rpath/libkinetikern2.dylib`, signs it
ad hoc and renames it over the old one. A Glyphs or a tool that still has the
old library loaded keeps the old file, and nothing ever loads a half-written
one. An identical build reports `unchanged` and leaves the file alone.

`--install` refuses to replace a real folder in the Plugins folder (an
installed copy rather than a link). Move that copy to the Trash and run it
again. `--verify` refuses unless the Plugins entry is a link to this bundle,
so the test never runs an old copy.

`--verify` never touches a Glyphs you have open. It starts its own instance
(`open -n`) with the test's parameters on the command line, in the argument
domain, so nothing is written to your preferences. It waits up to 10 minutes
for `selftest.json`, prints the summary, and exits non-zero unless the test
passed. The test quits its instance when it is done.

## Using it

Open a font and choose **Filter ▸ Kinetikern2…**. Choosing it again brings
that font's window to the front.

The window reads the selected master in slices, runs Phase 1 in the
background, and then shows two panes over the sample text:
- **Left:** the font as it is, with its own spacing and kerning.
- **Right:** Kinetikern2's live result.

The sample text takes several paragraphs and Glyphs' `/glyphname` escapes
(`/T/o /f/parenright`). Scrolling any of the three text areas scrolls the
other two.

### Controls

| Control | What it does |
|---|---|
| **Looseness / Tightness** | The ratio of Pass 1's spring tension to its bounding repulsion, which sets the rest gap of each rhythm group. The middle position (ratio 3.86) is v1's tuned median over 30 Google Fonts families. The label shows the resulting spring, repulsion and rest gaps. |
| **SDF Kerning Intensity** | The coupling β of Pass 2's contour field, 0–200 %. 0 % switches kerning off; 100 % is v1's calibration against designers' kerning. |
| **Ignore threshold** | 0–20 units per 1000 em (slider or field; default 5). Kerning values smaller than this are left out of the preview and of Apply. It applies to the final value, *after* the engine's own calibrated dead zone (1.5 % of the UPM on the raw contrast, from v1), not instead of it. The label also gives the value in the font's units. |
| **Master** | The master that is read, solved and written. |
| **Size** | Point size of the proofs. |
| **Threads** | *Auto* uses all cores but one (7 of 8 on an M1), or pick 1 to all. Takes effect from the next job. |
| **Max pairs** | The budget of a whole-font run: at most this many kerning entries, and the least important are left out (see *Budget* below). Default 30,000; empty or 0 means no limit. The note beside it warns above 100,000 (large source files, slow exports). The sample-text preview has no budget. |
| **↻** | Reads the master again, after you edit outlines in Glyphs. |
| **Apply to** | *Glyphs in sample text* applies the preview. *Whole font* runs every glyph with progress and Cancel, then applies. |
| **Replace existing kerning** | On by default. Removes the master's existing entries between the glyphs and groups that get new kerning. Existing pairs across scripts, which the engine never evaluates, are kept. Off: existing glyph pairs stay and keep overriding new class pairs (the dialog counts them). |
| **Apply to Font** | Plans the writes, asks once in a dialog that lists what will change, then writes in slices with a progress bar. |
| **Revert Last Apply** | Puts the kerning, groups and sidebearings back as they were before the last Apply. |
| **Spacing Groups…** | Opens the group picker (see *Spacing groups* below): freeze parts of the font, or space them with their own Looseness and kerning force. |
| **Pairs…** | Opens the Pairs window (see *Pairs, loosest to tightest* below). |
| **Designer harness** | On: corrections toward what designers of well-spaced fonts do, applied after the solve, in the preview, the whole-font run and Apply (see *Designer harness* below). The slider sets the strength, 0–100 % of what the data say. Off by default; the label says what it corrects for this master. |
| **Harness…** | Opens the Designer Harness window: the pairs the harness changes most, drawn with and without it. |
| **Connected script** | For scripts whose letters join (see *Connected scripts* below): finds the joins in the master's own spacing and kerning, spaces the letter bodies without them and lets two joining letters overlap as drawn. The note beside it says how many letters join, or that none do. Off by default. |
| **Along the italic angle** | On an italic master (italic angle 3° or more), measures its spacing along the angle: see *Italics* below. On by default; the checkbox names the master's angle and is off for an upright master. |
| **Progress bar, Cancel** | Shows the current step: *Reading outlines*, *Phase 1/3: Analyzing SDFs*, *Phase 2/3: Evaluating pairs* (with seconds elapsed in a whole-font run), *Phase 3/3: Grouping & pruning*, *Planning*, *Applying*, *Reverting*. Cancel stops reading, Phase 1, a preview, a whole-font run or the planning of an Apply. Once Apply or Revert has started writing, it runs to the end. |

The status line under the panes reports the last result. For example:
- glyphs spaced and kerned;
- entries, split into class pairs and exceptions;
- how many entries the budget dropped;
- timings and the number of threads.

During a whole-font run, the controls that would restart it are disabled. The
sample text stays editable, and the panes keep rendering.

### Spacing groups

**Spacing Groups…** opens a picker that paints glyphs into colour-coded
groups. A group either has its own spacing or is frozen:

- **Space.** The group's glyphs are spaced with a **Looseness offset** from the
  main slider (−1 to +1) and kerned with a **kerning force** (0–300 % of the
  main intensity). Figures can be looser, Cyrillic a touch tighter, fractions
  kerned more gently, all in one run.
- **Freeze.** The group's glyphs keep their sidebearings and groups, and the
  kerning *between* frozen glyphs stays exactly as it is. Only the rest of the
  font is spaced and kerned, including against the frozen glyphs. Use it to
  space only the new characters of a finished font.
- **Glyphs in no group** follow the main window's settings.

The window has three parts:

- **Sections.** Lists the font's glyphs by script and case (Latin ·
  Uppercase, Cyrillic · Lowercase…), then figures (tabular, old-style,
  fractions, superiors), punctuation, symbols and marks. Click a section to
  select its glyphs, or Command-click to combine sections. There are also
  **All / None / Invert**, **Glyphs in no group** and a name filter.
- **Grid.** Every glyph of the master as a tile, like Glyphs' font view, tinted
  with its group's colour; frozen glyphs carry a snowflake. Select with click,
  Shift-click for a range, Command-click to toggle, or drag a rectangle.
- **Groups.** **New Group**, then name it, pick a colour, choose **Space** or
  **Freeze** and set its sliders. **Put Selected Glyphs into This Group**
  assigns the selection, and **Take Selected Glyphs out of Their Groups** frees
  it. Keep picking until every part of the font has the values you want.
  **By Category** does the picking for the usual cases: the glyphs in no group
  yet go into **Figures**, **Punctuation**, **Symbols** and one group for the
  letters of each script but Latin (**Cyrillic**, **Greek**…), so each kind
  has a Looseness and kerning force of its own. New groups start at the main
  settings (nothing changes until a slider moves); a group of one of those
  names that is there already is used, its settings kept; glyphs already in
  a group stay where they are. Latin letters keep the main sliders.

**Match the frozen spacing** (on by default) first fits the Looseness to the
frozen glyphs' own sidebearings, so new glyphs come out as tight or loose as
the spacing already in the font. The main slider is then an offset from that
fit, and the window shows the fitted value.

Every change previews at once. The groups are saved in the font
(`font.userData["com.mirkovelimirovic.Kinetikern2.spacingGroups"]`), so they
come back with the .glyphs file.

How the engine handles groups:

- **Pass 1:** each glyph's rest gap is shifted by its group's Looseness
  offset, half on each side. Frozen glyphs are fixed at their current
  sidebearings.
- **Pass 2:** a pair's force is the coupling times the mean of the two
  glyphs' forces. Classes never mix frozen and free glyphs, and pairs between
  two frozen glyphs or classes are not kerned.
- **Apply:** frozen glyphs keep their sidebearings and groups, and existing
  kerning between frozen keys is kept even with *Replace* on. A free glyph that
  shared a kerning group with frozen glyphs leaves it for a group of its own.
  The dialog says how many glyphs that affects.

### Pairs, loosest to tightest

**Pairs…** measures the font as it is now against Kinetikern2. For every pair
in scope (the sample text, the whole font, or one section), it compares the
white the eye sees now — right sidebearing + left sidebearing + kerning —
with the white Kinetikern2 gives it. The font's overall tightness is taken
out first, so the list shows what departs from the font's *own* rhythm:

- **Loosest first / Tightest first**, the top 50 to 500 pairs, with Δ in units per
  1000 em, the gap now and Kinetikern2's, and both kerning values.
- A preview draws the selected pair as it is and as Kinetikern2 would set it.
  Double-click a pair, or use **Open in Edit Tab**, to look at it in Glyphs.

The engine measures all pairs of a whole font in a fraction of a second; a
whole-font scope runs the whole-font job first if it has no result yet.

### Designer harness

Kinetikern2 spaces from the outlines alone. Measured against the text fonts
on Google Fonts that people rate well spaced, it does some things
consistently differently from their designers. **Designer harness** corrects
those, after the solve and by as much as the data say (strength 100 %) or
less. Without it the model is untouched.

What the data say. Spacing QA has a report for every Google Fonts family:
for every pair of the 88 scored glyphs of the GF Latin Kernel (letters,
punctuation and the symbols spaced by their shape), the designer's gap minus
the bare model's at the font's best-fit Looseness. The reference fonts are
the sans serifs and serifs rated 70 or more on google/fonts' human
`/Quality/Spacing` scale (472 families), checked at Regular and the variable
ones also at 100–900 (1,693 observations). The median over them keeps the
model's habits and averages out any one designer's taste. Units are per 1000
em, + = designers looser (third table, `2026-10-08-1b31cb2d`):

| Where | Correction | Notes |
|---|---|---|
| inside parentheses | +33 | +50 at light weights |
| inside brackets, braces | +54 to +57, +37 | +89 at light weights |
| slash, backslash, bar | +19 to +31 | |
| ? ! & % * @ | +5 to +14 | |
| quotes, period, comma, hyphen, degree | −6 to −16 | the model sets them too loose |
| en and em dashes | −24 to −28 | −35 to −39 at light weights |
| open sides of E, F, L, T | +15, +9, +6, +4 | E +32 at light weights, +21 when tight |
| diagonals of A, V, W, Y, K | −4 to −11 | |
| 1,972 pairs, beyond their two sides | | mostly with punctuation and symbols: `?.` `/,` `’/`, a period or comma before a diagonal, a raised symbol after a letter the model would tuck it under (`L°`, ``L` ``) |

On the families it was not learned from (half held out at a time) it brings
the model 19 % closer to the designers (mean gap difference 24.9 → 20.3), and
the punctuation pairs 10 units or more off from 3,010 to 57.

How it is applied (`kk2_harness.py`):
- Each core glyph side gets `a + b·L + c·w + d·L·w`. L is the Looseness
  slider (with any frozen-glyph fit). w is the weight: the log of the stem of
  `I` (else `l`) per 1000 em, over 85. Both are clamped to the range the data
  cover.
- A side that follows another glyph (a metrics key, an auto-aligned
  composite) gets that glyph's shift. Accented letters take their base
  letter's; `.case` punctuation takes its base mark's.
- Pair corrections go to the exact glyphs, as glyph–glyph kerning over
  whatever class kerning the pair has. They are fitted after the side
  corrections, on what those leave of each pair, where the median is 8 units
  or more and 60 % of the observations agree on its sign. (The first table
  fitted them before the sides with 70 % agreement. A pair the sides move a
  lot then kept no correction when designers disagree on kerning it: a period
  before v or w, which the sides tighten, came out 20–27 units tight.)
- Frozen glyphs keep their sidebearings, and two frozen glyphs their kerning.
  Other scripts, figures and symbols are left as the model spaces them.
- The engine applies all of it after the budget (`run::Harness`,
  `kk2_solve_start3`), so the preview, the whole-font run, Apply and the
  Pairs window see the same thing.

**Conventions.** Display and handwriting faces space their punctuation more
openly than text faces. The Harness window's **Conventions** menu picks whose
punctuation to follow: **Text faces** (the default), **Display** or
**Handwriting**. The last two add, on top of the text faces' corrections, what
the designers of that category do with punctuation alone: learned from the
display and handwriting families rated 70 or more (220 and 133). Their
letters keep the text faces' corrections. After a period, for example,
handwriting designers leave about 37 units more than text-face designers.

Does it help? Each table was learned on half of the families and checked on
the other half: the mean distance between Kinetikern2 and the designers, and
the punctuation pairs whose median is still 10 units or more off.

| Pairs | Without → with the harness | Closer |
|---|---|---|
| All | 18.6 → 15.6 | 16 % |
| With punctuation | 23.6 → 19.3 | 18 % |
| With a parenthesis | 30.2 → 22.0 | 27 % |
| Letters | 15.5 → 13.2 | 15 % |
| Light weights (stem under 50) | 23.9 → 19.3 | 19 % |
| Light weights, E and F before a letter | 38.0 → 22.9 | 40 % |
| Punctuation pairs ≥ 10 units tighter / looser than the designers | 475 / 287 → 0 / 0 | |
| Display: punctuation pairs ≥ 10 tighter / looser (on top of the text table) | 135 / 66 → 5 / 0 | |
| Handwriting: the same | 824 / 382 → 75 / 58 | |

(The numbers published with the first table were not truly held out: the
cross-check matched each held-out family with the wrong observation. Fixed,
the first table scores nearly the same averages; what the new pair rule
changes is the pairs that stayed systematically off.)

What is left is mostly each designer's own taste, which no correction common
to all fonts can know.

**Harness…** lists the pairs the harness changes most at the current
settings and conventions: in running text (no lowercase before a capital, no
two punctuation marks; the default), letters only, punctuation with letters,
or all pairs.
For each it shows the change and its parts: the first glyph's right side, the
second glyph's left side, a pair correction. The selected pair is drawn three
ways: as in the font, as Kinetikern2 sets it, and with the harness. The window
follows the sliders live. While it is open, the preview also kerns its pairs.

The table ships in the bundle (`kk2_harness.json`), so the plugin needs
nothing else. Learning it again takes the reports of a Spacing QA library
scan (Spacing QA is a project of its own): run `tools/kk2_harness_data.sh`
and then `tools/kk2_harness_learn.py`. The commands are at the top of the
script.

### Connected scripts

Kinetikern2 never lets two glyphs' ink overlap. Connected scripts are drawn
to overlap where the letters join. **Connected script** spaces them as joined:

- **Is the font connected?** When at least half its lowercase a–z overlap at
  least half of their a–z partners at some height, as the master is spaced
  and kerned. Text faces never are, nor are scripts and casual hands whose
  strokes reach past the advance but stop short of the next letter (spacing
  them as joined made them less even).
- **Where are the joins?** For each letter side, the band of heights where it
  overlaps most of its partners, or where its ink reaches past its advance
  (before its origin on the left). Every letter is measured against the basic
  a–z, so accented and alternate letters get their bands like their bases.
- **What changes.** Each joining side's body — the side without its join
  band — is spaced in Pass 1, so the join stroke overhangs like the hook of a
  j. A pair whose facing sides both join gets no kerning and overlaps as
  drawn. Every other pair keeps every rule: punctuation, figures, and a
  letter beside one that does not join keep their clearance (a period after
  an exit stroke stays clear of it).

The joins are found on the main thread when the setting is turned on or the
master is read (milliseconds: 69 ms for Great Vibes' 1,630 letters on a busy
machine), then Phase 1 runs again with them. The same detector decides joins
in Spacing QA, which spaces every connected family this way. The design and
its evaluation on 365 families are in `notes/connected-scripts.md`.

### Italics

Kinetikern2's model is built on upright letters: it measures the white
between letters with disks and a distance field, which a slanted design
fools, so that it looks crammed. Measured upright, an italic comes out too
loose and uneven. On a master that leans by its italic angle (3° or more),
**Along the italic angle** (on by default) gives the engine the outlines
sheared upright about half the x-height, where the eye judges a leaning
letter's sides and Glyphs measures italic sidebearings. Sidebearings and
kerning are horizontal offsets, which a shear keeps, so the results apply to
the slanted outlines as they are; Apply, Revert and the proofs work on the
outlines as drawn.

What it does, on the 171 italics of the Google Fonts library (Spacing QA,
the declared italic angle, each italic against its designer's spacing, units
per 1000 em, medians): the gaps of the GF Latin Kernel's pairs 29.8 → 21.3
from the designer's (163 of 171 closer), sidebearings 29.9 → 13.4 (164 of
171), kerning correlation 0.46 → 0.70, best-fit Looseness −0.39 → +0.05
(measured upright, every italic looked too tight to the model). Uprights of
the same families: 14.5 and 8.7. Worse: Molle and Kristi, scripts with a
declared angle, and Josefin Slab Italic (+4.5); a script can switch it off.
In Glyphs, fitting the Looseness to Playfair Display Italic's own capitals
gave −6.00 (the limit) measured upright and −0.06 along the angle.

### What Apply writes

* **Kerning:** in Glyphs' own keys, with Glyphs' precedence (glyph–glyph,
  glyph–class, class–glyph, class–class).
  * Class pairs use `@MMK_L_<group>` / `@MMK_R_<group>`.
  * Exceptions are glyph–class, class–glyph or glyph–glyph entries.
  * An exception of 0 is written as 0, because it cancels a class value.
* **Groups:** your kerning groups are kept. A glyph without a group on a side
  joins the engine's class for that side; a new group takes its key glyph's
  name, with `.kk2` added if that name is already in use. Groups belong to the
  glyph, not the master: in a multi-master font, the dialog says how many
  glyphs join existing groups and so change kerning in the other masters.
* **Sidebearings:** free sides move by whole units to the engine's values,
  worked out on the ink of the outline, as the engine measures it. Glyphs
  reports sidebearings rounded half away from zero, so a side that sits on a
  half unit (a curve extreme at −144.5 reads −145, at 6.5 reads 7) would land
  a unit off if the move were worked out from what Glyphs reports; Apply sets
  `reported + move` instead, and checks the result on the ink.
  * **Sides that Glyphs computes:** sides driven by a metrics key or an
    auto-aligned component are refreshed by Glyphs (`syncMetrics`,
    `alignComponents`). The engine solved with those same rules, so the
    kerning fits the spacing the font really gets.
  * **Keeping their advance:** tabular figures, glyphs with a width metrics
    key, and the glyphs those keys follow.
  * **Untouched:** right-to-left glyphs, glyphs of joining scripts (Mongolian,
    Devanagari, Bengali, Gurmukhi and a few others) and box-drawing
    characters keep their sidebearings.
* **Composites stay rigid:** when a base glyph's outline moves, the components
  that draw it move back by the same amount, so an accent never slides off its
  letter.
* **Sample-text scope:** Apply re-spaces the glyphs of the sample text, the
  glyphs the preview kerned, and the glyphs their metrics keys and aligned
  components follow. For example, Ñ in the sample also moves N, as the right
  pane showed.

**Undo and Revert.** Apply writes with undo registration off: 3.5 µs per
kerning entry instead of 33. It clears the undo history of the kerning and of
the glyphs it writes, and the dialog says so. **Revert Last Apply** is the
undo; it moves every outline back to exactly where it was on the ink, half-unit
sides included:
- **Changes made after the Apply:** Revert puts back only what is still as
  Apply left it. If something was changed since, it asks: *Keep Later
  Changes*, *Revert Everything* or *Cancel*.
- **Closing the window:** a window closed after an Apply hands its revert point
  to the next window opened for the same document. Closing the document
  discards it.
- **Applying again:** after an Apply or a Revert, the next Apply reads the
  master again and reruns Phase 1 first, because groups and sidebearings
  changed.

## Architecture

```
 Glyphs main thread (one NSTimer)                 libkinetikern2 (threads it owns)
 ────────────────────────────────                 ─────────────────────────────────
 SnapshotReader: master → GlyphSpecs,
   ≤ 8 ms per tick ───────── packed input ──────▶ Phase 1/3  Analyzing SDFs → Context
 poll at 15 Hz (a few atomic loads)  ◀─ progress ─ Phase 2/3  Evaluating pairs
                                                  Phase 3/3  Grouping & pruning → Result
 Result read in place (zero-copy views) ◀───────── metrics + kerning entries
 Planner / Applier / Restorer,
   ≤ 8 ms per tick ───────────────────────────────▶ GSFont (kerning, groups, metrics)
```

### Engine: jobs and phases

`kk2_prepare_start` starts **Phase 1** on the library's own thread. It does
the following:
* builds every glyph's adaptive SDF profiles and DMAT white volumes;
* records what the pair pipeline needs: script, kerning eligibility, existing
  groups, composite base, ink heights;
* resolves the per-side rules:
  * *Free*: Pass 1 decides.
  * *Fixed*: kept as it is.
  * *Follow*: the same or the opposite side of another glyph, plus an offset.
    This is how metrics keys and aligned composites reach the engine.

Phase 1 returns a `Context`. `kk2_solve_start(context, params, mask)` then
runs:
- **Pass 1:** a few milliseconds.
- **Phase 2/3**, the pair work.
- **Phase 3/3**, exception compression and the budget.

A solve returns a `Result`. The design rules for both jobs:

* **Nothing calls back into Python.** Progress is a handful of atomics that
  `kk2_job_poll` reads.
* **Cancel is fast.** `kk2_job_cancel` sets a flag that every work item checks,
  so a job stops within one pair's time.
* **Freeing never blocks.** `kk2_job_free` never waits, and the context is
  reference counted, so the window can drop a running preview at any moment.
* **Panics never reach Glyphs.** Every export catches them; failures come back
  as messages.
* **Worker priority.** The job and worker threads run at user-initiated
  priority, below the interface thread.
* **Threads.** Each thread count has its own rayon pool.
* **Results are read in place.** A result is a set of compact arrays (metrics
  per glyph, kerning entries) that `kk2_bridge.Result` reads without copying.
  `value(left, right)` and `values()` resolve glyph pairs on demand; Python
  never builds an object per pair.

### Spacing zones

Pass 1 spaces each side by the extreme ink inside its group's zone: baseline
to x-height for lowercase, to cap height for capitals, the median extents of
the group. Ink outside it, the hook of an f, the tail of a j, an accent, may
overhang the advance, as designers draw them. The median is taken over the
base letters (a–z, A–Z, a Cyrillic or Greek letter without a mark: the
plugin marks them, `GLYPH_ZONE`, engine feature 16); over every member of the
group, accented letters, which outnumber the base letters in most fonts,
lifted the lowercase zone to accent height (Arial: 1,466 against an x-height
of 1,062), and an f's hook and the accents of î ï ĩ set those sides: f was
spaced 25–60 units per 1000 em too loose (Arial fo +25.4 → −0.2, f row
+34.9 → +3.6; Georgia +61.6 → +19.5), î ï ĩ some 30 looser than i. Spacing
QA, which reads only the GF Latin Kernel, always had the right zones.

### Pair scope

A glyph is kerned when all three hold:
* it is a letter, figure, punctuation mark, or a symbol that sits in text;
* it is not right-to-left;
* it is in the job's mask (all glyphs for a whole-font run; the sample
  text's glyphs for the preview).

Arrows, box drawing, math operators and other technical symbols are spaced
but not kerned. Marks and separators are neither. Pairs are formed only
within one script, or with Common/Inherited glyphs such as punctuation and
figures. On Arial this cuts the 5,890,329 ordered pairs of 2,427 spacing
glyphs to 1,976,538.

### Classes and exceptions (`classes.rs`, `run.rs`)

A class is one side group: glyphs whose facing profile is the same, so one
value serves all of them. Classes come from, in order:
1. **Your kerning groups,** kept as they are.
2. **The composite base's group:** an ungrouped glyph joins it when its profile
   matches the base's wherever the base has ink. Á, À and Ä join A; Ą, Ľ and Ơ
   do not.
3. **A shape merge:** classes without a designer group merge when their
   representatives match within 2 units per 1000 em over the spacing zone (n
   and m on the right side, Latin H and Cyrillic Н). Ink outside the zone,
   such as the ascender of h next to n, is recorded as a difference.

For every member the class records where its rays differ from the
representative's: an accent above the cap height, a tail below the baseline,
a stem one unit wider. The representative pair of every class pair is solved
in full. Each member pair then gets one of three treatments:

* **Inherited** without evaluation, when no difference lies near the
  partner's ink and its rays and probes are identical to the
  representative's.
* **Verified** otherwise. The verify replays the solver's sign steps and
  accepts the class value only when the final bracket lies inside the target
  window, so it agrees with the solver by construction.
* **Solved** when the verify finds the pair really differs. If the result
  differs from the class value by the threshold or more, it becomes an
  exception.

Exceptions that agree across every in-scope partner in a class are compressed
into one glyph–class or class–glyph entry. On whole Arial, the 1,868 kerned
glyphs collapse to 1,157 right and 1,105 left classes.

### The window solver (`pass2.rs`)

The physics is v1's. What changed is the cost of a pair.

**Less work per force evaluation:**
* The merged ray band is built into reusable buffers.
* One-sided rays that provably contribute nothing are skipped.
* The floors are skipped when exact bounds prove they cannot bind.
* Distance queries are skipped, or reuse an earlier query's nearest chunk,
  only where that provably gives the same value.

**Fewer force evaluations per pair.** The solver decides force *signs* from
per-ray bounds, refining only the most uncertain rays:
1. **A threshold-window test.** A pair whose value lies inside the window is 0,
   usually after two cheap decisions.
2. **Doubling steps, then bisection** down to a narrow bracket.
3. **Illinois regula falsi.**

The result is 1.8–2.7 force evaluations per pair, against v1's 6.7–7.2.

**Reference mode** (`Solver::Reference`, no threshold, no scope) performs v1's
exact arithmetic, and the equivalence gate checks it bit for bit. In
multi-equilibrium pairs the window solver can choose a different root than
v1 (see *Known limitations*).

### Threshold and budget

**Threshold.** Values whose magnitude is below the threshold become 0. The
engine never goes below v1's 0.5-unit rounding.

**Budget.** It keeps the most important entries, by this measure:

    importance = |value| / (mean white between the pair + 5 % of the UPM)

* **Weighting:** class pairs are weighted by √(member pairs covered), and
  compressed exceptions by √(pairs covered).
* **Dropping:** an exception whose class pair is dropped goes with it.

On whole Arial the budget of 30,000 keeps 29,991 of 298,918 entries: 4,825
class pairs and 25,166 exceptions.

### Plugin modules

| Module | Role |
|---|---|
| `plugin.py` | Registers Filter ▸ Kinetikern2… (a GeneralPlugin) and, only when launched with test arguments, the unattended hook. |
| `kk2_window.py` | The window. One NSTimer in the common run-loop modes drives reading, job polling, Apply/Revert slices and pane layout, so it keeps working during slider drags and open menus. At most one engine job runs at a time; for a preview, the newest request wins. |
| `kk2_snapshot.py` | `SnapshotReader` copies a master in three passes of at most 8 ms per tick: list glyphs, read outlines and properties, resolve references (metrics keys, components, tabular figures). It builds the engine input as it goes. |
| `kk2_proof.py` | TextKit 1 panes whose glyphs are attachment cells. Kerning goes into each cell's width, because TextKit ignores the kerning attribute on attachments. Right-to-left runs are set right to left. |
| `kk2_apply.py` | `Planner` (what will be written), `Applier` (writes, read-back), `RevertPoint` and `Restorer`. All of them step in slices from the window's timer. `plan()`, `apply()` and `restore()` run them to the end for tools. |
| `kk2_bridge.py` | ctypes structs, with their sizes checked against the library; `Engine`, `InputPacker`, `Job`, `Context`, `Result`. It has no Glyphs imports, so the tools use it too. |
| `kk2_args.py` | Reads test parameters from the argument domain only, so a stray preference can never turn a normal launch into a test run. |
| `kk2_groups.py` | The spacing-groups model (no AppKit): groups, members, per-glyph engine options, sections, saving in `font.userData`. |
| `kk2_groups_window.py` | The Spacing Groups window: sections list, glyph grid (a custom NSView), group rows and settings. |
| `kk2_pairs_window.py` | The Pairs window: measurement through the engine, the list and the pair preview. |
| `kk2_harness.py` | The designer harness: the learned table (`kk2_harness.json`), the glyphs it applies to, the stem, and each glyph side's shift and the pair corrections for a master, Looseness and strength (`Plan`). |
| `kk2_harness_window.py` | The Designer Harness window: the pairs it changes most, drawn as in the font, as Kinetikern2 sets them, and with the harness. |
| `kk2_selftest.py` | The unattended in-Glyphs test behind `build.sh --verify`. |

## Verification

Run the tools from the `KinetiKern` folder (the parent of this one) with
Glyphs' own Python, which has PyObjC, vanilla and fontTools:

```bash
GPY="$HOME/Library/Application Support/Glyphs 3/Repositories/GlyphsPythonPlugin/Python.framework/Versions/3.11/bin/python3"
```

The figures below were measured on an Apple M1 (4 performance and 4
efficiency cores, 16 GB, macOS 14) on 6 October 2026. Gates and timings
marked *final build* were run on the installed library after the last
change. The load average was 2–6 while they ran, and a run's own threads
count towards it.

### Engine unit tests

`./build.sh --test` (or `cargo test --release` in `engine/`): **28 passed**
(final build). Two of them cover the designer harness: the exact side shifts
and pair corrections in pair and class mode, and frozen glyphs left alone.

### Equivalence gates: `tools/kk2_equivalence.py`

Each gate runs five system fonts (Arial, Times New Roman, Verdana, Georgia,
Trebuchet MS) on their first 400 spacing glyphs. The reference gate also runs
75 core glyphs. T is the threshold, 5/1000 em (10.24 units at 2048 UPM).

| Gate | What it checks | Result (final build) |
|---|---|---|
| `reference` | Kinetikern2 in reference mode against v1, every pair. | **PASS:** sidebearings and kerning identical on all five fonts, core and 400 glyphs (Arial: 45,357 of 45,357 entries equal). |
| `window` | Window solver against reference. | 1.79–2.72 force evaluations per pair against 6.66–7.22. Pairs that differ by T or more: 6 / 41 / 16 / 50 / 25 (0.004–0.031 %). |
| `classes` | Class kerning expanded to glyph pairs against glyph-pair mode. | **0 pairs differ by T or more** on all five fonts (largest difference 10.2, just under T). 89.4–97.3 % within 1 unit. |

Class compression at 400 glyphs, from the same `classes` run:

| Font | Glyph-pair entries | Class entries (class pairs + exceptions) | Classes R / L |
|---|---|---|---|
| Arial | 35,360 | 23,668 (12,022 + 11,646) | 223 / 197 |
| Times New Roman | 51,869 | 29,710 (17,532 + 12,178) | 235 / 223 |
| Verdana | 35,904 | 21,240 (13,307 + 7,933) | 213 / 196 |
| Georgia | 56,520 | 38,252 (21,804 + 16,448) | 246 / 234 |
| Trebuchet MS | 36,815 | 23,269 (12,178 + 11,091) | 208 / 203 |

### Quality: `tools/kk2_benchmark.py`

v1's 73-font benchmark scores spacing against designers' own (v1's
`tools/benchmark.py` loads and scores; this script imports it). The sets are
30 Google Fonts families for tuning, 13 for validation, and 30 macOS system
families as a held-out test. Results are in `results/benchmark.json`.

The gate compares glyph-pair mode (`pairs`: window solver, script scope)
against v1. It **passes on all three sets**: every gated median moves by
0.003 or less, and sidebearings are bit-identical in all 73 fonts.

| Set | Pair gaps, v1 → kk2 | Designer-kerned pairs, v1 → kk2 | Kern r | Sidebearings |
|---|---|---|---|---|
| Google 1–30 (tuning) | 15.7 → 15.7 | 19.3 → 19.4 | 0.71 → 0.71 | 30/30 identical |
| Google 31–43 (validation) | 17.5 → 17.5 | 21.4 → 21.4 | 0.73 → 0.73 | 13/13 identical |
| System (held-out) | 20.8 → 20.8 | 32.1 → 32.1 | 0.81 → 0.81 | 30/30 identical |

**Large differences:** of 421,648 pairs, 6 differ from v1 by 5 units or more,
all in pairs with several equilibria:
- Roboto Slab: y and v next to ?
- Georgia: T next to x
- Palatino: v and y next to 7
- Charter: v next to 7

**Reference mode** is identical to v1 on all 73 fonts.

**Threshold 5 (`pairs-t5`, `classes`):** the gated medians move by at most
0.14. Sign agreement drops 2.5–5 points only because the benchmark counts a
pair dropped below the threshold as a sign mismatch. Solve time per set falls
from 9.8 / 2.9 / 7.4 s (v1) to 2.9 / 0.9 / 2.2 s (classes).

The benchmark ran before the last two engine changes (memory accounting and
thread priority), which leave the output unchanged; the reference and class
gates above were re-run on the final build.

### Speed: `tools/kk2_speed.py` and `tools/kk2_cpu.py`

`kk2_speed.py` times a whole-font run with the plugin's defaults:
- class mode;
- threshold 5;
- budget 30,000;
- Auto threads (7).

It prints the phases as the progress bar sees them, the work done, the
entries produced and peak memory. Whole Arial (2,427 spacing glyphs from the
TTF), final build; the v1 column comes from `kk2_speed.py --v1` and a split
of the same run into engine and Python time:

| | v1 | Kinetikern2, classes (default) | Kinetikern2, glyph pairs (`--pairs`) |
|---|---|---|---|
| Pairs | 5,890,329 (every ordered pair) | 1,976,538 in scope; 511,824 class pairs | 1,976,538 in scope |
| Phase 1 | 2.31 s | 2.32–2.44 s | 2.28 s |
| Pair work | 154.7 s (151.6 s engine, 2.0 s decoding the result into Python) | 15.5–15.7 s | 9.6 s |
| **Total** | **157.0 s** | **18.0–18.1 s** (3 runs) | **12.0 s** |
| CPU time of the pair work | 1,019 s | 49.5–50.1 s | 62.5 s |
| Output | 2,055,690 non-zero glyph pairs | 29,991 entries (298,918 before the budget) | 30,000 entries (528,464 before the budget) |
| Peak memory of the process | 1,053 MB | 225–226 MB | 193 MB |

`kk2_cpu.py` gives CPU microseconds per pair on 400-glyph sets, which holds
up better than wall time on a busy machine (final build). For comparison, the
earlier measurement of v1-style solving on the Arial set was 189 µs.

| | Reference mode | Window solver |
|---|---|---|
| Arial | 119.1 µs | 31.5 µs |
| Georgia | 236.7 µs | 91.3 µs |

### The window outside Glyphs: `tools/kk2_ui_smoke_test.py`

This test opens the real window invisibly against a mock of the Glyphs API
built from a TTF: two masters, groups, metrics keys, aligned composites,
existing kerning. It drives the window through its controls the way a user
does:
* sliced reading, previews, slider changes during a running preview;
* sample and whole-font Apply, Cancel at 30 %, Revert;
* closing and reopening the window.

Throughout, a 60 Hz heartbeat measures main-thread stalls. Result: **92
passed, 0 failed** on 400 glyphs and on 3,000 glyphs (`--glyphs 3000
--max-pairs 30000`).

### Inside Glyphs: `./build.sh --verify`

The self-test runs in a temporary Glyphs 3.5.1 on Arial (2,674 spacing glyphs
in Glyphs' import):
1. Opens the font and chooses Filter ▸ Kinetikern2… through the real menu,
   then chooses it again: the same window must come to the front.
2. Waits for the preview.
3. Cancels a whole-font run at about 30 % and restarts it.
4. Applies the whole-font result and reads every planned sidebearing and group
   back, plus 200 kerning entries. It also compares every spacing glyph's ink
   sidebearings with the result.
5. Reverts, and compares the master's whole kerning table, groups and
   sidebearings with the state before Apply.
6. Tests the designer harness, when the engine build has it, the way a user
   works with it:
   - opens the Designer Harness window;
   - turns the harness on with its checkbox, and checks that the preview
     moved every glyph side and every listed pair by exactly what was
     planned;
   - picks the Letters filter;
   - picks the Display conventions in the window's menu, checks that the
     preview moved every glyph side by exactly the Display plan, and picks
     Text faces again;
   - applies the preview, reads back every glyph the harness shifted, and
     reverts;
   - turns the harness off with the main window's switch, and checks that
     every glyph is back to Kinetikern2's own spacing.
   It saves `harness.png` and `harness-letters.png`.
7. Saves `window.png` and `selftest.json`, then quits.

`./build.sh --verify FONT --connected` adds a connected-script stage after
the harness (FONT must be a connected script; an OFL script from Google
Fonts such as Great Vibes): it turns **Connected script** on as a user does,
waits for Phase 1 and the preview, and checks that joins were found, that
the join pairs of the sample text are not kerned and that most overlap, and
that a period after a joining letter keeps its distance (the narrowest white
between the inks, from the engine's own profiles). It applies the preview
and checks the font against it, reverts, and turns the setting off: every
glyph must be back to the spacing without joins. It saves `connected.png`.
On Great Vibes: 1,615 of 1,630 letters join; 1,368 join pairs, none kerned.

`./build.sh --verify [font] --groups` adds a spacing-groups stage after
those:

1. Sets up the groups in the Spacing Groups window the way a user does. It
   uses the grid with real mouse events (click, Shift-click, Command-click, a
   dragged rectangle), All / None / Invert and the name filter. It picks the
   "Latin · Uppercase" section from the list, then uses New Group, the name
   field and Freeze. Next it picks "Figures" and sets Looseness +0.4 and 50 %
   force with the sliders. Finally it checks "Glyphs in no group", takes a
   glyph out of its group and puts it back. Each step is checked, and so is
   what the engine receives.
2. Opens the Spacing Groups and Pairs windows and runs the whole font.
3. Checks the result: the Looseness fitted to the capitals, frozen glyphs at
   their own sidebearings, and no entries between frozen glyphs.
4. Measures the font as it is in the Pairs window and saves `pairs.png`.
5. Applies, and checks that the frozen glyphs, their groups and the kerning
   between them did not change. Measures again: the Pairs window must read
   the font again first, and then find it within 8 units per 1000 em of the
   model on average. Saves `groups.png`.
6. Reverts, and checks that the font is as before.

Results (Glyphs 3.5.1, M1, 8 October 2026): **PASSED** on Lato and on Arial.
Lato was run last, on the final plugin (one label of the harness window was
reworded after it).
The reports are in `notes/verification-2026-10-08/`: Lato with its window,
groups and pairs images, and Arial as text, so its outlines are not
published.

| Font | Groups window, driven like a user | Frozen / spaced | Whole-font run | Frozen glyphs and kerning between them after Apply | Pairs measured | Longest main-thread stall |
|---|---|---|---|---|---|---|
| Lato (245 glyphs) | 19 of 19 steps | 68 capitals / 10 figures | 0.4 s, Looseness fitted −0.00 | unchanged | 52,789 pairs in 0.9 s; after Apply, mean difference 1.0 | 120 ms (the harness stage) |
| Arial (2,674 glyphs) | 19 of 19 steps | 354 capitals / 26 figures | 14.0 s, Looseness fitted −0.17 | unchanged | 2,336,295 pairs in 19.8 s, off the main thread; after Apply, mean difference 0.1 | 228 ms |

The designer harness stage. Lato ran last, with the second table (416 pairs,
the Display and Handwriting conventions); Arial ran with the first (294
pairs):

| Font | Corrected | Preview against the plan | Display conventions | Apply | Revert, then off |
|---|---|---|---|---|---|
| Lato | 128 glyph sides (stem 97), 416 pairs | every side and every listed pair exact | 554 pairs; every side exact (period −6/−3, hyphen −7/−4, question +14/+8) | 47 shifted glyphs written as previewed (E +2/+14, F +2/+9, A −11/−10) | font as before; every glyph back to the model |
| Arial | 680 glyph sides (stem 95; accented letters and keyed sides follow), 294 pairs | every side and every listed pair exact | (not in that build) | 51 shifted glyphs written as previewed (E +2/+15) | font as before; every glyph back to the model |

The Designer Harness window opened in about 120 ms on both. With the second
table its list leads with F/ +55, E) +47, L) +46, h) +46 in running text, and
with AA, YA, Yq, Fz among letters (`lato-harness.png`,
`lato-harness-letters.png`).

**The third table and connected scripts** (8 October, evening; Glyphs 3.5.1,
M1). Lato with `--groups` and the third table, learned on the GF Latin
Kernel: **PASSED**. The harness corrected 149 glyph sides and 1,925 pairs,
every one as planned (the window leads with L• +77, L° +67, T• +67, L% +66);
the Display conventions 2,506 pairs; Apply wrote the 48 shifted glyphs as
previewed and Revert put the font back; the spacing-groups stage passed all
19 steps; the longest main-thread stall was 124 ms (`lato-summary-kernel.txt`,
`lato-selftest-kernel.json`, `lato-kernel-harness.png`). Great Vibes (OFL,
1,839 spacing glyphs) with `--connected`: **PASSED**. The connected-script
stage: 1,615 of 1,630 letters join, found in 63 ms; 1,368 join pairs in the
sample, none kerned, 761 overlapping as drawn; a period after a joining
letter keeps at least 20 units per 1000 em of white between the inks (Y);
Apply as previewed, Revert exact, off restores the spacing without joins
exactly. The harness stage corrected 607 sides and 2,005 pairs exactly as
planned. The whole-font Apply read back 0 mismatches and Revert put all
1,839 glyphs back exactly, after a fix this font prompted: its half-unit
sides (curve extremes on half units, as Great Vibes' caron puts ccaron's
right side at −144.5) read a unit off in Glyphs, which rounds them half away
from zero, so Apply's read-back and Revert now work on the ink
(`greatvibes-connected-summary.txt`, `greatvibes-connected.png`).

A 60 Hz heartbeat measures how long the main thread is busy at a time.
Result on the final build: **PASSED** in 33.2 s.

| Step | Measured |
|---|---|
| Preview ready | 4.9 s after opening: outlines read in 1,125 ms of main-thread time, Phase 1 2.6 s |
| Cancel at Phase 2, 30 % | stopped in 2.5 ms; the preview result was kept |
| Whole-font run | 17.4 s on 7 threads: 1,951 kerned glyphs, 2,336,295 pairs in scope, 29,988 entries (332,560 before the budget) |
| Apply | 2.42 s in slices: 29,988 kerning entries, 3,398 group sides, 4,002 sidebearings. 0 read-back mismatches; all 2,674 glyphs' ink sidebearings match the result. |
| Revert | 2.07 s in slices: kerning (908 entries), groups and sidebearings of all 2,674 glyphs exactly as before |
| Longest main-thread stall while the window works | 67.6 ms during Apply: one full Python garbage collection (60.3 ms) over the roughly 354,000 objects in Glyphs' shared interpreter. Other stages: preview 38.1, whole-font run 18.5, Revert 33.6 ms. |
| Not judged | Opening the window 341 ms (building the vanilla window); closing 114 ms |

## Known limitations

* **Right-to-left scripts are not kerned yet.** Hebrew, Arabic and other
  right-to-left glyphs are left out of kerning, and their sidebearings are
  kept. The same goes for joining scripts and box drawing. v1 kerned
  right-to-left glyphs in left-to-right order, which is wrong for them;
  leaving them alone is the safe choice until there is a right-to-left pair
  order. The proofs do set right-to-left text right to left.
* **Connected scripts need some spacing first.** The joins are learned from
  the master's own spacing and kerning: a script whose letters do not yet
  overlap where they join (a font just drawn, all sidebearings at their
  defaults) has none to find, and is spaced as usual. Space a few letters so
  that they join, then turn **Connected script** on. A script that declares
  an italic angle is measured along it (*Italics*); one that declares none is
  measured as drawn (measuring along a slant measured from the stems, an
  experiment in Spacing QA, `spacingqa check --slant`, gave mixed results on
  joined scripts).
* **Multiple equilibria.** Some pairs have more than one equilibrium gap. The
  window solver picks its root by force signs; v1 searched from the Pass 1
  gap. Most of the time they agree, but on 0.004–0.031 % of pairs the values
  differ by the threshold or more, sometimes by a lot (Georgia T next to x:
  −28.2 against v1's −42.7). Reference mode reproduces v1 exactly if you need
  it (tools only).
* **Whole-font time.** Whole Arial takes about 18 s in the default class mode
  and 12 s in glyph-pair mode. The planned 7–13 s is met only by glyph-pair
  mode, which produces 528,464 entries before the budget, against 298,918.
  Class mode does less work in total (64 CPU-seconds against 78), but it is
  not spread evenly across threads.
  * **The tail:** Phase 2 runs on about 6.8 cores until it is 92 % done, then
    on a single core for its last 9 s. One thread is left finishing a run of
    expensive class pairs, whose member pairs run one after another
    (`run_classes`, member step).
  * **The likely gain:** spreading that work over all cores would bring class
    mode to roughly 10 s on this machine. That is an estimate, not a
    measurement.
* **The budget keeps mostly exceptions.** On whole Arial it keeps 25,166
  exceptions and only 4,825 class pairs. Many exceptions belong to class pairs
  whose own value is below the threshold. The importance ranking deserves a
  review on fonts with designer groups.
* **Apply and Revert are not instant.** On Arial they take about 2 s each in
  Glyphs, in slices with progress. Glyphs' own sidebearing setter accounts
  for most of it, and Cancel cannot stop them once writing has started.
* **Undo.** Apply is not on Glyphs' undo stack; Revert Last Apply is. A revert
  point does not survive closing the document.
* **Other masters.** Joining an existing kerning group changes that glyph's
  kerning in every master, because groups belong to the glyph. The dialog
  says so, but cannot keep the other masters' old values.
* **Long sample texts.** The panes' layout costs main-thread time: an
  all-glyph sample of 3,000 glyphs stalled for 156 ms once in the smoke test.
* **Not yet tested in Glyphs:** auto-aligned composites. Arial's imported
  composites never report `isAligned`, so this path is tested on mocks only.
  Try it on a `.glyphs` source with aligned composites.
