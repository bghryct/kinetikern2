# The join checker and Keep joins

9 October 2026. Built from the recommendation of
`connected-scripts-deep-dive.md` (section 4): a join checker first, and
"Keep joins" as the default of the Connected script setting. Numbers are in
units per 1000 em.

## What was built

**Contact geometry** (`engine/src/contact.rs`). Each glyph's ink as
nonzero-rule intervals on scanlines one unit apart, at the same absolute
heights for every glyph. Moving the second glyph right by `dx`, an interval
[b0, b1] meets [a0, a1] of the first when `dx` lies in
[a0 − b1, a1 − b0], so the set of offsets at which two glyphs touch is the
union of those ranges, read off in one pass:

- a pair **joins** when its offset lies in the set;
- its **window** is the piece of the set around the offset: the room to close
  and to open;
- a pair that does not join has a **gap** and a **fix** (the kerning change
  that joins it with 5 units of room to open where the contact allows). The
  fix is flagged when the strokes would cross there
  (`JOINPAIR_FIX_CROSSES`: kerned to join, Aguafina Script's l before b, h,
  k and l closes the white between the two ascenders, Birthstone Bounce's w
  crosses v), and the plugins and Spacing QA call a fix of more than 100
  units per 1000 em too far for kerning (Beth Ellen's r enters at the top,
  so after a, t and c it sits 178–228 units away);
- the **contact height** is the mean height of the overlap;
- a **crossing** is white the pair encloses that neither letter has alone, or
  a counter of either that changes by more than 1 %.

Ink on neighbouring scanlines touches too: strokes that meet flush along a
horizontal cut (Ballet's a and m meet along y = 169, which no scanline lands
on) join. Within one unit either way counts as touching; windows come from
contact on the same scanlines, so they stay exact.

**The checker** (`engine/src/checker.rs`, `FontJoins`). Built with the
context when the plugin prepares a connected script: the ink of every glyph
with an outline, and the pairs of a letter and a basic a–z letter (either
way round, the detector's partners) that touch as the font sets them — in a
design whose glyphs touch by construction (below), of any glyph and an a–z
letter. Measuring every pair of letters took 10 s on Great Vibes' 1,630
letters; the a–z partners take a fraction of a second and still find every
side that joins.

**Keep joins.** Every side with a join band or a join in the font keeps its
sidebearing (`SideRule::Fixed`), and every pair of two kept sides keeps the
font's kerning, exactly: Pass 2 gives such pairs their own values (so classes
carry the font's class kerning) and `run.rs` `keep_joins` writes a glyph–glyph
entry wherever classes, the threshold or the budget left another value (zero
exceptions included, which Apply writes); those entries come after the
budget, as the harness's do. A glyph with a fixed advance (tabular figures,
a glyph with a width metrics key: `GLYPH_FIXED_ADVANCE`) keeps both sides
when either is kept, since the two add up to its advance. The designer
harness leaves kept sides and pairs alone. With `PARAM_FIT_FROZEN` the
Looseness is fitted to the kept sides of the lowercase base letters: Great
Vibes' swash capitals alone fit below −6, its lowercase at −0.5.
`Solution.wanted_*` / `kk2_result_wanted` give what Pass 1 wanted for each
side: the drawing advice.

**Letters that touch, and designs that touch by construction.** The
detector needs overlap. Two more rules work on contact:

- *Touching letters* (`checker::letters_touching`, `kk2_detect_contact`): a
  font is connected when at least half of the a–z touch at least half of the
  a–z set after them as the font sets them, so strokes that meet flush
  without overlapping join too. The plugins ask it when the detector finds
  no joins; Spacing QA applies the same rule to its shaped pairs.
- *Decorated designs* (`checker::DECORATED` = 0.6, `decorated_design`,
  `kk2_detect_decorated` on the glyphs alone, `kk2_join_decorated` on a
  prepared context): a line, a grid, a background or an effect through every
  glyph (underline, charted and guide-line fonts) makes the figures touch
  the letters, which a script's figures do not. When at least 60 % of the
  sides of the default figures 0–9 (at least 5 present, and at least 13 of
  the a–z) touch at least half the a–z as the font sets them, every side
  that touches an a–z letter is kept, figures and punctuation included, so
  the line stays whole. The plugins run the test when the detector finds no
  joins (an underline drawn exactly from edge to edge) and inside the
  checker otherwise. Spacing QA skips such a font (`spacing/decorated`).

**C ABI.** `kk2_prepare_start3` (letter kinds, the font's kerning,
`PREPARE_KEEP_JOINS`), `kk2_join_check`, `kk2_join_pairs` (with
`JOINPAIR_FIX_CROSSES` when the kern that would join a pair makes its
strokes cross), `kk2_join_sides`, `kk2_result_wanted`, `kk2_join_decorated`,
`kk2_detect_decorated`, `kk2_detect_contact`; `kk2_features()` 255 (32 the
checker and Keep joins, 64 the decoration test, 128 touching letters).

