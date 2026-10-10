//! Rust API for tools that link the engine directly (Spacing QA, tests): the
//! same model as the C ABI, synchronously and without jobs.
//!
//! ```ignore
//! let engine = Engine::prepare(inputs, upm, threads)?;
//! let standard = engine.solve(&Settings::new())?;
//! let loose = engine.solve(&Settings { looseness: 0.5, ..Settings::new() })?;
//! let k = loose.kerning(a, b);
//! ```

use std::collections::HashMap;
use std::sync::Arc;

use crate::engine::Context;
use crate::job::Progress;
use crate::run::{self, Params, KIND_CLASS_CLASS, KIND_CLASS_GLYPH, KIND_GLYPH_CLASS, KIND_GLYPH_GLYPH};

pub use crate::classes::{EXISTING, GLYPH_KEYED};
pub use crate::engine::{
    physics_for_looseness, GlyphInput, GlyphOpt, SideRule, SolveOptions, COUPLING_CALIBRATION, GLYPH_FIGURE, GLYPH_FIXED_ADVANCE,
    GLYPH_KERN, GLYPH_RTL, GROUP_LOWERCASE, GROUP_OTHER, GROUP_UPPERCASE, LOOSENESS_GAIN, LOOSENESS_RATIO, MAX_GROUPS,
    NONE,
};
pub use crate::geometry::{Vec2, NODE_CURVE, NODE_LINE, NODE_OFFCURVE, NODE_QCURVE};
pub use crate::checker::{
    decorated_design, letters_touching, FontJoin, JoinCheck, JoinSetup, PairCheck, SideBreaks, DECORATED as JOIN_DECORATED,
    FRAGILE as JOIN_FRAGILE, MIN_CROSSING_CELLS,
};
pub use crate::contact::{
    contact as join_contact, contact_both, contact_height, contact_in, crossings, row_step, touch_heights, touch_set,
    touch_set_rows, touches, Contact, InkRows,
};
pub use crate::joins::{detect as detect_joins, Bands as JoinBands, JoinGlyph, JoinKind, JoinRule};
pub use crate::measure::{KernIn, Measured, PairOut};
pub use crate::pass2::Solver;
pub use crate::physics::rest_gap;
pub use crate::run::{Harness, Mode};

/// On WebAssembly (no threads), makes the calling thread the engine's only
/// worker; call it once before anything else there. Nothing elsewhere.
pub fn single_thread() {
    #[cfg(target_arch = "wasm32")]
    let _ = crate::job::pool(1);
}

/// Rhythm group of figures (`GlyphInput::group`).
pub const GROUP_FIGURES: u32 = 3;

/// Exact ink bounds [x0, x1, y0, y1] of contours in the engine's node format
/// (curve extrema included), the frame the engine measures sidebearings in;
/// None for an empty outline.
pub fn outline_bbox(contours: &[Vec<(Vec2, u32)>], upm: f64) -> Option<[f64; 4]> {
    let o = crate::geometry::Outline::from_contours(contours, 0.05 * upm / 1000.0);
    if o.is_empty() {
        None
    } else {
        Some([o.bbox.x0, o.bbox.x1, o.bbox.y0, o.bbox.y1])
    }
}

/// Entry kinds of `Solution::entries`.
pub const ENTRY_GLYPH_GLYPH: u8 = KIND_GLYPH_GLYPH;
pub const ENTRY_CLASS_CLASS: u8 = KIND_CLASS_CLASS;
pub const ENTRY_GLYPH_CLASS: u8 = KIND_GLYPH_CLASS;
pub const ENTRY_CLASS_GLYPH: u8 = KIND_CLASS_GLYPH;

/// What one solve does, in the plugin's terms.
#[derive(Clone, Debug)]
pub struct Settings {
    /// Looseness slider position (−1 tight … +1 loose, 0 = the tuned default).
    pub looseness: f64,
    /// Kerning intensity in percent (100 = calibrated).
    pub intensity: f64,
    pub mode: Mode,
    pub solver: Solver,
    /// Pairs only within one script plus Common/Inherited.
    pub scope_scripts: bool,
    /// Drop kerning below this many units per 1000 em (≤ 0.5 units keeps all).
    pub threshold_per_1000: f64,
    /// Maximum entries (0 = unlimited).
    pub budget: usize,
    /// Worker threads (0 = all cores but one).
    pub threads: usize,
    /// Glyphs to kern (None = all).
    pub kern_mask: Option<Vec<bool>>,
    /// Per-glyph spacing groups (frozen, Looseness offset, kerning force).
    pub glyph_opts: Option<Vec<GlyphOpt>>,
    /// Fit the Looseness to the frozen glyphs first.
    pub fit_frozen: bool,
    /// Corrections toward what well-spaced fonts do, after the solve.
    pub harness: Option<Harness>,
    /// Skip Pass 2 (sidebearings only).
    pub skip_kerning: bool,
}

