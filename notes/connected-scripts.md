# Connected scripts: design notes

Status (8 October 2026): the join mode is built in the engine, on by default
in Spacing QA, and in the plugin as the **Connected script** setting (see
"The rule, final" below). Measuring along the slant is built in Spacing QA
but off by default (`spacingqa check --slant`).

## The problem

Kinetikern2 never lets two glyphs' ink overlap, and it measures the white
between letters as if they were upright. Connected scripts break both:
their letters join (exit and entry strokes that overlap the neighbour) and
they lean 10–30°. Spacing QA sees it as the best-fit Looseness stopping at
the limit (−6): 168 Latin families are "out of range" today.

## What the out-of-range families are

Measured on Spacing QA's reports (designer spacing, lowercase pairs):

| Kind | Families | What is going on |
|---|---:|---|
| Joining scripts | 65 (56 Handwriting, 8 Display, 1 Sans) | Letters overlap at joins as designed (median closest approach −56 per 1000 em). Leave the joins out and the letter bodies are spaced like letters (+2 to +34). |
| Slanted, barely joining | many of the other 95 (Allison, Carattere, Calligraffitti …) | Closest approach +19 to +49: they hardly overlap. They lean 10–26° with tiny x-heights, and the model, measuring as if upright, wants far more room. |
| Shapes meant to overlap | 8 (Flow, Honk, Rubik Beastly, Micro 5 Charted …) | Bodies overlap even without joins: not spacing in the usual sense. Should stay out of range. |

## Joins: learned from the font's own spacing

A join is not an extreme point of the outline (exit heights vary a lot in a
font) nor simply a stroke sticking out of the body (that also catches
serifs). What works: for each glyph side, the height bands where its ink
overlaps its neighbours' in at least half of the pairs, as the designer
spaced them.

| Font | Bodies, join bands left out | Overlap at the joins | Join band (of the height) | Letters that join |
|---|---:|---:|---|---:|
| Mr Dafoe | +26 | −47 | 26 % right, 32 % left | 65 % |
| Great Vibes | +11 | −15 | 16 %, 17 % | 90 % |
| Sacramento | +10 | −35 | 20 %, 18 % | 100 % |
| Allura | +25 | −19 | 10 %, 15 % | 70 % |
| Dancing Script | +9 | −18 | 17 %, 18 % | 95 % |
| Alex Brush | +31 | −17 | 9 %, 13 % | 60 % |
| Lato, Merriweather | — | — | 0 % | 0 % |

The overlap at a font's joins is consistent: that is the "smart measure" of
how much overlap to allow — the font's own. Text faces get no join bands, so
the mode cannot change them by accident.

## The slant

A shear keeps horizontal distances at every height, but not the distances
the model measures (DMAT disks, the signed distance field), so a slanted
design looks crammed to it. Sheared upright by the slant measured from its
stems (l, i, h), and checked again:

| Font | Slant | Best-fit Looseness | Shape error |
|---|---:|---|---|
| Carattere | 21° | −6.00 → −1.43 | 70.7 → 71.6 |
| Calligraffitti | 10° | −6.00 → −2.34 | 82.5 → 74.6 |
| Allison | 26° | −6.00 → −3.84 | 69.9 → 70.1 |
| Dancing Script | 11° | −1.82 → −0.84 | 43.9 → 36.9 |
| Beau Rivage, Bonheur Royale, Allura, Great Vibes | 16–24° | −6.00 → −6.00 | (they join: need the join part) |

## Measuring along the slant, built: what it does across the library

Spacing QA's `crate::slant` measures a design's slant as the median slope of
its stems (l i h n m u r k b p, the centre of the first ink run between a
quarter and three quarters of the x-height; near-straight stems only, within
2 % of the em) and shears the outlines upright when they agree (interquartile
range up to 0.25) on 3° or more. Upright faces come out bit for bit as
before. Checked on the 168 out-of-range families and every handwriting
family:

- **27 out-of-range families come back in range by the slant alone**: Allison
  (26°, Looseness −3.6), Pinyon Script (34°), Qwitcher Grypen, Playball,
  Damion, Smooch, Petemoss, Satisfy, Praise, Parisienne, Carattere…
