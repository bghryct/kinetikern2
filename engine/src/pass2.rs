//! Pass 2 — the pair kernel.
//!
//! The physics is v1's: a pair relaxes in a contour field (repulsion on the
//! Euclidean SDF distance, tension on the horizontal white) sampled by the
//! union of both glyphs' adaptive rays; the kerning is the interaction
//! contrast against flat probe walls, passed through a dead zone and a
//! saturating response, then raised by the clearance and crevice floors.
//!
//! What changed is how much work a pair costs:
//!
//! * the merged band is built into reusable buffers (no allocation per pair);
//! * one-sided rays farther (vertically) than the hard-core radius from the
//!   partner's ink contribute exactly zero and are skipped; pairs whose ink
//!   heights never meet skip the merge entirely;
//! * the floors are skipped when exact bounds prove they cannot bind;
//! * distance queries are skipped where a lower bound of the distance (from
//!   per-glyph window extremes, `Windows`) already reaches the cap, and a
//!   query close to an earlier one of the same ray reuses its nearest chunk
//!   when that provably stays nearest (`Near`) — both give the same value;
//! * the window-first solver decides signs of the force from per-ray bounds,
//!   refining only the most uncertain rays: the threshold window test (a
//!   pair inside it is 0, usually in two cheap decisions), then doubling
//!   steps and bisection down to a narrow bracket, then Illinois. Its root
//!   choice depends on signs only, so `verify` can retrace it exactly.
//!
//! With `Solver::Reference` and no threshold the kernel reproduces v1's
//! arithmetic exactly (same operations in the same order on the rays that
//! contribute), which the equivalence tool checks bit for bit.

use crate::engine::{Context, PreparedGlyph, SolveOptions, COUPLING_CALIBRATION};
use crate::geometry::Vec2;
use crate::job::{Cancelled, Progress};
use crate::physics::{clearance_floor, crevice_floor, equilibrium_gap, rest_gap, soft_clip, ContourField};
use crate::profile::{PairBand, SdfProfile, Side};

use rayon::prelude::*;

pub const PAIR_CLEARANCE: u32 = 1;
pub const PAIR_CREVICE: u32 = 2;
pub const PAIR_SATURATED: u32 = 4;
pub const PAIR_DISJOINT: u32 = 8;
/// Settled by the window test (below the threshold, two evaluations).
pub const PAIR_WINDOW: u32 = 16;
/// The force rose across the window (several equilibria): v1's search from
/// the Pass-1 gap decided.
pub const PAIR_FALLBACK: u32 = 32;
/// Settled by the force bounds alone (no distance queries).
pub const PAIR_BOUNDED: u32 = 64;
/// The facing sides join (a connected script): no kerning and no floors, the
/// joins overlap as drawn; Pass 1 spaced the two bodies.
pub const PAIR_JOIN: u32 = 128;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Solver {
    /// v1: bracket outward from the Pass-1 gap, then Illinois.
    Reference,
    /// Window first, then bracket outward from the window edge.
    Window,
}

/// Constants of the value pipeline, in font units.
#[derive(Clone, Copy, Debug)]
pub struct Knobs {
    pub coupling: f64,
    pub neg: f64,
    pub pos: f64,
    pub clearance: f64,
    pub tau: f64,
    pub dead_zone: f64,
    pub tol: f64,
    /// Results with |value| below this are zero (at least v1's 0.5 rounding).
    pub threshold: f64,
}

impl Knobs {
    pub fn new(o: &SolveOptions, upm: f64, threshold: f64) -> Self {
        let neg = o.max_negative_kern.max(1e-6);
        let pos = o.max_positive_kern.max(1e-6);
        Knobs {
            coupling: o.coupling * COUPLING_CALIBRATION,
            neg,
            pos,
            clearance: o.min_clearance,
            tau: o.crevice_pressure,
            dead_zone: o.min_kern.max(0.0),
            tol: 0.2 * upm / 1000.0,
            threshold: if threshold.is_finite() { threshold.max(0.5) } else { 0.5 },
        }
    }

    /// Interaction contrast → pre-floor value (dead zone, saturating response).
    #[inline]
    pub fn shape(&self, raw: f64) -> (f64, bool) {
        let driven = self.coupling * raw;
        let shrunk = driven.signum() * (driven.abs() - self.dead_zone).max(0.0);
        let saturated = shrunk < -self.neg || shrunk > self.pos;
        (soft_clip(shrunk, self.neg, self.pos), saturated)
    }

    /// The raw-contrast interval whose pre-floor values lie in [lo, hi]
    /// (the pipeline is monotone; its dead zone maps a whole interval to 0).
    pub fn raw_window(&self, lo: f64, hi: f64) -> (f64, f64) {
        let c = self.coupling.max(1e-12);
        // inverse of soft_clip on (-neg, pos)
        let inv = |p: f64| -> f64 {
            if p <= -self.neg {
                f64::NEG_INFINITY
            } else if p >= self.pos {
                f64::INFINITY
            } else if p < 0.0 {
                self.neg * (1.0 + p / self.neg).ln()
            } else {
                -self.pos * (1.0 - p / self.pos).ln()
            }
        };
        let lo_s = inv(lo);
        let hi_s = inv(hi);
        // inverse of the dead zone: the lowest / highest drive reaching a value
        let lo_d = if lo_s > 0.0 { lo_s + self.dead_zone } else { lo_s - self.dead_zone };
        let hi_d = if hi_s < 0.0 { hi_s - self.dead_zone } else { hi_s + self.dead_zone };
        (lo_d / c, hi_d / c)
    }
}

/// Contour field of every rhythm-group pair.
pub struct Fields {
    pub ng: usize,
    pub fields: Vec<ContourField>,
}

impl Fields {
    pub fn new(ctx: &Context, o: &SolveOptions) -> Self {
        let ng = ctx.rhythm_scale.len();
        let (spring, repulsion) = (o.spring.max(1e-9), o.repulsion.max(0.0));
        let fields = (0..ng * ng)
            .map(|k| {
                let lam = 0.5 * (ctx.rhythm_scale[k / ng] + ctx.rhythm_scale[k % ng]);
                let g_ref = rest_gap(lam, spring, repulsion).max(0.02 * ctx.upm);
                ContourField::new(g_ref, o.field_cap, o.field_decay, o.field_core)
            })
            .collect();
        Fields { ng, fields }
    }

    #[inline]
    pub fn get(&self, ga: usize, gb: usize) -> &ContourField {
        &self.fields[ga * self.ng + gb]
    }
}

/// Every glyph's own equilibrium against a flat wall spanning its zone, per
/// partner group: what its sidebearings already account for. NaN where not
/// needed.
pub struct Probes {
    pub ng: usize,
    pub right: Vec<f64>,
    pub left: Vec<f64>,
}

impl Probes {
    #[inline]
    pub fn right(&self, glyph: usize, partner_group: usize) -> f64 {
        self.right[glyph * self.ng + partner_group]
    }
    #[inline]
    pub fn left(&self, glyph: usize, partner_group: usize) -> f64 {
        self.left[glyph * self.ng + partner_group]
    }