impl Settings {
    /// Glyph pairs, window solver, script scope, every value kept: the
    /// benchmark's configuration.
    pub fn new() -> Self {
        Settings {
            looseness: 0.0,
            intensity: 100.0,
            mode: Mode::Pairs,
            solver: Solver::Window,
            scope_scripts: true,
            threshold_per_1000: 0.0,
            budget: 0,
            threads: 0,
            kern_mask: None,
            glyph_opts: None,
            fit_frozen: false,
            harness: None,
            skip_kerning: false,
        }
    }

    /// The solve options these settings mean for a font of `upm` units.
    pub fn options(&self, upm: f64) -> SolveOptions {
        let mut o = SolveOptions::defaults(upm);
        let (spring, repulsion) = physics_for_looseness(self.looseness);
        o.spring = spring;
        o.repulsion = repulsion;
        o.coupling = if self.skip_kerning { 0.0 } else { (self.intensity / 100.0).max(0.0) };
        o
    }
}

impl Default for Settings {
    fn default() -> Self {
        Self::new()
    }
}

/// One adaptive ray of a glyph side: height, ink x (None where the side has
/// no ink) and the curvature tier that placed it (0 structural … 4 finest).
#[derive(Clone, Copy, Debug)]
pub struct Ray {
    pub y: f64,
    pub x: Option<f64>,
    pub tier: u8,
}

/// A prepared font (Phase 1/3): profiles, disks and classes, shared by any
/// number of solves.
#[derive(Clone)]
pub struct Engine {
    ctx: Arc<Context>,
}

impl Engine {
    /// Analyzes every glyph (`threads` = 0: all cores but one).
    pub fn prepare(inputs: Vec<GlyphInput>, upm: f64, threads: usize) -> Result<Engine, String> {
        let progress = Progress::new();
        let ctx = Context::prepare(inputs, upm, threads, &progress).map_err(|_| "cancelled".to_string())?;
        Ok(Engine { ctx: Arc::new(ctx) })
    }

    /// `prepare` for a connected script (its join bands in the inputs): every
    /// glyph with an outline is measured for the join checker, and with `setup.keep`
    /// (Keep joins) every joining side keeps its sidebearing and every pair
    /// of two joining sides the font's kerning (crate::checker).
    pub fn prepare_with_joins(inputs: Vec<GlyphInput>, upm: f64, threads: usize, setup: &JoinSetup) -> Result<Engine, String> {
        let progress = Progress::new();
        let ctx = Context::prepare_with_joins(inputs, upm, threads, &progress, Some(setup))
            .map_err(|_| "cancelled".to_string())?;
        Ok(Engine { ctx: Arc::new(ctx) })
    }

    /// The join bands each glyph was prepared with (Keep joins adds the sides
    /// with joins in the font), (left, right).
    pub fn join_bands(&self) -> Vec<JoinBands> {
        self.ctx.glyphs.iter().map(|g| (g.join_left, g.join_right)).collect()
    }

    /// Keep joins: the sides that keep their sidebearings, (left, right).
    pub fn kept_sides(&self) -> Vec<(bool, bool)> {
        self.ctx.glyphs.iter().map(|g| (g.kept_left, g.kept_right)).collect()
    }

    /// Keep joins was asked for.
    pub fn keeps_joins(&self) -> bool {
        self.ctx.joins.as_ref().is_some_and(|j| j.keep)
    }

    /// The glyphs touch by construction (a line, a grid or a background
    /// through every glyph, figures included: `checker::DECORATED`); every
    /// side that touches the a–z is then kept, letter or not.
    pub fn join_decorated(&self) -> bool {
        self.ctx.joins.as_ref().is_some_and(|j| j.decorated)
    }

    /// The pairs of glyphs that join as the font sets them (empty without
    /// `prepare_with_joins`).
    pub fn font_joins(&self) -> &[FontJoin] {
        self.ctx.joins.as_ref().map_or(&[], |j| &j.pairs)
    }

    /// The font's kerning of a glyph pair, as the join checker reads it.
    pub fn font_kerning(&self, a: usize, b: usize) -> f64 {
        self.ctx.joins.as_ref().map_or(0.0, |j| j.font_kern(a, b))
    }

    /// What a solution does to the font's joins (None without
    /// `prepare_with_joins`). `scope`: the glyphs that take the solution
    /// (None: all; the others keep their sides and kerning).
    pub fn check_joins(&self, solution: &Solution, scope: Option<&[bool]>) -> Option<JoinCheck> {
        let j = self.ctx.joins.as_ref()?;
        let kern = |a: usize, b: usize| solution.kerning(a, b);
        Some(crate::job::pool(0).install(|| j.check(&solution.lsb, &solution.rsb, &kern, scope)))
    }

    /// Pairs in detail as the font sets them, and under `solution` if given.
    pub fn check_pairs(&self, pairs: &[(u32, u32)], solution: Option<&Solution>, scope: Option<&[bool]>) -> Vec<PairCheck> {
        let Some(j) = self.ctx.joins.as_ref() else {
            return Vec::new();
        };
        crate::job::pool(0).install(|| match solution {
            Some(sol) => {
                let kern = |a: usize, b: usize| sol.kerning(a, b);
                j.pairs_in_detail(pairs, &sol.lsb, &sol.rsb, &kern, scope)
            }
            None => j.pairs_in_detail(pairs, &[], &[], &|_, _| 0.0, scope),
        })
    }