- **In-range handwriting, measured along its slant (116): mixed.** Median
  shape error 42.4 → 39.8, but only 47 % more even. The Playwrite families
  split: AU VIC, AU TAS, NZ, AU SA/NSW/QLD much better (about 60–70 → 20–28),
  FR Trad, ID, BE WAL, CL, VN, BR much worse (about 45–50 → 80–96). All are
  joined cursives from one foundry: sheared upright, their joining strokes
  meet differently, and with no join handling that can go either way.

So the slant waits for the joins: the two are evaluated together, and only
then turned on (`AnalyzeOptions::slant`, and `METHOD` in report.rs so a stale
scan checks the families again).

## The engine: where the joins go

Mapped from the engine's code (8 October):

- The Looseness fit compares Pass 1's sidebearings with the font's
  (`Context::fit_looseness`, clamped at ±6). A join that exists only in Pass 2
  never moves the fit: it has to exist in Pass 1.
- Pass 1 spaces each side's **zone extreme** (the extreme ink within the
  glyph's group zone) and its margin white Ω. Ink outside the zone is
  **overhang** (like the hook of a j) and may pass the advance. Leaving a join
  band out of the side's zone extreme and Ω turns the join stroke into
  overhang: Pass 1 then spaces the body and the stroke overlaps the neighbour
  by itself.