    /// Probes of the glyphs flagged in `needed` against the groups flagged in
    /// `partners`. Same computation as v1.
    pub fn compute(
        ctx: &Context,
        fields: &Fields,
        needed: &[bool],
        partners: &[bool],
        progress: &Progress,
        unit: u64,
    ) -> Result<Probes, Cancelled> {
        let ng = fields.ng;
        let n = ctx.glyphs.len();
        let tol = 0.2 * ctx.upm / 1000.0;
        let rows: Vec<Option<(Vec<f64>, Vec<f64>)>> = (0..n)
            .into_par_iter()
            .map(|i| {
                let mut right = vec![f64::NAN; ng];
                let mut left = vec![f64::NAN; ng];
                let g = &ctx.glyphs[i];
                if !needed[i] || !g.valid {
                    return Some((right, left));
                }
                if progress.cancelled() {
                    return None;
                }
                let wall_l = SdfProfile::probe(Side::Left, g.zone.0, g.zone.1, 0.0);
                let wall_r = SdfProfile::probe(Side::Right, g.zone.0, g.zone.1, 0.0);
                let band_r = PairBand::merge(&g.right, &wall_l);
                let band_l = PairBand::merge(&wall_r, &g.left);
                for h in (0..ng).filter(|&h| partners[h]) {
                    let f = fields.get(g.group, h);
                    let (lo, hi) = (-g.bbox.width() - f.cap, f.g_ref + 4.0 * f.cap);
                    right[h] = equilibrium_gap(&g.right, &wall_l, &band_r, f, f.g_ref, tol, lo, hi).0;
                    let f = fields.get(h, g.group);
                    left[h] = equilibrium_gap(&wall_r, &g.left, &band_l, f, f.g_ref, tol, lo, hi).0;
                }
                progress.add(unit);
                Some((right, left))
            })
            .collect();
        let mut p = Probes { ng, right: vec![f64::NAN; n * ng], left: vec![f64::NAN; n * ng] };
        for (i, row) in rows.into_iter().enumerate() {
            let (r, l) = row.ok_or(Cancelled)?;
            p.right[i * ng..(i + 1) * ng].copy_from_slice(&r);
            p.left[i * ng..(i + 1) * ng].copy_from_slice(&l);
        }
        Ok(p)
    }
}

/// Every glyph side's ink extremes over vertical windows, per partner group
/// and bound level: for ray j, the lowest (left side) or highest (right
/// side) ink x over its rays within ±h of ray j's height, widened by one ray
/// on each side. A band height between rays j and j + 1 takes the extreme of
/// the two entries — their windows cover its own window and the polyline
/// between their rays — so the per-pair distance bounds need no sweep.
pub struct Windows {
    /// Per glyph: `[partner group][level][ray]` flattened (empty when unused).
    left: Vec<Vec<f64>>,
    right: Vec<Vec<f64>>,
}

impl Windows {
    /// Windows of the glyphs flagged in `needed` against the groups flagged in
    /// `partners` (the field of a glyph pair sets the window heights).
    pub fn compute(ctx: &Context, fields: &Fields, needed: &[bool], partners: &[bool]) -> Windows {
        let ng = fields.ng;
        let one = |p: &SdfProfile, right: bool, own: usize| -> Vec<f64> {
            let n = p.rays.len();
            let ys: Vec<f64> = p.rays.iter().map(|r| r.y).collect();
            // minima of x (left) or of −x (right); missing ink is +∞
            let xs: Vec<f64> = p
                .rays
                .iter()
                .map(|r| if !r.has_ink() { f64::INFINITY } else if right { -r.x } else { r.x })
                .collect();
            let mut out = vec![if right { f64::NEG_INFINITY } else { f64::INFINITY }; ng * LEVELS * n];
            let mut dq = vec![0u32; n];
            for h in (0..ng).filter(|&h| partners[h]) {
                let f = if right { fields.get(own, h) } else { fields.get(h, own) };
                for (k, r) in BOUND_LEVELS.iter().enumerate() {
                    let o = &mut out[(h * LEVELS + k) * n..(h * LEVELS + k + 1) * n];
                    window_min(&ys, &xs, r * f.g_ref, o, 1, &mut dq);
                    if right {
                        for v in o.iter_mut() {
                            *v = -*v;
                        }
                    }
                }
            }
            out
        };
        let rows: Vec<(Vec<f64>, Vec<f64>)> = (0..ctx.glyphs.len())
            .into_par_iter()
            .map(|i| {
                let g = &ctx.glyphs[i];
                if !needed[i] || !g.valid || g.left.rays.is_empty() || g.right.rays.is_empty() {
                    return (Vec::new(), Vec::new());
                }
                (one(&g.left, false, g.group), one(&g.right, true, g.group))
            })
            .collect();
        let (left, right) = rows.into_iter().unzip();
        Windows { left, right }
    }
}

/// Vertical window half-heights of the distance bounds, in rest gaps.
const BOUND_LEVELS: [f64; 3] = [0.05, 0.3, 1.2];
const LEVELS: usize = BOUND_LEVELS.len();

/// Per-thread buffers of the kernel.
#[derive(Default)]
pub struct Scratch {
    pub band: PairBand,
    active: Vec<u32>,
    near_a: Vec<f64>,
    near_b: Vec<f64>,
    /// Per band height and level: the lowest B x / highest A x within the
    /// vertical window (distance lower bounds without distance queries).
    bmin: Vec<f64>,
    amax: Vec<f64>,
    hs: [f64; LEVELS],
    /// Per active ray: what its bounds need, offset-free (see `RayConst`).
    consts: Vec<RayConst>,
    /// Per active ray side (A's point, B's point): the last distance query.
    near: Vec<Near>,
    /// The pair the band and bounds above belong to (a `verify` that found
    /// the pair differing leaves them for its `solve`).
    prepared: Option<(usize, usize)>,
    /// Per active ray: bounds of its contribution, and the refinement order.
    rlo: Vec<f64>,
    rhi: Vec<f64>,
    order: Vec<u64>,
}

/// Sliding-window minimum of `xs` (no NaN; missing ink is +∞) over the
/// sorted heights `ys` within ±h of each height, widened by one sample on
/// each side (so the polyline between samples is covered). Writes
/// `out[i * stride]`. `dq` is a deque of indices with room for every sample.
fn window_min(ys: &[f64], xs: &[f64], h: f64, out: &mut [f64], stride: usize, dq: &mut [u32]) {
    let n = ys.len();
    let (mut head, mut tail) = (0usize, 0usize);
    let mut lo = 0usize;
    let mut next = 0usize; // next index to push
    for i in 0..n {
        let y = ys[i];
        while ys[lo] < y - h {
            lo += 1;
        }
        let start = lo.saturating_sub(1);
        // the last height ≤ y + h is at least i; push through one past it
        while next < n && (next <= i || ys[next - 1] <= y + h) {
            let v = xs[next];
            while tail > head && xs[dq[tail - 1] as usize] >= v {
                tail -= 1;
            }
            dq[tail] = next as u32;
            tail += 1;
            next += 1;
        }
        // the newest index is ≥ i ≥ start, so the deque never runs empty
        while (dq[head] as usize) < start {
            head += 1;
        }
        out[i * stride] = xs[dq[head] as usize];
    }
}

