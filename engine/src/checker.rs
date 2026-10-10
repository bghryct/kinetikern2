//! The join checker of a connected script: which pairs of glyphs join in
//! the font (exact ink contact, crate::contact), how much room each join has,
//! and what a spacing does to them.
//!
//! Two ways to space a connected script share it:
//!
//! * **Keep joins** (the default of the Connected script setting): every
//!   joining side keeps its sidebearing, and a pair of two joining sides keeps
//!   the font's kerning, so every join stays as drawn. A side joins when the
//!   detector gave it a band (crate::joins) or when any pair joins through it
//!   in the font: a side with joins but no band gets the band of its contact
//!   heights. Everything else (punctuation, figures, capitals that do not join,
//!   a letter next to a period) is spaced and kerned by the model as usual —
//!   except in a design whose glyphs touch by construction (`DECORATED`),
//!   where every side that touches an a–z letter keeps its sidebearing, and
//!   a glyph whose advance is kept keeps both sides when either is kept.
//! * **Space joined letters**: the bodies are spaced and two joining sides are
//!   not kerned (crate::joins); the checker counts the joins that breaks.
//!
//! Every glyph with an outline is measured. The pairs that can join are
//! every ordered pair of a letter and a basic a–z letter (either way round;
//! the detector's partners) whose boxes meet as the font sets them — in a
//! decorated design, of any glyph and a basic a–z letter; a pair joins when
//! the inks touch.

use rayon::prelude::*;

use crate::contact::{self, InkRows};
use crate::engine::GlyphInput;
use crate::job::{Cancelled, Progress};
use crate::joins::JoinKind;
use crate::measure::{CurrentKerning, KernIn};

/// What `Context::prepare_with_joins` needs besides the glyphs.
#[derive(Clone, Debug)]
pub struct JoinSetup {
    /// What each glyph is to the joins, one per glyph: letters join (the
    /// basic a–z are the partners); other glyphs only in a decorated design.
    pub kinds: Vec<JoinKind>,
    /// The font's kerning now (class sides: the glyphs' group ids).
    pub kerning: Vec<KernIn>,
    /// Keep joins (else: space joined letters).
    pub keep: bool,
}

/// A pair of glyphs (a letter, or in a decorated design any glyph, and a
/// basic a–z letter) whose inks touch as the font sets them.
#[derive(Clone, Copy, Debug)]
pub struct FontJoin {
    pub left: u32,
    pub right: u32,
    /// Where the right glyph's origin sits from the left one's (advance plus kerning).
    pub offset: f64,
    /// The heights the inks touch between.
    pub y0: f32,
    pub y1: f32,
}

/// One side's breaks under a spacing.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct SideBreaks {
    pub glyph: u32,
    pub right: bool,
    /// Joins through this side that no longer touch.
    pub breaks: u32,
    /// How far the spacing moved the side (+ away from the neighbour).
    pub delta: f64,
}

/// What a spacing does to the font's joins.
#[derive(Clone, Debug, Default)]
pub struct JoinCheck {
    /// Pairs that join in the font.
    pub joins: usize,
    /// Of them, still joined / no longer touching.
    pub kept: usize,
    pub broken: usize,
    /// Of them, set at another offset (|change| ≥ 0.5 units).
    pub moved: usize,
    /// Sides with breaks, most first.
    pub sides: Vec<SideBreaks>,
    /// The basic a–z (`JoinKind::Lower`): joins in the font, joins broken,
    /// crossings in the font and crossings the spacing makes.
    pub az_joins: usize,
    pub az_broken: usize,
    pub az_crossings_drawn: usize,
    pub az_crossings_made: usize,
}