    pub fn upm(&self) -> f64 {
        self.ctx.upm
    }

    pub fn glyph_count(&self) -> usize {
        self.ctx.glyphs.len()
    }

    /// The glyph has an outline the engine can space.
    pub fn valid(&self, glyph: usize) -> bool {
        self.ctx.glyphs.get(glyph).is_some_and(|g| g.valid)
    }

    /// Ink bounds [x0, x1, y0, y1].
    pub fn bbox(&self, glyph: usize) -> [f64; 4] {
        let b = self.ctx.glyphs[glyph].bbox;
        [b.x0, b.x1, b.y0, b.y1]
    }

    /// The spacing zone (heights) of the glyph.
    pub fn zone(&self, glyph: usize) -> (f64, f64) {
        self.ctx.glyphs[glyph].zone
    }

    /// Adaptive rays of one side (`right` = the glyph's right side).
    pub fn rays(&self, glyph: usize, right: bool) -> Vec<Ray> {
        let g = &self.ctx.glyphs[glyph];
        let p = if right { &g.right } else { &g.left };
        p.rays
            .iter()
            .map(|r| Ray { y: r.y, x: if r.has_ink() { Some(r.x) } else { None }, tier: r.tier })
            .collect()
    }

    /// Rhythm scale λ of the glyph's group.
    pub fn rhythm_scale(&self, glyph: usize) -> f64 {
        self.ctx.rhythm_scale[self.ctx.glyphs[glyph].group]
    }

    /// The Looseness (absolute slider position) at which Pass 1 gives the
    /// glyphs flagged in `which`, on average, the sidebearings they have now.
    pub fn fit_looseness(&self, settings: &Settings, which: &[bool]) -> Option<f64> {
        let o = settings.options(self.ctx.upm);
        self.ctx.fit_looseness(&o, which).map(|dt| settings.looseness + dt)
    }

    /// `fit_looseness` over sides: the left sides flagged in `left` and the
    /// right sides in `right` (kept joins, frozen glyphs).
    pub fn fit_looseness_sides(&self, settings: &Settings, left: &[bool], right: &[bool]) -> Option<f64> {
        let o = settings.options(self.ctx.upm);
        self.ctx.fit_looseness_sides(&o, left, right).map(|dt| settings.looseness + dt)
    }

    /// One solve; blocks until done.
    pub fn solve(&self, s: &Settings) -> Result<Solution, String> {
        Ok(self.run(s, false)?.0)
    }

    /// One solve with the designer harness of `s`, and the same solve as it
    /// was before the harness — the bare model — at no extra cost: (with the
    /// harness, bare). Without a harness both are the same solve.
    pub fn solve_with_bare(&self, s: &Settings) -> Result<(Solution, Solution), String> {
        let (with, bare) = self.run(s, true)?;
        Ok(match bare {
            Some(bare) => (with, bare),
            None => (with.clone(), with),
        })
    }

    fn run(&self, s: &Settings, keep_bare: bool) -> Result<(Solution, Option<Solution>), String> {
        let ctx = &self.ctx;
        let p = Params {
            options: s.options(ctx.upm),
            mode: s.mode,
            solver: s.solver,
            scope_scripts: s.scope_scripts,
            threshold: (s.threshold_per_1000 * ctx.upm / 1000.0).max(0.5),
            budget: s.budget,
            radius_ratio: 1.0,
            threads: s.threads,
            glyph_opts: s.glyph_opts.clone().map(Arc::new),
            fit_frozen: s.fit_frozen,
            harness: s.harness.clone().map(Arc::new),
            keep_bare,
        };
        let mask: Option<Vec<u8>> = s.kern_mask.as_ref().map(|m| m.iter().map(|&b| b as u8).collect());
        let progress = Progress::new();
        let out = run::run(ctx, &p, mask.as_deref(), &progress).map_err(|e| match e {
            crate::job::JobError::Cancelled => "cancelled".to_string(),
            crate::job::JobError::Failed(msg) => msg,
        })?;
        let classes = out.classes.as_ref().unwrap_or(&ctx.classes);
        let solution = |pass1: &crate::engine::Pass1, raw: &[run::Entry]| {
            let mut look = Lookup { classes: out.mode == Mode::Classes, ..Lookup::default() };
            let entries: Vec<Entry> = raw
                .iter()
                .map(|e| Entry { kind: e.kind, left: e.left, right: e.right, value: e.value, importance: e.importance })
                .collect();
            for e in &entries {
                let map = match e.kind {
                    KIND_CLASS_CLASS => &mut look.cc,
                    KIND_GLYPH_CLASS => &mut look.gc,
                    KIND_CLASS_GLYPH => &mut look.cg,
                    _ => &mut look.gg,
                };
                map.insert((e.left, e.right), e.value);
            }
            let m = &pass1.metrics;
            Solution {
                lsb: pass1.lsb.clone(),
                rsb: pass1.rsb.clone(),
                wanted_lsb: pass1.wanted_lsb.clone(),
                wanted_rsb: pass1.wanted_rsb.clone(),
                advance: m.iter().map(|x| x.advance).collect(),
                valid: m.iter().map(|x| x.valid).collect(),
                kerned: out.kern.clone(),
                fitted: if out.fitted.is_finite() { Some(s.looseness + out.fitted) } else { None },
                rest_gap: pass1.rest_gap,
                right_class: classes.right.class_of.clone(),
                left_class: classes.left.class_of.clone(),
                right_origin: classes.right.origin.clone(),
                left_origin: classes.left.origin.clone(),
                entries,
                look,
            }
        };
        let with = solution(&out.pass1, &out.entries);
        let bare = out.bare.as_ref().map(|(p1, e)| solution(p1, e));
        Ok((with, bare))
    }