/// One pair's outcome.
#[derive(Clone, Copy, Debug, Default)]
pub struct PairOut {
    /// Final kerning (floors applied, |value| < 0.5 → 0).
    pub value: f64,
    /// Value before the floors.
    pub pre: f64,
    /// Interaction contrast.
    pub raw: f64,
    pub flags: u32,
    pub evals: u32,
    /// Merged rays of the band and the ones that contribute.
    pub rays: u32,
    pub active: u32,
    /// Mean horizontal white over facing rays at the final gap (importance).
    pub white: f64,
}

/// Outcome of checking a pair against a value it is meant to share.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Verify {
    /// Its own pre-floor value is within the threshold of the target.
    Within,
    /// It needs its own solve.
    Differs,
}

/// Everything a pair evaluation reads.
pub struct Kernel<'a> {
    pub ctx: &'a Context,
    pub fields: &'a Fields,
    pub probes: &'a Probes,
    pub windows: &'a Windows,
    pub lsb: &'a [f64],
    pub rsb: &'a [f64],
    pub knobs: Knobs,
    pub solver: Solver,
    /// Per-glyph kerning force (multiple of the coupling); None = all 1.
    pub force: Option<&'a [f64]>,
}

/// Vertical distance from every band height to the nearest height where the
/// other side has ink (∞ if none), via two sweeps.
fn nearest_ink(ys: &[f64], xs: &[f64], out: &mut Vec<f64>) {
    let n = ys.len();
    out.clear();
    out.resize(n, f64::INFINITY);
    let mut last = f64::NEG_INFINITY;
    for i in 0..n {
        if xs[i].is_finite() {
            last = ys[i];
        }
        out[i] = ys[i] - last;
    }
    let mut next = f64::INFINITY;
    for i in (0..n).rev() {
        if xs[i].is_finite() {
            next = ys[i];
        }
        out[i] = out[i].min(next - ys[i]);
    }
}

/// The last exact distance query of one ray side: at gap `s`, the profile's
/// chunk `chunk` held the nearest piece (`u32::MAX`: none within the cap) and
/// every piece outside it was at least `other` away. A query point moves by |Δs|
/// when the gap does, and so does every distance; at a nearby gap the walk is
/// skipped while that chunk stays strictly nearest, or while nothing can come
/// within the cap.
#[derive(Clone, Copy, Debug)]
struct Near {
    s: f64,
    chunk: u32,
    other: f64,
}

const NEAR_UNSET: Near = Near { s: f64::NAN, chunk: u32::MAX, other: f64::NEG_INFINITY };
/// Absolute margin (font units) against rounding of the query points.
const NEAR_MARGIN: f64 = 1e-6;

/// `prof.distance(p, cap)` for the query point at gap `s`, from the slot's
/// last query when that proves the answer (the same value either way).
#[inline]
fn query(prof: &SdfProfile, p: Vec2, cap: f64, s: f64, slot: &mut Near) -> f64 {
    let room = slot.other - (s - slot.s).abs() - NEAR_MARGIN;
    if room > 0.0 {
        if slot.chunk != u32::MAX {
            let d = prof.chunk_distance(slot.chunk, p);
            if d < room {
                return cap.min(d);
            }
            if cap <= room && cap <= d {
                return cap;
            }
        } else if cap <= room {
            return cap;
        }
    }
    let (d, chunk, other) = prof.distance_near(p, cap);
    *slot = Near { s, chunk, other };
    d
}

/// v1's net contour force restricted to the rays that can contribute
/// (identical arithmetic, identical order). A distance query is skipped when
/// the distance bound already reaches the query's cap: the query would
/// return the cap itself, so the result is unchanged bit for bit.
#[inline]
fn force_at(a: &SdfProfile, b: &SdfProfile, sc: &mut Scratch, s: f64, f: &ContourField) -> f64 {
    let mut near = std::mem::take(&mut sc.near);
    let band = &sc.band;
    let dx = a.extreme - b.extreme + s;
    let mut total = 0.0;
    let mut xs = [0.0f64; LEVELS];
    for (j, &k) in sc.active.iter().enumerate() {
        let i = k as usize;
        let y = band.ys[i];
        let (xa, xb) = (band.xa[i], band.xb[i]);
        let facing = xa.is_finite() && xb.is_finite();
        let g = if facing { xb + dx - xa } else { f64::NAN };
        let reach = if facing { f.search.min(g.abs() + 1e-9) } else { f.core_radius };
        if band.wa[i] > 0.0 {
            let p = Vec2::new(xa - dx, y);
            for q in 0..LEVELS {
                xs[q] = sc.bmin[i * LEVELS + q] + dx - xa;
            }
            let dist = if distance_floor(&xs, &sc.hs) >= reach { reach } else { query(b, p, reach, s, &mut near[2 * j]) };
            let d = if facing && p.x > xb { -dist } else { dist };
            total += band.wa[i] * f.force(d, g, facing);
        }
        if band.wb[i] > 0.0 {
            let q = Vec2::new(xb + dx, y);
            for l in 0..LEVELS {
                xs[l] = xb + dx - sc.amax[i * LEVELS + l];
            }
            let dist = if distance_floor(&xs, &sc.hs) >= reach { reach } else { query(a, q, reach, s, &mut near[2 * j + 1]) };
            let d = if facing && q.x < xa { -dist } else { dist };
            total += band.wb[i] * f.force(d, g, facing);
        }
    }
    sc.near = near;
    total
}

/// Illinois regula falsi inside a bracket force(lo) ≥ 0 ≥ force(hi) (v1).
fn illinois(
    force: &mut impl FnMut(f64) -> f64,
    mut lo: f64,
    mut flo: f64,
    mut hi: f64,
    mut fhi: f64,
    tol: f64,
    ftol: f64,
) -> f64 {
    let mut last = 0i8;
    for _ in 0..60 {
        if hi - lo <= tol {
            break;
        }
        let mut m = (lo * fhi - hi * flo) / (fhi - flo);
        if !(m > lo && m < hi) {
            m = 0.5 * (lo + hi);
        }
        let fm = force(m);
        if fm.abs() <= ftol {
            return m;
        }
        if fm > 0.0 {
            lo = m;
            flo = fm;
            if last == 1 {
                fhi *= 0.5;
            }
            last = 1;
        } else {
            hi = m;
            fhi = fm;
            if last == -1 {
                flo *= 0.5;
            }
            last = -1;
        }
    }
    0.5 * (lo + hi)
}