/// One pair in detail.
#[derive(Clone, Copy, Debug, Default)]
pub struct PairCheck {
    pub left: u32,
    pub right: u32,
    /// As the font sets it.
    pub drawn: contact::Contact,
    /// Mean contact height (NaN when it does not join).
    pub height: f64,
    pub crossing_drawn: bool,
    /// The spacing's change of the pair's offset, and the pair after it.
    pub delta: f64,
    pub joins_after: bool,
    pub crossing_after: bool,
    /// Not joined: the kerning change that joins it (`drawn.fix`) makes the
    /// strokes cross or close a counter, so no kerning joins it cleanly (a
    /// longer stroke or an alternate does).
    pub fix_crosses: bool,
}

/// A connected script's letters for the checker, built with the context.
pub struct FontJoins {
    pub keep: bool,
    /// Scanline spacing and the room below which a join is fragile (font units).
    pub step: f64,
    pub fragile: f64,
    pub kinds: Vec<JoinKind>,
    pub ink: Vec<Option<InkRows>>,
    /// Counters of each letter (for crossings).
    pub holes: Vec<Vec<f64>>,
    kerning: CurrentKerning,
    pub right_group: Vec<u32>,
    pub left_group: Vec<u32>,
    pub advance: Vec<f64>,
    pub cur_lsb: Vec<f64>,
    pub cur_rsb: Vec<f64>,
    /// The pairs that join in the font, by left glyph then right glyph.
    pub pairs: Vec<FontJoin>,
    /// Sides with a join in the font (left, right).
    pub joined: Vec<(bool, bool)>,
    /// The glyphs touch by construction (`decorated`): a line, a grid or a
    /// background runs through every glyph, figures included. Every side
    /// that touches an a–z letter then has a join, letter or not.
    pub decorated: bool,
}

/// Padding of a band made from contact heights (as crate::joins pads its own).
const PAD: f64 = 0.02;
/// A join with less room than this (units per 1000 em) to open is fragile.
pub const FRAGILE: f64 = 5.0;
/// Enclosed white smaller than this many scanline cells is not a crossing
/// (where two inks only touch, the scanlines leave slivers of a cell).
pub const MIN_CROSSING_CELLS: f64 = 4.0;
/// The glyphs touch by construction when at least this share of the
/// figures' sides touch at least half the a–z as the font sets them: a
/// script's figures stand apart from its letters, while an underline, a
/// chart or guide lines run through them too (Spacing QA's `joins::decorated`
/// is the same rule).
pub const DECORATED: f64 = 0.6;

/// Figures and a–z letters a decoration test needs at the least.
const DECORATED_MIN_FIGURES: usize = 5;
const DECORATED_MIN_AZ: usize = 13;

/// The decoration test (`DECORATED`): at least `DECORATED` of the figures'
/// sides touch at least half the a–z letters as the font sets them (offset
/// = advance + the font's kerning; touching within one scanline). `ink`
/// holds every glyph of `figures` and `az`.
fn figures_touch(
    ink: &[Option<InkRows>],
    advance: &[f64],
    kern: &(dyn Fn(usize, usize) -> f64 + Sync),
    step: f64,
    figures: &[usize],
    az: &[usize],
) -> bool {
    if figures.len() < DECORATED_MIN_FIGURES || az.len() < DECORATED_MIN_AZ {
        return false;
    }
    let touches = |a: usize, b: usize| {
        let offset = advance[a] + kern(a, b);
        offset.is_finite() && contact::touch_heights(ink[a].as_ref().unwrap(), ink[b].as_ref().unwrap(), offset, step).is_some()
    };
    let half = az.len() as f64 / 2.0;
    let sides: usize = figures
        .par_iter()
        .map(|&f| {
            let right = az.iter().filter(|&&b| touches(f, b)).count() as f64 >= half;
            let left = az.iter().filter(|&&a| touches(a, f)).count() as f64 >= half;
            right as usize + left as usize
        })
        .sum();
    sides as f64 >= DECORATED * (2 * figures.len()) as f64
}

