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
    physics_for_looseness, GlyphInput, GlyphOpt, SideRule, SolveOptions, COUPLING_CALIBRATION, GLYPH_FIXED_ADVANCE,
    GLYPH_KERN, GLYPH_RTL, GROUP_LOWERCASE, GROUP_OTHER, GROUP_UPPERCASE, LOOSENESS_GAIN, LOOSENESS_RATIO, MAX_GROUPS,
    NONE,
};
pub use crate::geometry::{Vec2, NODE_CURVE, NODE_LINE, NODE_OFFCURVE, NODE_QCURVE};
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

    /// One solve; blocks until done.
    pub fn solve(&self, s: &Settings) -> Result<Solution, String> {
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
        };
        let mask: Option<Vec<u8>> = s.kern_mask.as_ref().map(|m| m.iter().map(|&b| b as u8).collect());
        let progress = Progress::new();
        let out = run::run(ctx, &p, mask.as_deref(), &progress).map_err(|e| match e {
            crate::job::JobError::Cancelled => "cancelled".to_string(),
            crate::job::JobError::Failed(msg) => msg,
        })?;
        let classes = out.classes.as_ref().unwrap_or(&ctx.classes);
        let mut look = Lookup { classes: out.mode == Mode::Classes, ..Lookup::default() };
        let entries: Vec<Entry> = out
            .entries
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
        let m = &out.pass1.metrics;
        Ok(Solution {
            lsb: out.pass1.lsb.clone(),
            rsb: out.pass1.rsb.clone(),
            advance: m.iter().map(|x| x.advance).collect(),
            valid: m.iter().map(|x| x.valid).collect(),
            kerned: out.kern.clone(),
            fitted: if out.fitted.is_finite() { Some(s.looseness + out.fitted) } else { None },
            rest_gap: out.pass1.rest_gap,
            right_class: classes.right.class_of.clone(),
            left_class: classes.left.class_of.clone(),
            right_origin: classes.right.origin.clone(),
            left_origin: classes.left.origin.clone(),
            entries,
            look,
        })
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

#[derive(Default)]
struct Lookup {
    classes: bool,
    gg: HashMap<(u32, u32), f64>,
    gc: HashMap<(u32, u32), f64>,
    cg: HashMap<(u32, u32), f64>,
    cc: HashMap<(u32, u32), f64>,
}

/// The result of one solve.
pub struct Solution {
    pub lsb: Vec<f64>,
    pub rsb: Vec<f64>,
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
        let h = GlyphInput::simple(
            vec![rect(60.0, 0.0, 140.0, 700.0), rect(460.0, 0.0, 540.0, 700.0), rect(140.0, 320.0, 460.0, 390.0)],
            600.0,
            GROUP_UPPERCASE,
        );
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