/// Searches upward from `(lo, flo > 0)` for a sign change (v1's doubling
/// steps), then refines.
#[allow(clippy::too_many_arguments)]
fn search_up(force: &mut impl FnMut(f64) -> f64, lo0: f64, flo0: f64, step0: f64, tol: f64, ftol: f64, s_max: f64) -> f64 {
    let (mut lo, mut flo) = (lo0, flo0);
    let mut step = step0;
    let mut s = lo0;
    let (hi, fhi);
    loop {
        s += step;
        if s >= s_max {
            let fm = force(s_max);
            if fm > 0.0 {
                return s_max;
            }
            hi = s_max;
            fhi = fm;
            break;
        }
        let fs = force(s);
        if fs <= 0.0 {
            hi = s;
            fhi = fs;
            break;
        }
        lo = s;
        flo = fs;
        step *= 2.0;
    }
    illinois(force, lo, flo, hi, fhi, tol, ftol)
}

/// Searches downward from `(hi, fhi < 0)`.
#[allow(clippy::too_many_arguments)]
fn search_down(force: &mut impl FnMut(f64) -> f64, hi0: f64, fhi0: f64, step0: f64, tol: f64, ftol: f64, s_min: f64) -> f64 {
    let (mut hi, mut fhi) = (hi0, fhi0);
    let mut step = step0;
    let mut s = hi0;
    let (lo, flo);
    loop {
        s -= step;
        if s <= s_min {
            let fm = force(s_min);
            if fm < 0.0 {
                return s_min;
            }
            lo = s_min;
            flo = fm;
            break;
        }
        let fs = force(s);
        if fs >= 0.0 {
            lo = s;
            flo = fs;
            break;
        }
        hi = s;
        fhi = fs;
        step *= 2.0;
    }
    illinois(force, lo, flo, hi, fhi, tol, ftol)
}

/// v1's `equilibrium_gap` from `s0` over an arbitrary force.
fn reference_root(force: &mut impl FnMut(f64) -> f64, s0: f64, g_ref: f64, tol: f64, ftol: f64, s_min: f64, s_max: f64) -> f64 {
    let s0 = s0.clamp(s_min, s_max);
    let f0 = force(s0);
    if f0.abs() <= ftol {
        return s0;
    }
    let step = 0.25 * g_ref.max(4.0 * tol);
    if f0 < 0.0 {
        search_down(force, s0, f0, step, tol, ftol, s_min)
    } else {
        search_up(force, s0, f0, step, tol, ftol, s_max)
    }
}

/// Advances `j` to the last ray at or below `y` and returns the rays whose
/// window entries cover height `y`: `(j, j)` on a ray or past either end,
/// else `(j, j + 1)`.
#[inline]
fn covering(rays: &[crate::profile::Ray], j: &mut usize, y: f64) -> (usize, usize) {
    while *j + 1 < rays.len() && rays[*j + 1].y <= y {
        *j += 1;
    }
    let k = *j;
    if rays[k].y == y || k + 1 == rays.len() || y < rays[k].y {
        (k, k)
    } else {
        (k, k + 1)
    }
}

/// Lower bound of a distance from the horizontal clearances `x[k]` within
/// the vertical windows `hs[k]`: points inside a window are at least x[k]
/// away, points outside at least h[k].
#[inline]
fn distance_floor(x: &[f64], hs: &[f64; LEVELS]) -> f64 {
    let mut best = 0.0f64;
    for k in 0..LEVELS {
        let xk = x[k].max(0.0);
        best = best.max(hs[k].min(xk));
    }
    best
}

/// e^−x on a grid of 1/64 from x = −20 (the repulsion's clamp) to 60,
/// raised by a hair against rounding of the index: e^−x ≤ table[⌊64(x+20)⌋].
const EXP_GRID: f64 = 64.0;
const EXP_TABLE_LEN: usize = 80 * 64 + 1;

fn exp_table() -> &'static [f64] {
    static TABLE: std::sync::OnceLock<Vec<f64>> = std::sync::OnceLock::new();
    TABLE.get_or_init(|| (0..EXP_TABLE_LEN).map(|k| (20.0 - k as f64 / EXP_GRID).exp() * (1.0 + 1e-12)).collect())
}

/// Lower and upper bounds of `f.repulsion(d)` within about 1e-4: the
/// tangent and the chord of e^−x on its grid cell (e^−x is convex). Past the
/// table, between 0 and its last entry.
#[inline]
fn repulsion_between(f: &ContourField, d: f64, table: &[f64]) -> (f64, f64) {
    let x = (d / f.lambda).max(-20.0);
    let v = (x + 20.0) * EXP_GRID;
    if !(v < (EXP_TABLE_LEN - 1) as f64) {
        return (0.0, f.k_c * table[EXP_TABLE_LEN - 1]);
    }
    let k = v as usize;
    let u = v - k as f64;
    let (t0, t1) = (table[k], table[k + 1]);
    let hi = t0 + u * (t1 - t0);
    let lo = t1 * (1.0 + (1.0 - u) / EXP_GRID) * (1.0 - 1e-11);
    (f.k_c * lo, f.k_c * hi * (1.0 + 1e-11))
}

/// A quick upper bound of `f.repulsion(d)` (at most 1.6 % above it), for
/// distances only known by their floor.
#[inline]
fn repulsion_hi(f: &ContourField, d: f64, table: &[f64]) -> f64 {
    let x = (d / f.lambda).max(-20.0);
    let k = (((x + 20.0) * EXP_GRID) as usize).min(EXP_TABLE_LEN - 1);
    f.k_c * table[k]
}

/// One active ray of the band with its bound constants: at the offset `dx`
/// the horizontal white is `gx + dx` and the clearances of the distance
/// bounds are `ca[k] + dx` (A's point against B's ink in the window `k`)
/// and `cb[k] + dx` (B's point against A's ink).
#[derive(Clone, Copy, Debug, Default)]
struct RayConst {
    gx: f64,
    wa: f64,
    wb: f64,
    facing: bool,
    ca: [f64; LEVELS],
    cb: [f64; LEVELS],
}

/// Fills `sc.consts` for the active rays (after the band's window minima).
fn prepare_consts(sc: &mut Scratch) {
    let band = &sc.band;
    sc.consts.clear();
    for &k in &sc.active {
        let i = k as usize;
        let (xa, xb) = (band.xa[i], band.xb[i]);
        let mut c = RayConst { gx: xb - xa, wa: band.wa[i], wb: band.wb[i], facing: xa.is_finite() && xb.is_finite(), ..RayConst::default() };
        for q in 0..LEVELS {
            c.ca[q] = sc.bmin[i * LEVELS + q] - xa;
            c.cb[q] = xb - sc.amax[i * LEVELS + q];
        }
        sc.consts.push(c);
    }
}

/// `distance_floor` of the clearances `c[k] + dx`.
#[inline]
fn floor_at(c: &[f64; LEVELS], dx: f64, hs: &[f64; LEVELS]) -> f64 {
    let mut best = 0.0f64;
    for k in 0..LEVELS {
        best = best.max(hs[k].min((c[k] + dx).max(0.0)));
    }
    best
}