/// The figures the decoration test reads: the default figures 0–9 when the
/// caller marks them (`GLYPH_FIGURE`), else every glyph of `GROUP_FIGURES`.
fn figure_glyphs(inputs: &[GlyphInput]) -> Vec<usize> {
    let marked: Vec<usize> = (0..inputs.len()).filter(|&i| inputs[i].flags & crate::engine::GLYPH_FIGURE != 0).collect();
    if !marked.is_empty() {
        return marked;
    }
    (0..inputs.len()).filter(|&i| inputs[i].group == crate::api::GROUP_FIGURES).collect()
}

/// The decoration test on the glyphs alone, without the checker: for a font
/// whose letters the detector finds unjoined because nothing overlaps (an
/// underline or a chart drawn exactly from edge to edge touches without
/// overlapping). The same rule as `FontJoins::build` applies: figures are
/// `GROUP_FIGURES`, a–z `JoinKind::Lower`.
pub fn decorated_design(inputs: &[GlyphInput], upm: f64, kinds: &[JoinKind], kerning: &[KernIn]) -> bool {
    let n = inputs.len();
    let inked = |i: usize| !inputs[i].contours.is_empty() && inputs[i].advance.is_finite();
    let az: Vec<usize> = (0..n).filter(|&i| kinds.get(i) == Some(&JoinKind::Lower) && inked(i)).collect();
    let figures: Vec<usize> = figure_glyphs(inputs).into_iter().filter(|&i| inked(i)).collect();
    if figures.len() < DECORATED_MIN_FIGURES || az.len() < DECORATED_MIN_AZ {
        return false;
    }
    let step = contact::row_step(upm);
    let mut ink: Vec<Option<InkRows>> = vec![None; n];
    for &i in figures.iter().chain(&az) {
        ink[i] = Some(InkRows::of_contours(&inputs[i].contours, step, 0.0, 0.0)).filter(|r| !r.is_empty());
    }
    let figures: Vec<usize> = figures.into_iter().filter(|&i| ink[i].is_some()).collect();
    let az: Vec<usize> = az.into_iter().filter(|&i| ink[i].is_some()).collect();
    let advance: Vec<f64> = inputs.iter().map(|g| g.advance).collect();
    let current = CurrentKerning::new(kerning);
    let kern = |a: usize, b: usize| {
        let v = current.value_in(a as u32, b as u32, inputs[a].right_group, inputs[b].left_group);
        if v.is_finite() {
            v
        } else {
            0.0
        }
    };
    figures_touch(&ink, &advance, &kern, step, &figures, &az)
}

/// Letters that join by touching, as the font sets them: of the basic a–z
/// (`JoinKind::Lower`), (how many have a right side that touches at least
/// half the a–z, how many were measured). A font is connected when at least
/// half do — the rule Spacing QA applies to the pairs as a browser sets them
/// inside words, here on the pairs as drawn: strokes that meet flush, without
/// overlapping, join too (the detector, `joins::detect`, needs overlap).
pub fn letters_touching(inputs: &[GlyphInput], upm: f64, kinds: &[JoinKind], kerning: &[KernIn]) -> (usize, usize) {
    let n = inputs.len();
    let az: Vec<usize> = (0..n)
        .filter(|&i| kinds.get(i) == Some(&JoinKind::Lower) && !inputs[i].contours.is_empty() && inputs[i].advance.is_finite())
        .collect();
    if az.is_empty() {
        return (0, 0);
    }
    let step = contact::row_step(upm);
    let mut ink: Vec<Option<InkRows>> = vec![None; n];
    for &i in &az {
        ink[i] = Some(InkRows::of_contours(&inputs[i].contours, step, 0.0, 0.0)).filter(|r| !r.is_empty());
    }
    let az: Vec<usize> = az.into_iter().filter(|&i| ink[i].is_some()).collect();
    let current = CurrentKerning::new(kerning);
    let kern = |a: usize, b: usize| {
        let v = current.value_in(a as u32, b as u32, inputs[a].right_group, inputs[b].left_group);
        if v.is_finite() {
            v
        } else {
            0.0
        }
    };
    let half = az.len() as f64 / 2.0;
    let joining = az
        .par_iter()
        .filter(|&&a| {
            let touching = az
                .iter()
                .filter(|&&b| {
                    let offset = inputs[a].advance + kern(a, b);
                    offset.is_finite()
                        && contact::touch_heights(ink[a].as_ref().unwrap(), ink[b].as_ref().unwrap(), offset, step).is_some()
                })
                .count();
            touching as f64 >= half
        })
        .count();
    (joining, az.len())
}