**Plugins** (Glyphs and RoboFont). Connected script is on by default (a font
whose letters do not join is spaced as usual) with a menu: Keep joins
(default) or Space joined letters (the 8 October body spacing). The Joins…
button carries the checker's count and opens the Joins window: findings as
drawn, what the preview does to the joins, drawing advice, proofs in an Edit
tab or the Space Center. Its findings follow Spacing QA's rule on the basic
a–z pairs as drawn (no shaping): a side joins when it joins at least half
its a–z partners; two joining sides that do not meet are broken, 3 units
per 1000 em apart or more (closer: *nearly touch: a hairline gap*), in a
script where fewer than 1 in 20 such pairs fail to meet. In a partly
connected hand (1 in 20 or more) only a pair whose sides both join at least
75 % of their partners is broken; the rest are listed as *not joined:
partly connected* (style, not flagged). A broken join shows the kern that
joins it, or *the kern that joins it makes the strokes cross*, *too far for
kerning* (over 100 units) or *no small kern joins it*; in a decorated
design, *the line does not meet*. Drawing advice is relative to the median
kept a–z side and leaves out sides within 5 units of it.

**Spacing QA.** Keep joins by default; the checker sets every a–z pair inside a
word ("n" + pair + "n", default features) with harfrust, so scripts that join
through contextual alternates and connectors (Playwrite) are seen too. A side
joins when the glyph shaping sets there joins at least half the glyphs it is
set next to, so a form set without its stroke (Pacifico's o.fina before x)
makes a pen lift, not a break. Broken joins 3 units apart or more are a WARN
(`spacing/joins-broken`); closer, the strokes nearly touch. A partly
connected hand (`joins::PARTLY`, 1 in 20) flags only pairs of two sides that
join at least 75 % of their partners (`joins::STRONG`). Scores leave the
kept joins out; every report also tries Space joined letters and no join mode
on the same pairs.

## How it was checked

- **Against exact geometry.** The deep dive measured every a–z pair of 118
  families (those Spacing QA then reported as connected, and 16 Playwrite
  families) with skia-pathops unions and bisected windows. On the 61,054
  pairs set the same way (no contextual substitution), the checker agrees on
  99.8 % of joins; every disagreement is a gap under about two units that the
  one-unit tolerance counts as touching. The room to open agrees within a unit
  on 98.8 % of joins, the room to close within 4.5 units on 99.4 %, crossings
  on 99.9 %.
- **Engine tests** (`cargo test --release`, 9 October: 44 pass, 14 of them on
  connected scripts): touch sets and windows on boxes and a diagonal hairline
  against brute force, flush joins, crossings and counters, unions; Keep
  joins keeps every join and the font's kerning through a 10-unit threshold
  in both pair and class mode, the period keeps its clearance, the wanted
  sides are the body spacing; Space joined letters counts breaks
  consistently with the pair details; letters that meet flush join by
  touching; a line through every glyph is a decoration, not a script, and
  tabular figures keep both sides in it; a kern that would make the strokes
  cross is flagged.
- **In Glyphs** (`build.sh --verify FONT --connected`, 9 October): Great
  Vibes (the 15:41 build) and Pacifico (the 16:25 build) PASSED. Keep joins
  kept 56,355 of 56,355 joins (Great Vibes) and 44,642 of 44,642 (Pacifico),
  the Looseness matched −0.54 and +0.21; the sample's pairs of two kept sides
  kept the font's kerning (1,444 and 1,406); a period after a joining letter
  kept its distance; after Apply the master was read again and every join
  between the sample's letters still touched (999 and 1,170 joins); Revert
  exact. Space joined letters broke 26,531 and 5,436. Arial with `--groups`
  PASSED (15:45; the Spacing Groups stage 21 of 21 steps, By Category
  included). The re-runs on the evening build are pending: its Pacifico run
  found the same joins and failed only on one 740 ms main-thread pause during
  the whole-font run, under heavy machine load.
- **In RoboFont 4.4** (9 October, 15:49): the same stage on Great Vibes
  PASSED (56,207 of 56,207 joins kept, 3,140 kept sides, Looseness matched
  −0.55, 1,444 sample pairs of two kept sides with the font's kerning, 999
  joins still touching after Apply, Revert exact, Space joined letters
  27,147 broken; longest stall 228 ms, in Revert). The headless suite
  (`tests/test_headless.py`, that evening) PASSED: `test_keep_joins` on a
  synthetic script, 676 of 676 joins through Apply, read back by ink
  contact, c d kept at −6, Revert exact; `test_joins_window`, the Joins
  window's rows for a consistent joiner, a partly connected hand and a
  decorated design; `test_decorated`, an underline drawn edge to edge: the
  detector finds nothing, the decoration test finds it, all 37 glyphs keep
  both sides, and 1,248 of 1,248 touching pairs are kept.

## Left for later

- Calt in the plugin (it measures the glyphs as drawn; Spacing QA shapes).
- Exit and entry anchors as exact join points.
- Pair-level joins in Space joined letters.
- Punctuation and symbols measured on their own outline instead of the
  capitals' zone: an experiment on 150 text families cut the shape error from
  a median 25.2 to 22.8 (123 families better), but the designer harness was
  learned on the zone model and would have to be learned again.