/// Bounds (lower, upper) of one ray's contribution to `force_at` at the
/// offset `dx`: the same terms with every Euclidean distance replaced by a
/// bound, in O(1), no distance queries.
#[inline]
fn ray_bounds(c: &RayConst, hs: &[f64; LEVELS], dx: f64, f: &ContourField, table: &[f64]) -> (f64, f64) {
    let (mut lo, mut hi) = (0.0, 0.0);
    if c.facing {
        let g = c.gx + dx;
        let reach = f.search.min(g.abs() + 1e-9);
        let t = f.tension(g);
        let w = c.wa + c.wb;
        if g < 0.0 {
            return (w * (f.k_c - t), w * (repulsion_hi(f, -reach, table) - t));
        }
        // the partner's point at the same height bounds the distance from
        // above; a side whose floor reaches that far is exact
        let (e_lo, e_hi) = repulsion_between(f, reach, table);
        lo += w * (e_lo - t);
        for (wk, ck) in [(c.wa, &c.ca), (c.wb, &c.cb)] {
            if wk > 0.0 {
                let d = floor_at(ck, dx, hs);
                let rep = if d >= reach { e_hi } else { repulsion_hi(f, d, table) };
                hi += wk * (rep - t);
            }
        }
    } else {
        // a one-sided ray's distance is capped at the core radius, where its
        // force is exactly zero
        for (wk, ck) in [(c.wa, &c.ca), (c.wb, &c.cb)] {
            if wk > 0.0 {
                let d = floor_at(ck, dx, hs);
                if d < f.core_radius {
                    hi += wk * (repulsion_hi(f, d, table) - f.core).max(0.0);
                }
            }
        }
    }
    (lo, hi)
}

/// One ray's exact contribution (the per-ray terms of `force_at`).
#[inline]
#[allow(clippy::too_many_arguments)]
fn ray_exact(a: &SdfProfile, b: &SdfProfile, sc: &Scratch, near: &mut [Near], j: usize, s: f64, dx: f64, f: &ContourField) -> f64 {
    let band = &sc.band;
    let i = sc.active[j] as usize;
    let y = band.ys[i];
    let (xa, xb) = (band.xa[i], band.xb[i]);
    let facing = xa.is_finite() && xb.is_finite();
    let g = if facing { xb + dx - xa } else { f64::NAN };
    let reach = if facing { f.search.min(g.abs() + 1e-9) } else { f.core_radius };
    let mut xs = [0.0f64; LEVELS];
    let mut total = 0.0;
    if band.wa[i] > 0.0 {
        let p = Vec2::new(xa - dx, y);
        for q in 0..LEVELS {
            xs[q] = sc.bmin[i * LEVELS + q] + dx - xa;
        }
        let dist = if distance_floor(&xs, &sc.hs) >= reach { reach } else { query(b, p, reach, s, &mut near[2 * j]) };
        let d = if facing && p.x > xb { -dist } else { dist };
        total += band.wa[i] * f.force(d, g, facing);
    }
    if band.wb[i] > 0.0 {
        let q = Vec2::new(xb + dx, y);
        for l in 0..LEVELS {
            xs[l] = xb + dx - sc.amax[i * LEVELS + l];
        }
        let dist = if distance_floor(&xs, &sc.hs) >= reach { reach } else { query(a, q, reach, s, &mut near[2 * j + 1]) };
        let d = if facing && q.x < xa { -dist } else { dist };
        total += band.wb[i] * f.force(d, g, facing);
    }
    total
}

/// Decides `F(s) ≥ thresh` (`ge`) or `F(s) ≤ thresh` (`!ge`) with as few
/// distance queries as possible: per-ray bounds first, then the most
/// uncertain rays are replaced by their exact contributions until the
/// comparison is settled. Returns the answer and how many rays were refined.
fn decide(a: &SdfProfile, b: &SdfProfile, sc: &mut Scratch, s: f64, f: &ContourField, ge: bool, thresh: f64) -> (bool, usize) {
    let dx = a.extreme - b.extreme + s;
    let n = sc.active.len();
    sc.rlo.clear();
    sc.rhi.clear();
    let (mut lo, mut hi) = (0.0, 0.0);
    let table = exp_table();
    for c in &sc.consts {
        let (l, h) = ray_bounds(c, &sc.hs, dx, f, table);
        sc.rlo.push(l);
        sc.rhi.push(h);
        lo += l;
        hi += h;
    }
    let verdict = |lo: f64, hi: f64| -> Option<bool> {
        if ge {
            if lo >= thresh {
                Some(true)
            } else if hi < thresh {
                Some(false)
            } else {
                None
            }
        } else if hi <= thresh {
            Some(true)
        } else if lo > thresh {
            Some(false)
        } else {
            None
        }
    };
    if let Some(v) = verdict(lo, hi) {
        return (v, 0);
    }
    // most uncertain first: the uncertainty's bits (positive, so ordered like
    // the values) above the ray index in one sortable key
    sc.order.clear();
    for j in 0..n {
        let u = sc.rhi[j] - sc.rlo[j];
        if u > 0.0 {
            sc.order.push(((u.to_bits() >> 20) << 20) | j as u64);
        }
    }
    sc.order.sort_unstable_by(|x, y| y.cmp(x));
    let mut near = std::mem::take(&mut sc.near);
    let scr: &Scratch = sc;
    let mut out = None;
    for (count, &key) in scr.order.iter().enumerate() {
        let j = (key & 0xF_FFFF) as usize;
        let e = ray_exact(a, b, scr, &mut near, j, s, dx, f);
        lo += e - scr.rlo[j];
        hi += e - scr.rhi[j];
        if let Some(v) = verdict(lo, hi) {
            out = Some((v, count + 1));
            break;
        }
    }
    let out = out.unwrap_or_else(|| {
        let exact = 0.5 * (lo + hi);
        (verdict(exact, exact).unwrap_or(ge), scr.order.len())
    });
    sc.near = near;
    out
}

/// Where the window solver's sign phase ends.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Walk {
    /// The pre-floor value lies inside the threshold window: it is 0.
    Window,
    /// The force keeps its sign up to this gap limit.
    Pinned(f64),
    /// force(lo) ≥ 0 ≥ force(hi), and the root is refined in there.
    Bracket(f64, f64),
    /// The force rises across the window (several equilibria).
    Fallback,
}

/// Limits of one pair's window walk (gaps).
struct WalkLimits {
    w0: (f64, f64),
    s_min: f64,
    s_max: f64,
    ftol: f64,
    /// The bisection stops at this bracket width.
    width: f64,
}