impl FontJoins {
    /// Measures the glyphs of `inputs` and finds the pairs of a letter
    /// (`setup.kinds`) and an a–z letter that join as the font sets them —
    /// in a design whose glyphs touch by construction, of any glyph and an
    /// a–z letter. Progress: one unit per glyph.
    pub fn build(inputs: &[GlyphInput], upm: f64, setup: &JoinSetup, progress: &Progress) -> Result<FontJoins, Cancelled> {
        let n = inputs.len();
        let step = contact::row_step(upm);
        let kinds: Vec<JoinKind> = (0..n).map(|i| setup.kinds.get(i).copied().unwrap_or(JoinKind::Other)).collect();
        let ink: Vec<Option<InkRows>> = inputs
            .par_iter()
            .map(|g| {
                if progress.cancelled() {
                    return None;
                }
                progress.add(1);
                if g.contours.is_empty() {
                    return None;
                }
                let rows = InkRows::of_contours(&g.contours, step, 0.0, 0.0);
                (!rows.is_empty()).then_some(rows)
            })
            .collect();
        if progress.cancelled() {
            return Err(Cancelled);
        }
        let holes: Vec<Vec<f64>> = ink.par_iter().map(|r| r.as_ref().map(|r| r.holes()).unwrap_or_default()).collect();
        let mut fj = FontJoins {
            keep: setup.keep,
            step,
            fragile: FRAGILE * upm / 1000.0,
            kinds,
            ink,
            holes,
            kerning: CurrentKerning::new(&setup.kerning),
            right_group: inputs.iter().map(|g| g.right_group).collect(),
            left_group: inputs.iter().map(|g| g.left_group).collect(),
            advance: inputs.iter().map(|g| g.advance).collect(),
            cur_lsb: inputs.iter().map(|g| g.cur_lsb).collect(),
            cur_rsb: inputs.iter().map(|g| g.cur_rsb).collect(),
            pairs: Vec::new(),
            joined: vec![(false, false); n],
            decorated: false,
        };
        // every ordered pair of a letter and a basic a–z letter (either way)
        // whose boxes meet, by left glyph: the detector's partners, so a
        // font of a thousand letters takes a fraction of a second (a side
        // that joins, joins a–z)
        let letters: Vec<usize> =
            (0..n).filter(|&i| fj.kinds[i] != JoinKind::Other && fj.ink[i].is_some() && fj.advance[i].is_finite()).collect();
        let lower: Vec<bool> = (0..n).map(|i| fj.kinds[i] == JoinKind::Lower).collect();
        let boxes: Vec<(f64, f64, i64, i64)> = (0..n)
            .map(|i| match &fj.ink[i] {
                Some(r) => {
                    let (x0, x1) = r.x_range();
                    (x0, x1, r.k0, r.k_end())
                }
                None => (f64::NAN, f64::NAN, 0, 0),
            })
            .collect();
        let rows: Vec<Vec<FontJoin>> = letters
            .par_iter()
            .map(|&a| {
                let mut out = Vec::new();
                if progress.cancelled() {
                    return out;
                }
                let ra = fj.ink[a].as_ref().unwrap();
                let (_, ax1, ak0, ak1) = boxes[a];
                for &b in &letters {
                    if !lower[a] && !lower[b] {
                        continue;
                    }
                    let (bx0, _, bk0, bk1) = boxes[b];
                    if bk0 >= ak1 || ak0 >= bk1 {
                        continue;
                    }
                    let offset = fj.advance[a] + fj.font_kern(a, b);
                    if !offset.is_finite() || bx0 + offset > ax1 {
                        continue;
                    }
                    let rb = fj.ink[b].as_ref().unwrap();
                    if let Some((y0, y1)) = contact::touch_heights(ra, rb, offset, step) {
                        out.push(FontJoin { left: a as u32, right: b as u32, offset, y0: y0 as f32, y1: y1 as f32 });
                    }
                }
                out
            })
            .collect();
        if progress.cancelled() {
            return Err(Cancelled);
        }
        fj.pairs = rows.into_iter().flatten().collect();
        // a design whose glyphs touch by construction: every glyph that
        // touches the a–z keeps that side too (an underline or a chart stays
        // continuous through the punctuation and figures)
        let az: Vec<usize> = (0..n).filter(|&i| lower[i] && fj.ink[i].is_some() && fj.advance[i].is_finite()).collect();
        let figures: Vec<usize> =
            figure_glyphs(inputs).into_iter().filter(|&i| fj.ink[i].is_some() && fj.advance[i].is_finite()).collect();
        fj.decorated = fj.figures_touch(&figures, &az);
        if fj.decorated {
            let others: Vec<usize> =
                (0..n).filter(|&i| fj.kinds[i] == JoinKind::Other && fj.ink[i].is_some() && fj.advance[i].is_finite()).collect();
            let more: Vec<FontJoin> = others
                .par_iter()
                .flat_map_iter(|&o| {
                    let mut out = Vec::new();
                    for &b in &az {
                        for (l, r) in [(o, b), (b, o)] {
                            let offset = fj.advance[l] + fj.font_kern(l, r);
                            if !offset.is_finite() {
                                continue;
                            }
                            let (ra, rb) = (fj.ink[l].as_ref().unwrap(), fj.ink[r].as_ref().unwrap());
                            if let Some((y0, y1)) = contact::touch_heights(ra, rb, offset, step) {
                                out.push(FontJoin { left: l as u32, right: r as u32, offset, y0: y0 as f32, y1: y1 as f32 });
                            }
                        }
                    }
                    out
                })
                .collect();
            fj.pairs.extend(more);
            fj.pairs.sort_by_key(|p| (p.left, p.right));
        }
        for p in &fj.pairs {
            fj.joined[p.left as usize].1 = true;
            fj.joined[p.right as usize].0 = true;
        }
        Ok(fj)
    }