- Pass 2's no-touch rules: the field's facing rays (an overlap always
  repels), the hard core of one-sided rays, the clearance floor (0.01 em,
  every height), the crevice floor (a stroke's tip pressing into the
  partner's entry), and kerning capped at −0.10 em. For a pair whose facing
  sides **both** join, the pair uses the profiles with the join bands masked
  (gap rays), so the field, the bounds and both floors see only the bodies;
  every other pair keeps the full outlines — a letter next to a period keeps
  clearance from the exit stroke.
- Classes: a joining side and a non-joining side must not share a class.
- Input: per glyph side a join band (y range), prepared once (zone extremes,
  Ω, pieces, disks, ink ranges, windows and probes all depend on it). The C
  ABI's glyph record is frozen: a new export with a per-side array and
  `struct_size`, announced by a feature bit, as the harness did.

## The join mode, built (stage 1): results

The engine (`GlyphInput::join_left/right`, 8 October): a joining side's body
is its profile with the join band left out (`SdfProfile::masked`); Pass 1
spaces the body (zone extreme and margin white), so the join stroke
overhangs like the hook of a j; a pair whose facing sides both join gets no
field kerning and no floors (`PAIR_JOIN`), its joins overlap as drawn; every
other pair keeps the full outlines and every floor; joining and non-joining
sides never share a class (`PART_JOIN`). Without join bands every result is
bit for bit as before (the engine's tests, and Spacing QA's reports of Lato,
Merriweather, Great Vibes and Dancing Script, pair by pair). The engine test
`a_connected_script_overlaps_at_its_joins` checks a small script in both
modes: strokes overhang, letters overlap at their joins with no kerning, a
period keeps its clearance on either side.

Spacing QA's `crate::joins` finds the joins (an experiment: `spacingqa check
--joins`). On 365 families (every handwriting family, every out-of-range one,
eight text faces), with the first detector (heights where at least half the
pairs overlap):

| | Connected found | Out of range → in range | In range before and after |
|---|---|---|---|
| joins | 91 (74 handwriting) | 14 of 70 (shape error 57.3 → 52.6) | 21: 53.3 → 48.1, **100 % more even** |
| joins + slant | 91 | 29 of 70 (59.1 → 56.7) | 21: 53.3 → 49.1, 81 % more even |

Every other family, the text faces among them, is untouched.

## The mode (plan)

A **Connected script** setting in the plugin (and on automatically in Spacing
QA when a family's joins are found):

1. **Measure along the slant.** The engine works on the outlines sheared
   upright by the font's slant (the master's italic angle, or measured from
   its stems). Sidebearings and kerning are horizontal offsets, which a shear
   keeps, so the results apply to the real outlines unchanged.
2. **Find the joins** from the font's own spacing: per glyph side, the height
   bands where it overlaps most neighbours. For a font with no spacing yet,
   from the outlines: near-horizontal strokes that reach the side.
3. **Space the bodies.** For a pair whose facing sides both have joins, the
   join bands are left out of the no-overlap rule and of the white the model
   balances: the letter bodies are spaced by the model as usual.
4. **Keep the joins joined.** In the join band the gap is held at the font's
   own overlap (its median), within a tolerance, so a join neither breaks
   (too loose) nor doubles up (too tight).
5. **Everything else as today.** Punctuation, figures, capitals that do not
   join, and any pair where one side does not join keep the normal rules and
   the designer harness (Handwriting conventions).

Then: Spacing QA measures the joining families instead of reporting them out
of range, and the learner can give handwriting a proper table of its own.

## To verify

- The 65 joining families come back in range; their shape errors are
  comparable with in-range handwriting.
- Text faces are unchanged bit for bit (no joins found, no slant).
- Held out: well-rated scripts, the mode against the designers, half the
  families learned from, half checked.
- In Glyphs: the self-test on an OFL connected script (Dancing Script),
  with the joins drawn in the preview.

## The rule, final (8 October)

Three detectors were compared on the same 365 families (every handwriting
family, every out-of-range one, eight text faces), each with the engine's
join mode:

| Detector | Connected | Out of range → in range | In range before and after | Less even |
|---|---:|---|---|---:|
| v1: heights where most pairs overlap | 91 | 14 of 70 | 21: 53.3 → 48.1 | 0 |
| v2: those, or ink past the advance | 178 | 66 of 121 | 57: 48.7 → 42.3 | 10 |
| v3: ink past the advance only | 173 | 63 of 116 | 57: 48.7 → 43.2 | 10 |

Every one of v2's ten less even families was found by the overhang signal
alone (Caveat 34.8 → 52.7, Edu AU VIC WA NT Arrows 37.0 → 52.6, Playwrite AU
SA 58.8 → 70.3, Kaushan Script 39.7 → 49.0, …): casual hands and scripts whose
strokes reach past the advance but stop short of the next letter — their
median closest approach between letters is +13 to +148 per 1000 em. They are
spaced, not joined. So the final rule splits the two questions:

- **Is the family connected?** When at least half its lowercase letters
  overlap at least half their a–z partners at some height, as spaced and
  kerned (v1's test).
- **Where are the joins?** For each letter side, the heights where it
  overlaps most partners or where its ink reaches past its advance (before
  its origin on the left) (v2's bands).

On the 91 connected families that gives 40 of 70 out-of-range families in
range (v1's bands: 14) and 20 of 21 in-range families more even (median
53.3 → 41.8; Meie Script unchanged at 187.0 → 187.6). Text faces are
untouched.

Every letter is measured against the basic a–z as partners, on both sides
(`JoinKind::Lower` = a–z, every other letter `Upper`): accented and alternate
letters get their bands like their bases, and the detection takes
milliseconds even for a font of 800 letters (Great Vibes, 1,014 glyphs: 55 ms
under load; it was 340 ms with every lowercase letter as a partner).

## Built

- Engine: `joins.rs` (the detector, `api::detect_joins`), the join mode
  (`GlyphInput::join_left/right`), and the C ABI: `kk2_detect_joins`,
  `kk2_prepare_start2` with `KK2Joins` (32 bytes, NaN = none), feature bit 8.
- Spacing QA: `crates/spacingqa/src/joins.rs` wraps the detector; on by
  default (`check --no-joins`), reports record `join_sides` and say so
  (`spacing/joins`).
- Plugin: **Connected script** checkbox in the main window. On, it finds the
  joins in the master's own spacing and kerning (on the main thread, in
  milliseconds), runs Phase 1 again with them and previews; the note beside
  it says how many letters join ("no joins: the letters do not overlap" for a
  font that does not). The self-test's connected stage
  (`build.sh --verify FONT --connected`): joins found, join pairs unkerned and
  overlapping, a period's distance from an exit stroke kept, Apply as
  previewed, Revert, and off again restores the spacing without joins exactly.