    /// The font's spacing as it is (each glyph's current sidebearings, from
    /// `GlyphInput::cur_lsb` / `cur_rsb`, plus `current` kerning) against a
    /// solution, over the glyphs flagged in `mask`. See `measure`.
    pub fn measure(
        &self,
        solution: &Solution,
        current: &[KernIn],
        mask: &[bool],
        scope_scripts: bool,
        cap: usize,
    ) -> Measured {
        let model = |a: u32, b: u32| solution.kerning(a as usize, b as usize);
        crate::measure::measure(&self.ctx, &solution.lsb, &solution.rsb, &model, current, mask, scope_scripts, cap)
    }
}

/// One kerning entry of a solution (see the C ABI's `KK2Entry`).
#[derive(Clone, Copy, Debug)]
pub struct Entry {
    pub kind: u8,
    pub left: u32,
    pub right: u32,
    pub value: f64,
    pub importance: f64,
}

#[derive(Clone, Default)]
struct Lookup {
    classes: bool,
    gg: HashMap<(u32, u32), f64>,
    gc: HashMap<(u32, u32), f64>,
    cg: HashMap<(u32, u32), f64>,
    cc: HashMap<(u32, u32), f64>,
}

/// The result of one solve.
#[derive(Clone)]
pub struct Solution {
    pub lsb: Vec<f64>,
    pub rsb: Vec<f64>,
    /// What Pass 1 wanted before rules, frozen glyphs and kept joins: for a
    /// kept join, the sidebearing the model would give its body (drawing advice).
    pub wanted_lsb: Vec<f64>,
    pub wanted_rsb: Vec<f64>,
    pub advance: Vec<f64>,
    pub valid: Vec<bool>,
    /// Glyphs kerned in this solve.
    pub kerned: Vec<bool>,
    /// Looseness the solve moved to with `fit_frozen` (absolute slider units).
    pub fitted: Option<f64>,
    /// Pass-1 rest gap per rhythm group id (NaN where unused).
    pub rest_gap: [f64; MAX_GROUPS as usize],
    pub right_class: Vec<u32>,
    pub left_class: Vec<u32>,
    pub right_origin: Vec<u32>,
    pub left_origin: Vec<u32>,
    pub entries: Vec<Entry>,
    look: Lookup,
}