    /// The decoration test (`DECORATED`) on the checker's ink.
    fn figures_touch(&self, figures: &[usize], az: &[usize]) -> bool {
        figures_touch(&self.ink, &self.advance, &|a, b| self.font_kern(a, b), self.step, figures, az)
    }

    /// The font's kerning of glyph pair (a, b).
    pub fn font_kern(&self, a: usize, b: usize) -> f64 {
        let v = self.kerning.value_in(a as u32, b as u32, self.right_group[a], self.left_group[b]);
        if v.is_finite() {
            v
        } else {
            0.0
        }
    }

    /// Keep joins: gives every side with a join in the font a band (the
    /// heights it touches its partners at, padded) where it has none, so it
    /// is kept and its body measured without the join.
    pub fn add_bands(&self, inputs: &mut [GlyphInput], upm: f64) {
        let n = inputs.len();
        let mut left: Vec<Option<(f64, f64)>> = vec![None; n];
        let mut right: Vec<Option<(f64, f64)>> = vec![None; n];
        let widen = |b: &mut Option<(f64, f64)>, y0: f64, y1: f64| {
            *b = Some(b.map_or((y0, y1), |(lo, hi)| (lo.min(y0), hi.max(y1))));
        };
        for p in &self.pairs {
            widen(&mut right[p.left as usize], p.y0 as f64, p.y1 as f64);
            widen(&mut left[p.right as usize], p.y0 as f64, p.y1 as f64);
        }
        let pad = PAD * upm + 0.5 * self.step;
        for (i, g) in inputs.iter_mut().enumerate() {
            if g.join_left.is_none() {
                g.join_left = left[i].map(|(lo, hi)| (lo - pad, hi + pad));
            }
            if g.join_right.is_none() {
                g.join_right = right[i].map(|(lo, hi)| (lo - pad, hi + pad));
            }
        }
    }