/// The window solver's sign phase: the threshold window test, then doubling
/// steps away from the window in the direction of the force until it changes
/// sign, then bisection of that bracket down to `lim.width`. Only signs of
/// the force are needed (`decide`), so `verify` can retrace it exactly;
/// `stop(lo, hi)` is asked after every narrowing of the bracket and ends the
/// walk early. Adds the rays it refined to `refined`.
fn walk(
    a: &SdfProfile,
    b: &SdfProfile,
    sc: &mut Scratch,
    f: &ContourField,
    lim: &WalkLimits,
    step0: f64,
    refined: &mut usize,
    mut stop: impl FnMut(f64, f64) -> bool,
) -> Walk {
    let mut sign = |sc: &mut Scratch, s: f64, ge: bool, thresh: f64| {
        let (v, r) = decide(a, b, sc, s, f, ge, thresh);
        *refined += r;
        v
    };
    let (wlo, whi) = lim.w0;
    let ge = sign(sc, wlo, true, -lim.ftol);
    let le = ge && sign(sc, whi, false, lim.ftol);
    if ge && le {
        return Walk::Window;
    }
    let (mut lo, mut hi) = if ge {
        // pushed apart past the window: step up from its upper edge
        if !sign(sc, wlo, true, 0.0) {
            return Walk::Fallback;
        }
        let (mut prev, mut s, mut step) = (whi, whi, step0);
        loop {
            s += step;
            if s >= lim.s_max {
                if !sign(sc, lim.s_max, false, 0.0) {
                    return Walk::Pinned(lim.s_max);
                }
                break (prev, lim.s_max);
            }
            if sign(sc, s, false, 0.0) {
                break (prev, s);
            }
            prev = s;
            step *= 2.0;
        }
    } else {
        // pulled together past the window: step down from its lower edge
        if !sign(sc, whi, false, 0.0) {
            return Walk::Fallback;
        }
        let (mut prev, mut s, mut step) = (wlo, wlo, step0);
        loop {
            s -= step;
            if s <= lim.s_min {
                if !sign(sc, lim.s_min, true, 0.0) {
                    return Walk::Pinned(lim.s_min);
                }
                break (lim.s_min, prev);
            }
            if sign(sc, s, true, 0.0) {
                break (s, prev);
            }
            prev = s;
            step *= 2.0;
        }
    };
    while !stop(lo, hi) && hi - lo > lim.width {
        let mid = 0.5 * (lo + hi);
        if sign(sc, mid, true, 0.0) {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    Walk::Bracket(lo, hi)
}

impl<'a> Kernel<'a> {
    /// Merges the band and lists the rays that can contribute. Returns false
    /// when no height has ink on both sides (the pair has nothing to balance).
    fn band(&self, a: &PreparedGlyph, b: &PreparedGlyph, f: &ContourField, sc: &mut Scratch) -> bool {
        sc.prepared = None;
        let meet = a.ink_right.0 <= b.ink_left.1 && b.ink_left.0 <= a.ink_right.1;
        if !meet {
            sc.band.ys.clear();
            sc.active.clear();
            return false;
        }
        sc.band.merge_into(&a.right, &b.left);
        let band = &sc.band;
        let facing = band.xa.iter().zip(&band.xb).any(|(x, y)| x.is_finite() && y.is_finite());
        if !facing {
            sc.active.clear();
            return false;
        }
        nearest_ink(&band.ys, &band.xb, &mut sc.near_b);
        nearest_ink(&band.ys, &band.xa, &mut sc.near_a);
        sc.active.clear();
        for i in 0..band.len() {
            let facing = band.xa[i].is_finite() && band.xb[i].is_finite();
            // a one-sided ray's distance is capped at the core radius, where its
            // force is exactly zero: rays that far from the partner's ink drop out
            let from_a = band.wa[i] > 0.0 && (facing || sc.near_b[i] < f.core_radius);
            let from_b = band.wb[i] > 0.0 && (facing || sc.near_a[i] < f.core_radius);
            if from_a || from_b {
                sc.active.push(i as u32);
            }
        }
        true
    }

    /// Prepares the distance bounds of the current band from the glyphs'
    /// window extremes.
    fn prepare_bounds(&self, ia: usize, ib: usize, f: &ContourField, sc: &mut Scratch) {
        let (a, b) = (&self.ctx.glyphs[ia], &self.ctx.glyphs[ib]);
        let n = sc.band.len();
        for (k, r) in BOUND_LEVELS.iter().enumerate() {
            sc.hs[k] = r * f.g_ref;
        }
        sc.bmin.resize(n * LEVELS, 0.0);
        sc.amax.resize(n * LEVELS, 0.0);
        let (ra, rb) = (&a.right.rays, &b.left.rays);
        let (wa, wb) = (&self.windows.right[ia], &self.windows.left[ib]);
        let (na, nb) = (ra.len(), rb.len());
        let wa = &wa[b.group * LEVELS * na..(b.group + 1) * LEVELS * na];
        let wb = &wb[a.group * LEVELS * nb..(a.group + 1) * LEVELS * nb];
        let (mut ja, mut jb) = (0usize, 0usize);
        for i in 0..n {
            let y = sc.band.ys[i];
            let (a0, a1) = covering(ra, &mut ja, y);
            let (b0, b1) = covering(rb, &mut jb, y);
            for k in 0..LEVELS {
                sc.amax[i * LEVELS + k] = wa[k * na + a0].max(wa[k * na + a1]);
                sc.bmin[i * LEVELS + k] = wb[k * nb + b0].min(wb[k * nb + b1]);
            }
        }
        prepare_consts(sc);
        sc.near.clear();
        sc.near.resize(2 * sc.active.len(), NEAR_UNSET);
        sc.prepared = Some((ia, ib));
    }

    #[inline]
    fn macro_gap(&self, ia: usize, ib: usize) -> f64 {
        self.rsb[ia] + self.lsb[ib]
    }

    /// The value pipeline of pair (ia, ib): the coupling scaled by the mean
    /// kerning force of its two glyphs' spacing groups.
    #[inline]
    pub fn knobs_for(&self, ia: usize, ib: usize) -> Knobs {
        match self.force {
            Some(f) => {
                let m = 0.5 * (f[ia] + f[ib]);
                if (m - 1.0).abs() < 1e-12 {
                    self.knobs
                } else {
                    Knobs { coupling: self.knobs.coupling * m.max(0.0), ..self.knobs }
                }
            }
            None => self.knobs,
        }
    }

    /// The gap window (absolute) inside which the pre-floor value lies in
    /// [lo, hi], for the pair's probes.
    fn gap_window(&self, ia: usize, ib: usize, f: &ContourField, lo: f64, hi: f64) -> (f64, f64, f64) {
        let (a, b) = (&self.ctx.glyphs[ia], &self.ctx.glyphs[ib]);
        let s_star = self.probes.right(ia, b.group) + self.probes.left(ib, a.group) - f.g_ref;
        let (rlo, rhi) = self.knobs_for(ia, ib).raw_window(lo, hi);
        (s_star + rlo, s_star + rhi, s_star)
    }

    /// Full evaluation of one ordered pair.
    pub fn solve(&self, ia: usize, ib: usize, sc: &mut Scratch) -> PairOut {
        let (a, b) = (&self.ctx.glyphs[ia], &self.ctx.glyphs[ib]);
        if a.joins(b) {
            return PairOut { flags: PAIR_JOIN, ..PairOut::default() };
        }
        let f = self.fields.get(a.group, b.group);
        let macro_gap = self.macro_gap(ia, ib);
        let kk = self.knobs_for(ia, ib);
        let k = &kk;
        let mut out = PairOut::default();
        let ready = sc.prepared == Some((ia, ib));
        if ready || self.band(a, b, f, sc) {
            out.rays = sc.band.len() as u32;
            out.active = sc.active.len() as u32;
            let weight: f64 = sc.band.wa.iter().chain(&sc.band.wb).sum();
            let ftol = 1e-6 * weight.max(1e-9);
            let s_min = -(a.bbox.width() + b.bbox.width()) - f.cap;
            let s_max = macro_gap.max(f.g_ref) + 4.0 * f.cap;
            let probe_a = self.probes.right(ia, b.group);
            let probe_b = self.probes.left(ib, a.group);
            if !ready {
                self.prepare_bounds(ia, ib, f, sc);
            }
            let mut evals = 0u32;
            let mut walked = None;
            if self.solver == Solver::Window {
                // Window first: settle "below the threshold" and find the root's
                // bracket from signs of the force alone (few distance queries)
                let lim = self.walk_limits(ia, ib, f, s_min, s_max, ftol);
                let mut refined = 0usize;
                let w = walk(&a.right, &b.left, sc, f, &lim, self.step0(f), &mut refined, |_, _| false);
                evals += refined.div_ceil(sc.active.len().max(1)) as u32;
                if w == Walk::Window && refined == 0 {
                    out.flags |= PAIR_BOUNDED;
                }
                walked = Some(w);
            }
            let mut force = |s: f64| {
                evals += 1;
                force_at(&a.right, &b.left, sc, s, f)
            };
            let s_ab = match walked {
                Some(Walk::Window) => {
                    out.flags |= PAIR_WINDOW;
                    None
                }
                Some(Walk::Pinned(s)) => Some(s),
                Some(Walk::Bracket(lo, hi)) if hi - lo <= k.tol => Some(0.5 * (lo + hi)),
                Some(Walk::Bracket(lo, hi)) => {
                    let (flo, fhi) = (force(lo), force(hi));
                    Some(illinois(&mut force, lo, flo, hi, fhi, k.tol, ftol))
                }
                Some(Walk::Fallback) => {
                    out.flags |= PAIR_FALLBACK;
                    Some(reference_root(&mut force, macro_gap, f.g_ref, k.tol, ftol, s_min, s_max))
                }
                None => Some(reference_root(&mut force, macro_gap, f.g_ref, k.tol, ftol, s_min, s_max)),
            };
            out.evals = evals;
            match s_ab {
                Some(s_ab) => {
                    // interaction contrast: whatever the flat probes explain is the sidebearings' job
                    out.raw = s_ab - probe_a - probe_b + f.g_ref;
                    let (pre, saturated) = k.shape(out.raw);
                    out.pre = pre;
                    if saturated {
                        out.flags |= PAIR_SATURATED;
                    }
                }
                None => {
                    out.raw = 0.0;
                    out.pre = 0.0;
                }
            }
        } else {
            out.flags |= PAIR_DISJOINT;
        }
        let (value, fl) = self.floors(a, b, macro_gap, out.pre);
        out.flags |= fl;
        out.value = if value.abs() < 0.5 || !value.is_finite() { 0.0 } else { value };
        if out.value != 0.0 && !sc.band.is_empty() {
            out.white = self.mean_white(a, b, macro_gap + out.value, &sc.band);
        }
        out
    }

    /// Doubling step of the window walk.
    #[inline]
    fn step0(&self, f: &ContourField) -> f64 {
        0.25 * f.g_ref.max(4.0 * self.knobs.tol)
    }

    /// The pair's threshold window and walk limits.
    fn walk_limits(&self, ia: usize, ib: usize, f: &ContourField, s_min: f64, s_max: f64, ftol: f64) -> WalkLimits {
        let kk = self.knobs_for(ia, ib);
        let k = &kk;
        let (wlo, whi, _) = self.gap_window(ia, ib, f, -k.threshold, k.threshold);
        WalkLimits {
            w0: (wlo.clamp(s_min, s_max), whi.clamp(s_min, s_max)),
            s_min,
            s_max,
            ftol,
            // a quarter of a kerned value's threshold window, at least 4 tolerances
            // the threshold in gaps: wide enough to leave most of the narrowing
            // to Illinois, narrow enough to fit a kerned value's window
            width: (k.threshold / k.coupling.max(1e-9)).max(4.0 * k.tol),
        }
    }

    /// Checks whether pair (ia, ib), solved on its own, would land within the
    /// threshold of `target` *and* on the same side of the threshold (kept or
    /// dropped alike). Retraces the window solver's sign phase: the pair's
    /// value is within when the solver's final bracket lies inside the target's
    /// window (the refinement never leaves the bracket). Returns the verdict,
    /// the force evaluations it cost and whether the force bounds alone decided.
    pub fn verify(&self, ia: usize, ib: usize, target: f64, sc: &mut Scratch) -> (Verify, u32, bool) {
        let (a, b) = (&self.ctx.glyphs[ia], &self.ctx.glyphs[ib]);
        if a.joins(b) {
            // a join pair is 0, as its class representative (classes never mix joins)
            return (if target.abs() < self.knobs.threshold { Verify::Within } else { Verify::Differs }, 0, true);
        }
        let f = self.fields.get(a.group, b.group);
        let t = self.knobs.threshold;
        if self.solver != Solver::Window {
            return (Verify::Differs, 0, false);
        }
        let middle = target > -t && target < t;
        // a hair inside the window, against rounding in the value pipeline
        let margin = 1e-6 * self.ctx.upm;
        let (lo, hi) = if target >= t {
            ((target - t).max(t) + margin, target + t - margin)
        } else if target <= -t {
            (target - t + margin, (target + t).min(-t) - margin)
        } else {
            (-t, t)
        };
        if !self.band(a, b, f, sc) {
            // nothing to balance: its pre-floor value is 0
            return (if middle { Verify::Within } else { Verify::Differs }, 0, false);
        }
        let weight: f64 = sc.band.wa.iter().chain(&sc.band.wb).sum();
        let ftol = 1e-6 * weight.max(1e-9);
        let macro_gap = self.macro_gap(ia, ib);
        let s_min = -(a.bbox.width() + b.bbox.width()) - f.cap;
        let s_max = macro_gap.max(f.g_ref) + 4.0 * f.cap;
        let lim = self.walk_limits(ia, ib, f, s_min, s_max, ftol);
        let (vlo, vhi, _) = self.gap_window(ia, ib, f, lo, hi);
        self.prepare_bounds(ia, ib, f, sc);
        let mut refined = 0usize;
        // stop once the bracket is inside the window or misses it
        let w = walk(&a.right, &b.left, sc, f, &lim, self.step0(f), &mut refined, |l, h| {
            (l >= vlo && h <= vhi) || h < vlo || l > vhi
        });
        let within = match w {
            Walk::Window => middle,
            Walk::Pinned(s) => !middle && s >= vlo && s <= vhi,
            Walk::Bracket(l, h) => !middle && l >= vlo && h <= vhi,
            Walk::Fallback => false,
        };
        let evals = refined.div_ceil(sc.active.len().max(1)) as u32;
        (if within { Verify::Within } else { Verify::Differs }, evals, refined == 0)
    }

    /// Clearance and crevice floors for a pre-floor value, skipped when exact
    /// bounds show they cannot bind.
    pub fn floors(&self, a: &PreparedGlyph, b: &PreparedGlyph, macro_gap: f64, pre: f64) -> (f64, u32) {
        let k = &self.knobs;
        let mut value = pre;
        let mut flags = 0;
        // At a bbox gap of δ every point of B is ≥ δ right of every point of A,
        // so the clearance floor never asks for more than δ.
        let margin = 1e-6 * (1.0 + macro_gap.abs() + k.clearance.abs());
        if macro_gap + value < k.clearance + margin {
            if let Some(gap) =
                clearance_floor(&a.right_pieces, a.right.extreme, &b.left_pieces, b.left.extreme, k.clearance)
            {
                if gap - macro_gap > value {
                    value = gap - macro_gap;
                    flags |= PAIR_CLEARANCE;
                }
            }
        }
        // Crevice disks lie inside their own bbox; only tip disks can reach
        // past it, so the floor never asks for more than the larger protrusion.
        let reach = a.tip_right.max(b.tip_left).max(0.0);
        if macro_gap + value < reach + margin {
            if let Some(gap) = crevice_floor(
                (&a.right_crevices, &a.right_tips, &a.right_pieces, a.right.extreme),
                (&b.left_crevices, &b.left_tips, &b.left_pieces, b.left.extreme),
                k.tau,
            ) {
                if gap - macro_gap > value {
                    value = gap - macro_gap;
                    flags |= PAIR_CREVICE;
                }
            }
        }
        (value, flags)
    }

    /// Final value of a pair whose pre-floor value is `pre` (floors, rounding).
    pub fn finish(&self, ia: usize, ib: usize, pre: f64) -> (f64, u32) {
        let (a, b) = (&self.ctx.glyphs[ia], &self.ctx.glyphs[ib]);
        if a.joins(b) {
            return (0.0, PAIR_JOIN);
        }
        let (v, fl) = self.floors(a, b, self.macro_gap(ia, ib), pre);
        (if v.abs() < 0.5 || !v.is_finite() { 0.0 } else { v }, fl)
    }

    fn mean_white(&self, a: &PreparedGlyph, b: &PreparedGlyph, s: f64, band: &PairBand) -> f64 {
        let dx = a.extreme_right() - b.extreme_left() + s;
        let (mut sum, mut w) = (0.0, 0.0);
        for i in 0..band.len() {
            if band.xa[i].is_finite() && band.xb[i].is_finite() {
                let wi = band.wa[i] + band.wb[i];
                sum += wi * (band.xb[i] + dx - band.xa[i]);
                w += wi;
            }
        }
        if w > 0.0 {
            sum / w
        } else {
            0.0
        }
    }
}

impl PreparedGlyph {
    #[inline]
    pub fn extreme_right(&self) -> f64 {
        self.right.extreme
    }
    #[inline]
    pub fn extreme_left(&self) -> f64 {
        self.left.extreme
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn window_min_matches_brute_force() {
        // uneven heights, gaps (+∞) and repeated values
        let mut seed = 12345u64;
        let mut rnd = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (seed >> 11) as f64 / (1u64 << 53) as f64
        };
        for _ in 0..200 {
            let n = 1 + (rnd() * 60.0) as usize;
            let mut ys = Vec::with_capacity(n);
            let mut y = 0.0;
            for _ in 0..n {
                y += 0.1 + rnd() * 20.0;
                ys.push(y);
            }
            let xs: Vec<f64> = (0..n).map(|_| if rnd() < 0.15 { f64::INFINITY } else { (rnd() * 8.0).floor() }).collect();
            let h = rnd() * 60.0;
            let mut out = vec![0.0; n * 2];
            let mut dq = vec![0u32; n];
            window_min(&ys, &xs, h, &mut out[1..], 2, &mut dq);
            for i in 0..n {
                let lo = ys.iter().position(|&v| v >= ys[i] - h).unwrap().saturating_sub(1);
                let hi = (ys.iter().rposition(|&v| v <= ys[i] + h).unwrap() + 1).min(n - 1);
                let want = xs[lo..=hi].iter().cloned().fold(f64::INFINITY, f64::min);
                assert_eq!(out[1 + 2 * i], want, "i {i} h {h}");
            }
        }
    }

    #[test]
    fn repulsion_table_bounds_from_above() {
        let f = ContourField::new(300.0, 4.0, 0.2, 0.3);
        let table = exp_table();
        for k in 0..100_000 {
            let d = -30.0 * f.lambda + 100.0 * f.lambda * k as f64 / 100_000.0;
            let (r, h) = (f.repulsion(d), repulsion_hi(&f, d, table));
            assert!(r <= h && (d > 59.0 * f.lambda || h <= r * 1.0161), "d {d}: {r} vs {h}");
        }
    }

    #[test]
    fn repulsion_between_brackets() {
        let f = ContourField::new(300.0, 4.0, 0.2, 0.3);
        let table = exp_table();
        for k in 0..100_000 {
            let d = -25.0 * f.lambda + 100.0 * f.lambda * k as f64 / 100_000.0;
            let r = f.repulsion(d);
            let (lo, hi) = repulsion_between(&f, d, table);
            assert!(lo <= r && r <= hi, "d {d}: {lo} {r} {hi}");
            assert!(d > 59.0 * f.lambda || hi - lo <= 2e-4 * r, "d {d}: {lo} {r} {hi}");
        }
    }

    #[test]
    fn raw_window_inverts_shape() {
        let o = SolveOptions::defaults(1000.0);
        let k = Knobs::new(&o, 1000.0, 5.0);
        let (lo, hi) = k.raw_window(-5.0, 5.0);
        assert!(lo < 0.0 && hi > 0.0);
        let (v_lo, _) = k.shape(lo);
        let (v_hi, _) = k.shape(hi);
        assert!((v_lo + 5.0).abs() < 1e-6, "{v_lo}");
        assert!((v_hi - 5.0).abs() < 1e-6, "{v_hi}");
        // just inside the window the value is below the threshold
        assert!(k.shape(lo * 0.999).0.abs() < 5.0);
        // a window around a negative value
        let (a, b) = k.raw_window(-40.0, -30.0);
        assert!((k.shape(a).0 + 40.0).abs() < 1e-6 && (k.shape(b).0 + 30.0).abs() < 1e-6);
        // a window straddling the dead zone keeps the whole dead zone inside
        let (a, b) = k.raw_window(-2.0, 2.0);
        assert!(a * k.coupling <= -k.dead_zone && b * k.coupling >= k.dead_zone);
    }
}