impl Solution {
    /// Kerning of glyph pair (a, b) with the usual precedence; 0 if none or
    /// either glyph was not kerned.
    pub fn kerning(&self, a: usize, b: usize) -> f64 {
        if !self.kerned.get(a).copied().unwrap_or(false) || !self.kerned.get(b).copied().unwrap_or(false) {
            return 0.0;
        }
        let (a32, b32) = (a as u32, b as u32);
        let l = &self.look;
        if let Some(&v) = l.gg.get(&(a32, b32)) {
            return v;
        }
        if !l.classes {
            return 0.0;
        }
        let (ra, lb) = (self.right_class[a], self.left_class[b]);
        if let Some(&v) = l.gc.get(&(a32, lb)) {
            return v;
        }
        if let Some(&v) = l.cg.get(&(ra, b32)) {
            return v;
        }
        l.cc.get(&(ra, lb)).copied().unwrap_or(0.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn poly(points: &[(f64, f64)]) -> Vec<(Vec2, u32)> {
        points.iter().map(|&(x, y)| (Vec2::new(x, y), NODE_LINE)).collect()
    }

    fn rect(x0: f64, y0: f64, x1: f64, y1: f64) -> Vec<(Vec2, u32)> {
        poly(&[(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    }

    /// H, V, O (an octagon), period.
    fn font() -> Engine {
        font_with(false)
    }

    /// `font`, with H's advance kept (`fixed_h`: a width-keyed glyph).
    fn font_with(fixed_h: bool) -> Engine {
        let mut h = GlyphInput::simple(
            vec![rect(60.0, 0.0, 140.0, 700.0), rect(460.0, 0.0, 540.0, 700.0), rect(140.0, 320.0, 460.0, 390.0)],
            600.0,
            GROUP_UPPERCASE,
        );
        if fixed_h {
            h.flags |= GLYPH_FIXED_ADVANCE;
        }
        let v = GlyphInput::simple(
            vec![poly(&[(10.0, 700.0), (95.0, 700.0), (300.0, 110.0), (505.0, 700.0), (590.0, 700.0), (340.0, 0.0), (260.0, 0.0)])],
            600.0,
            GROUP_UPPERCASE,
        );
        let o = GlyphInput::simple(
            vec![
                poly(&[(250.0, 0.0), (420.0, 0.0), (560.0, 200.0), (560.0, 500.0), (420.0, 700.0), (250.0, 700.0), (110.0, 500.0), (110.0, 200.0)]),
                poly(&[(280.0, 80.0), (190.0, 230.0), (190.0, 470.0), (280.0, 620.0), (390.0, 620.0), (480.0, 470.0), (480.0, 230.0), (390.0, 80.0)]),
            ],
            670.0,
            GROUP_UPPERCASE,
        );
        let dot = GlyphInput::simple(vec![rect(60.0, 0.0, 150.0, 90.0)], 210.0, GROUP_OTHER);
        Engine::prepare(vec![h, v, o, dot], 1000.0, 1).unwrap()
    }

    fn harness() -> Harness {
        Harness { sides: vec![[5.0, -3.0], [0.0, 0.0], [2.0, 4.0], [-6.0, 0.0]], pairs: vec![(1, 3, -20.0), (0, 1, 7.0)] }
    }

    #[test]
    fn the_harness_shifts_sides_and_corrects_pairs() {
        let e = font();
        for mode in [Mode::Pairs, Mode::Classes] {
            let base = Settings { mode, ..Settings::new() };
            let plain = e.solve(&base).unwrap();
            let with = e.solve(&Settings { harness: Some(harness()), ..base.clone() }).unwrap();
            for (i, s) in harness().sides.iter().enumerate() {
                assert!((with.lsb[i] - plain.lsb[i] - s[0]).abs() < 1e-9, "{mode:?} lsb {i}");
                assert!((with.rsb[i] - plain.rsb[i] - s[1]).abs() < 1e-9, "{mode:?} rsb {i}");
                assert!((with.advance[i] - plain.advance[i] - s[0] - s[1]).abs() < 1e-9, "{mode:?} advance {i}");
            }
            // the corrected pairs, over whatever kerning they had
            assert!((with.kerning(1, 3) - plain.kerning(1, 3) + 20.0).abs() < 1e-6, "{mode:?} V period");
            assert!((with.kerning(0, 1) - plain.kerning(0, 1) - 7.0).abs() < 1e-6, "{mode:?} H V");
            // every other pair as it was
            for a in 0..4 {
                for b in 0..4 {
                    if (a, b) != (1, 3) && (a, b) != (0, 1) {
                        assert!((with.kerning(a, b) - plain.kerning(a, b)).abs() < 1e-9, "{mode:?} pair {a} {b}");
                    }
                }
            }
        }
    }

    #[test]
    fn the_harness_keeps_an_advance_that_is_kept() {
        // H keeps its advance (a width key): the harness's side shifts would
        // change it, so they leave H alone; the other glyphs take theirs
        let e = font_with(true);
        let plain = e.solve(&Settings::new()).unwrap();
        let with = e.solve(&Settings { harness: Some(harness()), ..Settings::new() }).unwrap();
        assert_eq!((with.lsb[0], with.rsb[0], with.advance[0]), (plain.lsb[0], plain.rsb[0], plain.advance[0]));
        assert!((with.lsb[2] - plain.lsb[2] - 2.0).abs() < 1e-9 && (with.rsb[2] - plain.rsb[2] - 4.0).abs() < 1e-9);
    }

    #[test]
    fn the_bare_model_is_the_solve_before_the_harness() {
        let e = font();
        for mode in [Mode::Pairs, Mode::Classes] {
            let base = Settings { mode, ..Settings::new() };
            let plain = e.solve(&base).unwrap();
            let with = e.solve(&Settings { harness: Some(harness()), ..base.clone() }).unwrap();
            let (w, bare) = e.solve_with_bare(&Settings { harness: Some(harness()), ..base.clone() }).unwrap();
            for i in 0..4 {
                assert_eq!((bare.lsb[i], bare.rsb[i], bare.advance[i]), (plain.lsb[i], plain.rsb[i], plain.advance[i]), "{mode:?} bare {i}");
                assert_eq!((w.lsb[i], w.rsb[i], w.advance[i]), (with.lsb[i], with.rsb[i], with.advance[i]), "{mode:?} with {i}");
            }
            for a in 0..4 {
                for b in 0..4 {
                    assert_eq!(bare.kerning(a, b), plain.kerning(a, b), "{mode:?} bare pair {a} {b}");
                    assert_eq!(w.kerning(a, b), with.kerning(a, b), "{mode:?} pair {a} {b}");
                }
            }
            // without a harness both are the plain solve
            let (x, y) = e.solve_with_bare(&base).unwrap();
            assert_eq!((x.lsb.clone(), y.lsb.clone()), (plain.lsb.clone(), plain.lsb.clone()));
        }
    }

    /// Three lowercase letters of a connected script — a body with an entry
    /// stroke on the left and an exit stroke on the right, along the baseline,
    /// both reaching past the advance — and a period. `joins`: the strokes are
    /// declared as joins.
    fn script(joins: bool) -> Engine {
        // an n: two stems and an arch (its counter sets the rhythm), with the strokes
        let letter = |w: f64| {
            let mut g = GlyphInput::simple(
                vec![
                    rect(-20.0, 0.0, 100.0, 40.0),
                    rect(100.0, 0.0, 170.0, 500.0),
                    rect(170.0, 430.0, 30.0 + w, 500.0),
                    rect(30.0 + w, 0.0, 100.0 + w, 500.0),
                    rect(100.0 + w, 0.0, 220.0 + w, 40.0),
                ],
                200.0 + w,
                GROUP_LOWERCASE,
            );
            if joins {
                g.join_left = Some((0.0, 40.0));
                g.join_right = Some((0.0, 40.0));
            }
            g
        };
        let dot = GlyphInput::simple(vec![rect(60.0, 0.0, 150.0, 90.0)], 210.0, GROUP_OTHER);
        Engine::prepare(vec![letter(280.0), letter(300.0), letter(260.0), dot], 1000.0, 1).unwrap()
    }

    #[test]
    fn a_connected_script_overlaps_at_its_joins() {
        let gap = |s: &Solution, a: usize, b: usize| s.rsb[a] + s.lsb[b] + s.kerning(a, b);
        for mode in [Mode::Pairs, Mode::Classes] {
            let base = Settings { mode, ..Settings::new() };
            let plain = script(false).solve(&base).unwrap();
            let joined = script(true).solve(&base).unwrap();
            // without joins the strokes are the letters' edges: no overlap anywhere
            assert!(gap(&plain, 0, 1) > 0.0, "{mode:?} {}", gap(&plain, 0, 1));
            // with them each stroke overhangs its body, and two letters overlap
            // at the join (their bodies are spaced), without kerning
            assert!(joined.rsb[0] < 0.0 && joined.lsb[1] < 0.0, "{mode:?} joined rsb {} lsb {}; plain rsb {} lsb {}", joined.rsb[0], joined.lsb[1], plain.rsb[0], plain.lsb[1]);
            for a in 0..3 {
                for b in 0..3 {
                    assert_eq!(joined.kerning(a, b), 0.0, "{mode:?} join pair {a} {b}");
                    assert!(gap(&joined, a, b) < 0.0, "{mode:?} {a} {b} overlap {}", gap(&joined, a, b));
                }
            }
            // a period after a letter keeps its clearance from the exit stroke
            assert!(gap(&joined, 0, 3) >= 0.0, "{mode:?} letter period {}", gap(&joined, 0, 3));
            assert!(gap(&joined, 3, 1) >= 0.0, "{mode:?} period letter {}", gap(&joined, 3, 1));
        }
    }

    /// The script of `script(true)` as a font would have it: current
    /// sidebearings, the letters' kinds and some kerning (a join pair −7, a
    /// letter before the period −15); the third letter's entry has no band
    /// (the detector missed it) but touches in the font.
    fn script_font(keep: bool) -> Engine {
        let mut inputs = Vec::new();
        for (k, w) in [280.0, 300.0, 260.0].into_iter().enumerate() {
            let mut g = GlyphInput::simple(
                vec![
                    rect(-20.0, 0.0, 100.0, 40.0),
                    rect(100.0, 0.0, 170.0, 500.0),
                    rect(170.0, 430.0, 30.0 + w, 500.0),
                    rect(30.0 + w, 0.0, 100.0 + w, 500.0),
                    rect(100.0 + w, 0.0, 220.0 + w, 40.0),
                ],
                200.0 + w,
                GROUP_LOWERCASE,
            );
            g.join_left = if k == 2 { None } else { Some((0.0, 40.0)) };
            g.join_right = Some((0.0, 40.0));
            g.cur_lsb = -20.0;
            g.cur_rsb = -20.0;
            inputs.push(g);
        }
        let mut dot = GlyphInput::simple(vec![rect(60.0, 0.0, 150.0, 90.0)], 210.0, GROUP_OTHER);
        dot.cur_lsb = 60.0;
        dot.cur_rsb = 60.0;
        inputs.push(dot);
        let kinds = vec![JoinKind::Lower, JoinKind::Lower, JoinKind::Lower, JoinKind::Other];
        let kerning = vec![
            KernIn { kind: ENTRY_GLYPH_GLYPH, left: 0, right: 1, value: -7.0 },
            KernIn { kind: ENTRY_GLYPH_GLYPH, left: 1, right: 3, value: -15.0 },
        ];
        Engine::prepare_with_joins(inputs, 1000.0, 1, &JoinSetup { kinds, kerning, keep }).unwrap()
    }

    #[test]
    fn keep_joins_keeps_every_join_as_drawn() {
        let e = script_font(true);
        assert!(e.keeps_joins());
        // the third letter's entry joins in the font: it got a band and is kept
        let kept = e.kept_sides();
        assert!(kept[..3].iter().all(|&(l, r)| l && r), "{kept:?}");
        assert_eq!(kept[3], (false, false));
        // every letter pair joins in the font (the strokes overlap by 33–40)
        assert_eq!(e.font_joins().len(), 9, "{:?}", e.font_joins());
        for mode in [Mode::Pairs, Mode::Classes] {
            for looseness in [-1.0, 0.0, 1.0] {
                // a threshold above the kept −7 must not drop it
                let s = Settings { mode, looseness, threshold_per_1000: 10.0, ..Settings::new() };
                let sol = e.solve(&s).unwrap();
                for i in 0..3 {
                    assert_eq!((sol.lsb[i], sol.rsb[i]), (-20.0, -20.0), "{mode:?} {looseness} letter {i}");
                }
                for a in 0..3 {
                    for b in 0..3 {
                        let want = if (a, b) == (0, 1) { -7.0 } else { 0.0 };
                        assert_eq!(sol.kerning(a, b), want, "{mode:?} {looseness} pair {a} {b}");
                    }
                }
                let check = e.check_joins(&sol, None).unwrap();
                assert_eq!((check.joins, check.kept, check.broken, check.moved), (9, 9, 0, 0), "{mode:?} {looseness}");
                // the period is spaced by the model: clear of the exit stroke
                let gap = sol.rsb[1] + sol.lsb[3] + sol.kerning(1, 3);
                assert!(gap >= 0.0, "{mode:?} {looseness} letter period {gap}");
                // what the model wanted for a kept side: its body spacing
                assert!(sol.wanted_rsb[0].is_finite() && sol.wanted_rsb[0] != -20.0, "{}", sol.wanted_rsb[0]);
            }
        }
        // the Looseness fitted to the kept sides
        let fitted = e.solve(&Settings { fit_frozen: true, ..Settings::new() }).unwrap();
        assert!(fitted.fitted.is_some());
    }

    #[test]
    fn spacing_joined_letters_is_checked_against_the_font() {
        let e = script_font(false);
        assert!(!e.keeps_joins());
        assert_eq!(e.kept_sides().iter().filter(|s| s.0 || s.1).count(), 0);
        // the joins are found either way
        assert_eq!(e.font_joins().len(), 9);
        for looseness in [-1.0, 0.0, 1.0, 3.0] {
            let sol = e.solve(&Settings { looseness, ..Settings::new() }).unwrap();
            let check = e.check_joins(&sol, None).unwrap();
            assert_eq!(check.kept + check.broken, 9);
            let breaks: u32 = check.sides.iter().map(|s| s.breaks).sum();
            assert_eq!(breaks as usize, 2 * check.broken, "{looseness} {check:?}");
            // pair details agree with the summary
            let pairs: Vec<(u32, u32)> = e.font_joins().iter().map(|p| (p.left, p.right)).collect();
            let detail = e.check_pairs(&pairs, Some(&sol), None);
            assert_eq!(detail.iter().filter(|d| !d.joins_after).count(), check.broken, "{looseness}");
            assert!(detail.iter().all(|d| d.drawn.joins));
        }
        // loose enough, the bodies part and every join breaks
        let sol = e.solve(&Settings { looseness: 6.0, ..Settings::new() }).unwrap();
        assert!(e.check_joins(&sol, None).unwrap().broken > 0);
    }

    /// Thirteen letters, five figures (`tabular`: with a fixed advance) and a
    /// period; with `underline`, a line runs under every glyph exactly from
    /// its origin to its advance (touching its neighbours, overlapping none).
    fn underlined_inputs(underline: bool, tabular: bool) -> (Vec<GlyphInput>, Vec<JoinKind>) {
        let mut inputs = Vec::new();
        let mut kinds = Vec::new();
        let glyph = |w: f64, group: u32| {
            let mut c = vec![rect(40.0, 0.0, w - 40.0, 500.0)];
            if underline {
                c.push(rect(0.0, -120.0, w, -80.0));
            }
            let mut g = GlyphInput::simple(c, w, group);
            g.cur_lsb = if underline { 0.0 } else { 40.0 };
            g.cur_rsb = g.cur_lsb;
            g
        };
        for k in 0..13 {
            inputs.push(glyph(300.0 + 10.0 * k as f64, GROUP_LOWERCASE));
            kinds.push(JoinKind::Lower);
        }
        for _ in 0..5 {
            let mut g = glyph(320.0, GROUP_FIGURES);
            if tabular {
                g.flags |= GLYPH_FIXED_ADVANCE;
            }
            inputs.push(g);
            kinds.push(JoinKind::Other);
        }
        inputs.push(glyph(200.0, GROUP_OTHER));
        kinds.push(JoinKind::Other);
        (inputs, kinds)
    }

    fn underlined(underline: bool) -> Engine {
        let (inputs, kinds) = underlined_inputs(underline, false);
        Engine::prepare_with_joins(inputs, 1000.0, 1, &JoinSetup { kinds, kerning: Vec::new(), keep: true }).unwrap()
    }

    #[test]
    fn a_line_through_every_glyph_is_a_decoration_not_a_script() {
        let e = underlined(true);
        assert!(e.join_decorated());
        // every glyph keeps both sides, the figures and the period included,
        // so the line stays continuous
        assert!(e.kept_sides().iter().all(|&(l, r)| l && r), "{:?}", e.kept_sides());
        // without the line the letters stand apart: no joins, no decoration
        let plain = underlined(false);
        assert!(!plain.join_decorated());
        assert!(plain.kept_sides().iter().all(|&(l, r)| !l && !r));
        // a script's figures stand apart from its letters
        assert!(!script_font(true).join_decorated());
        // the same rule on the glyphs alone (what the plugin asks when the
        // detector finds nothing overlapping): an edge-to-edge line touches
        let (inputs, kinds) = underlined_inputs(true, false);
        assert!(decorated_design(&inputs, 1000.0, &kinds, &[]));
        let (inputs, kinds) = underlined_inputs(false, false);
        assert!(!decorated_design(&inputs, 1000.0, &kinds, &[]));
    }

    #[test]
    fn letters_that_meet_flush_join_by_touching() {
        // the line runs exactly from edge to edge: every letter touches
        // every partner, though none overlaps (the detector finds nothing)
        let (inputs, kinds) = underlined_inputs(true, false);
        assert_eq!(letters_touching(&inputs, 1000.0, &kinds, &[]), (13, 13));
        let (inputs, kinds) = underlined_inputs(false, false);
        assert_eq!(letters_touching(&inputs, 1000.0, &kinds, &[]), (0, 13));
        // a script's letters overlap at their joins, so they touch too
        let e = script_font(true);
        assert!(e.font_joins().len() >= 9);
    }

    #[test]
    fn tabular_figures_keep_their_sides_when_the_line_runs_through_them() {
        let (inputs, kinds) = underlined_inputs(true, true);
        let e = Engine::prepare_with_joins(inputs, 1000.0, 1, &JoinSetup { kinds, kerning: Vec::new(), keep: true }).unwrap();
        assert!(e.join_decorated());
        let sol = e.solve(&Settings::new()).unwrap();
        for i in 13..18 {
            assert_eq!((sol.lsb[i], sol.rsb[i]), (0.0, 0.0), "figure {i} keeps its sides (and its advance)");
        }
        let check = e.check_joins(&sol, None).unwrap();
        assert_eq!(check.broken, 0, "{check:?}");
    }

    #[test]
    fn a_fix_that_would_cross_the_strokes_is_flagged() {
        // a bracket whose arms reach right at the top and the bottom, a full
        // bar and a bar at the top only, each set 20 units after it
        let bracket = GlyphInput::simple(
            vec![rect(0.0, 0.0, 60.0, 500.0), rect(60.0, 440.0, 300.0, 500.0), rect(60.0, 0.0, 300.0, 60.0)],
            320.0,
            GROUP_LOWERCASE,
        );
        let bar = GlyphInput::simple(vec![rect(0.0, 0.0, 60.0, 500.0)], 60.0, GROUP_LOWERCASE);
        let top = GlyphInput::simple(vec![rect(0.0, 440.0, 60.0, 500.0)], 60.0, GROUP_LOWERCASE);
        let setup = JoinSetup { kinds: vec![JoinKind::Lower; 3], kerning: Vec::new(), keep: false };
        let e = Engine::prepare_with_joins(vec![bracket, bar, top], 1000.0, 1, &setup).unwrap();
        let d = e.check_pairs(&[(0, 1), (0, 2)], None, None);
        assert_eq!(d.len(), 2);
        for p in &d {
            assert!(!p.drawn.joins && (p.drawn.gap - 20.0).abs() <= 1.0, "{p:?}");
            assert!(p.drawn.fix < -19.0, "{p:?}");
        }
        // kerned to touch, the bar closes the white inside the bracket
        assert!(d[0].fix_crosses, "{:?}", d[0]);
        // the short bar meets the top arm only
        assert!(!d[1].fix_crosses, "{:?}", d[1]);
    }

    #[test]
    fn the_harness_leaves_frozen_glyphs_alone() {
        let e = font();
        let frozen = GlyphOpt { frozen: true, looseness: 0.0, intensity: 1.0 };
        let opts = vec![frozen, frozen, GlyphOpt::default(), GlyphOpt::default()];
        let base = Settings { glyph_opts: Some(opts), ..Settings::new() };
        let plain = e.solve(&base).unwrap();
        let with = e.solve(&Settings { harness: Some(harness()), ..base.clone() }).unwrap();
        // H and V are frozen: their sides stay, and so does the kerning between them
        for i in 0..2 {
            assert_eq!((with.lsb[i], with.rsb[i]), (plain.lsb[i], plain.rsb[i]));
        }
        assert_eq!(with.kerning(0, 1), plain.kerning(0, 1));
        // the period is not frozen: its side moves, and V–period is corrected
        assert!((with.lsb[3] - plain.lsb[3] + 6.0).abs() < 1e-9);
        assert!((with.kerning(1, 3) - plain.kerning(1, 3) + 20.0).abs() < 1e-6);
    }
}