    /// The joins under a spacing: `lsb` / `rsb` per glyph and `kern(a, b)`
    /// (the frame of the context's inputs). `scope[i]`: glyph i takes the
    /// spacing (else it keeps its sides and kerning, as an Apply to fewer
    /// glyphs leaves it). None: every glyph.
    pub fn check(&self, lsb: &[f64], rsb: &[f64], kern: &(dyn Fn(usize, usize) -> f64 + Sync), scope: Option<&[bool]>) -> JoinCheck {
        let n = self.advance.len();
        let takes = |i: usize| scope.map_or(true, |s| s.get(i).copied().unwrap_or(false));
        let side = |i: usize, right: bool| -> f64 {
            if !takes(i) {
                return 0.0;
            }
            let (new, cur) = if right { (rsb.get(i), self.cur_rsb[i]) } else { (lsb.get(i), self.cur_lsb[i]) };
            match new {
                Some(&v) if v.is_finite() && cur.is_finite() => v - cur,
                _ => 0.0,
            }
        };
        let d_l: Vec<f64> = (0..n).map(|i| side(i, false)).collect();
        let d_r: Vec<f64> = (0..n).map(|i| side(i, true)).collect();
        let delta = |a: usize, b: usize| -> f64 {
            let dk = if takes(a) && takes(b) { kern(a, b) - self.font_kern(a, b) } else { 0.0 };
            d_r[a] + d_l[b] + if dk.is_finite() { dk } else { 0.0 }
        };
        let results: Vec<(bool, bool)> = self
            .pairs
            .par_iter()
            .map(|p| {
                let (a, b) = (p.left as usize, p.right as usize);
                let d = delta(a, b);
                let still = d.abs() < 1e-9
                    || contact::touches(self.ink[a].as_ref().unwrap(), self.ink[b].as_ref().unwrap(), p.offset + d, self.step);
                (still, d.abs() >= 0.5 * self.step)
            })
            .collect();
        let mut out = JoinCheck { joins: self.pairs.len(), ..JoinCheck::default() };
        let mut breaks_r = vec![0u32; n];
        let mut breaks_l = vec![0u32; n];
        for (p, &(still, moved)) in self.pairs.iter().zip(&results) {
            out.moved += moved as usize;
            let az = self.kinds[p.left as usize] == JoinKind::Lower && self.kinds[p.right as usize] == JoinKind::Lower;
            out.az_joins += az as usize;
            if still {
                out.kept += 1;
            } else {
                out.broken += 1;
                out.az_broken += az as usize;
                breaks_r[p.left as usize] += 1;
                breaks_l[p.right as usize] += 1;
            }
        }
        for i in 0..n {
            if breaks_r[i] > 0 {
                out.sides.push(SideBreaks { glyph: i as u32, right: true, breaks: breaks_r[i], delta: d_r[i] });
            }
            if breaks_l[i] > 0 {
                out.sides.push(SideBreaks { glyph: i as u32, right: false, breaks: breaks_l[i], delta: d_l[i] });
            }
        }
        out.sides.sort_by(|x, y| y.breaks.cmp(&x.breaks).then(y.delta.abs().total_cmp(&x.delta.abs())));
        // crossings among the basic a–z: as drawn, and new under the spacing
        let az: Vec<usize> = (0..n).filter(|&i| self.kinds[i] == JoinKind::Lower && self.ink[i].is_some()).collect();
        let counts: Vec<(bool, bool)> = az
            .par_iter()
            .flat_map_iter(|&a| az.iter().map(move |&b| (a, b)))
            .map(|(a, b)| {
                let offset = self.advance[a] + self.font_kern(a, b);
                let drawn = self.crossing_at(a, b, offset);
                let d = delta(a, b);
                let after = if d.abs() < 1e-9 { drawn } else { self.crossing_at(a, b, offset + d) };
                (drawn, after && !drawn)
            })
            .collect();
        out.az_crossings_drawn = counts.iter().filter(|c| c.0).count();
        out.az_crossings_made = counts.iter().filter(|c| c.1).count();
        out
    }

    fn crossing_at(&self, a: usize, b: usize, offset: f64) -> bool {
        let (Some(ra), Some(rb)) = (&self.ink[a], &self.ink[b]) else {
            return false;
        };
        let (_, ax1) = ra.x_range();
        let (bx0, _) = rb.x_range();
        if bx0 + offset > ax1 {
            return false; // apart: nothing to enclose
        }
        let own: Vec<f64> = self.holes[a].iter().chain(&self.holes[b]).copied().collect();
        !contact::crossings(ra, rb, offset, &own, MIN_CROSSING_CELLS * self.step * self.step).is_empty()
    }

    /// Pairs in detail: as the font sets them, and under a spacing (as
    /// `check`; `lsb` empty = as drawn).
    pub fn pairs_in_detail(
        &self,
        pairs: &[(u32, u32)],
        lsb: &[f64],
        rsb: &[f64],
        kern: &(dyn Fn(usize, usize) -> f64 + Sync),
        scope: Option<&[bool]>,
    ) -> Vec<PairCheck> {
        let n = self.advance.len();
        let takes = |i: usize| scope.map_or(true, |s| s.get(i).copied().unwrap_or(false));
        let side = |i: usize, right: bool| -> f64 {
            if !takes(i) || lsb.is_empty() {
                return 0.0;
            }
            let (new, cur) = if right { (rsb.get(i), self.cur_rsb[i]) } else { (lsb.get(i), self.cur_lsb[i]) };
            match new {
                Some(&v) if v.is_finite() && cur.is_finite() => v - cur,
                _ => 0.0,
            }
        };
        pairs
            .par_iter()
            .map(|&(l, r)| {
                let (a, b) = (l as usize, r as usize);
                let mut out = PairCheck { left: l, right: r, height: f64::NAN, ..PairCheck::default() };
                if a >= n || b >= n {
                    return out;
                }
                let (Some(ra), Some(rb)) = (&self.ink[a], &self.ink[b]) else {
                    return out;
                };
                let offset = self.advance[a] + self.font_kern(a, b);
                let set = contact::touch_set(ra, rb);
                let same = contact::touch_set_rows(ra, rb, false);
                out.drawn = contact::contact_both(&same, &set, offset, self.fragile, self.step);
                if out.drawn.joins {
                    out.height = contact::contact_height(ra, rb, offset);
                }
                out.crossing_drawn = self.crossing_at(a, b, offset);
                if !out.drawn.joins && out.drawn.fix.is_finite() {
                    out.fix_crosses = self.crossing_at(a, b, offset + out.drawn.fix);
                }
                let dk = if takes(a) && takes(b) && !lsb.is_empty() { kern(a, b) - self.font_kern(a, b) } else { 0.0 };
                out.delta = side(a, true) + side(b, false) + if dk.is_finite() { dk } else { 0.0 };
                let after = offset + out.delta;
                out.joins_after = set.iter().any(|&(lo, hi)| lo - self.step <= after && after <= hi + self.step);
                out.crossing_after =
                    if out.delta.abs() < 1e-9 { out.crossing_drawn } else { self.crossing_at(a, b, after) };
                out
            })
            .collect()
    }
}

impl JoinSetup {
    /// No letters (a context without a checker behaves as before).
    pub fn none(n: usize) -> JoinSetup {
        JoinSetup { kinds: vec![JoinKind::Other; n], kerning: Vec::new(), keep: false }
    }
}
