//! The two physics passes.
//!
//! ## Pass 1 — macro rhythm (sidebearings)
//!
//! A line of text is modelled as a chain of rigid bodies (the glyph bounding
//! boxes, of width `w`) joined by springs. Every ordered triplet `(a, b, c)` —
//! glyph `b` set between neighbours `a` and `c` — contributes
//!
//! ```text
//! E_abc = U(a,b) + U(b,c) + ½ k_r (G_ab − G_bc)²
//! U(a,b) = ½ k_s G_ab²  +  k_b λ² exp(−(G_ab + ξ (w_a + w_b)/2) / λ)
//!          spring tension     bounding repulsion
//! G_ab   = R_a + L_b + η (Ω_a^R + Ω_b^L)          (optical gap)
//! ```
//!
//! where `L`, `R` are the sidebearings, `Ω` the DMAT outer white volume of a
//! side divided by the glyph height, `η` the white credit, `λ` the rhythm scale
//! (from the groups' inner white volume) and `ξ` couples the bounding boxes'
//! widths into the repulsion. A soft wall keeps every sidebearing from sinking
//! far below zero. The kinetic equilibrium over *all* N³ triplets is the
//! minimum of `E = 1/(2N²) Σ_abc E_abc + walls`. The triplet sum reduces
//! exactly to pair sums, and because `exp(−(x + y)/λ) = exp(−x/λ)·exp(−y/λ)`
//! the pair sums factor into per-group sums — one sweep costs O(N·G) instead
//! of O(N³). The equilibrium is found by damped block Newton relaxation (every
//! sidebearing moves until the net force on it vanishes). The test module
//! checks the reduced energy and gradient against brute-force enumeration.
//!
//! For an isolated pair the springs and the repulsion balance at the rest gap
//! `G* = λ·W(k_b / k_s)` (Lambert W), so the ratio of the two constants is the
//! Looseness/Tightness control.
//!
//! ## Pass 2 — micro SDF kerning
//!
//! Along the merged adaptive rays of a pair, every ink ray feels the signed
//! distance `d` to the other glyph's contour (its SDF) and, where both glyphs
//! have ink at that height, the horizontal white `g` between them:
//!
//! ```text
//! facing ray:    φ = k_c exp(−d/λ_c)  −  min(g, C)/C
//!                    contour repulsion    spring tension (saturates at depth C)
//! one-sided ray: φ = max(0, k_c exp(−d/λ_c) − k_c exp(−d_core/λ_c))
//! ```
//!
//! The tension lives on the white between facing contours, so for ordinary
//! pairs it adds up exactly like the sidebearings do; heights where only one
//! glyph has ink (an H's stem above an x) stay neutral and only push back if
//! the other contour comes inside the hard core (an arm over a tucked bowl).
//! The constants are scaled so that two flat stems balance exactly at the
//! Pass-1 rest gap. The
//! pair relaxes from its Pass-1 placement to the nearest equilibrium gap
//! `S(a,b)`. Kerning is the pair-specific part of that equilibrium: the
//! interaction contrast against flat probe walls,
//! `S(a,b) − S(a,|) − S(|,b) + S(|,|)`, so anything a sidebearing already
//! explains cancels out. The contrast is scaled by the contour-field coupling
//! (the SDF Kerning Intensity) through a saturating response, then clamped by
//! two hard floors: a Euclidean clearance between the facing contours and the
//! crevice pressure limit between DMAT micro-disks.

use std::cell::Cell;

use crate::dmat::Disk;
use crate::geometry::{contact_shift, Vec2};
use crate::profile::{PairBand, SdfProfile};

#[inline]
fn ex(a: f64) -> f64 {
    a.min(60.0).exp()
}

/// Principal branch of the Lambert W function for x ≥ 0 (Halley iteration).
pub fn lambert_w0(x: f64) -> f64 {
    if !(x > 0.0) {
        return 0.0;
    }
    let mut w = (1.0 + x).ln();
    for _ in 0..64 {
        let e = w.exp();
        let f = w * e - x;
        let fp = e * (w + 1.0);
        let dw = f / (fp - (w + 2.0) * f / (2.0 * w + 2.0));
        w -= dw;
        if dw.abs() < 1e-14 * (1.0 + w.abs()) {
            break;
        }
    }
    w
}

/// Rest gap of an isolated pair: `k_s G = k_b λ e^{−G/λ}`.
pub fn rest_gap(lambda: f64, spring: f64, repulsion: f64) -> f64 {
    lambda * lambert_w0(repulsion / spring.max(1e-12))
}

// ---------------------------------------------------------------- Pass 1 --

/// What Pass 1 knows about a glyph.
#[derive(Clone, Copy, Debug)]
pub struct SpacingBody {
    /// Dense rhythm-group index.
    pub group: usize,
    /// Bounding box width.
    pub width: f64,
    /// Outer white equivalent widths (DMAT volume / height) of each side.
    pub omega_l: f64,
    pub omega_r: f64,
}

#[derive(Clone, Debug)]
pub struct Pass1Params {
    /// k_s
    pub spring: f64,
    /// k_b
    pub repulsion: f64,
    /// η: how much of the outer white volume counts as spacing.
    pub white_credit: f64,
    /// k_r: triplet balance between a glyph's left and right gaps.
    pub rhythm: f64,
    /// ξ: bounding-box width coupling of the repulsion.
    pub width_coupling: f64,
    /// Wall stiffness relative to the spring.
    pub wall: f64,
    /// Wall softness length.
    pub wall_length: f64,
    pub tolerance: f64,
    pub max_iterations: u32,
}

#[derive(Clone, Debug)]
pub struct Pass1Result {
    pub lsb: Vec<f64>,
    pub rsb: Vec<f64>,
    pub iterations: u32,
    /// Largest remaining force on any sidebearing.
    pub residual: f64,
}

struct Derivatives {
    gl: Vec<f64>,
    hl: Vec<f64>,
    gr: Vec<f64>,
    hr: Vec<f64>,
}

/// The mean-field triplet chain.
pub struct TripletSystem<'a> {
    bodies: &'a [SpacingBody],
    /// Rhythm scale per group.
    lambda: &'a [f64],
    p: &'a Pass1Params,
}

impl<'a> TripletSystem<'a> {
    pub fn new(bodies: &'a [SpacingBody], lambda: &'a [f64], p: &'a Pass1Params) -> Self {
        TripletSystem { bodies, lambda, p }
    }

    #[inline]
    fn lam(&self, g: usize, h: usize) -> f64 {
        0.5 * (self.lambda[g] + self.lambda[h])
    }

    /// Optical margins ρ = R + ηΩ^R and ℓ = L + ηΩ^L.
    fn margins(&self, l: &[f64], r: &[f64]) -> (Vec<f64>, Vec<f64>) {
        let eta = self.p.white_credit;
        let rho = self.bodies.iter().zip(r).map(|(b, &x)| x + eta * b.omega_r).collect();
        let ell = self.bodies.iter().zip(l).map(|(b, &x)| x + eta * b.omega_l).collect();
        (rho, ell)
    }

    fn derivatives(&self, l: &[f64], r: &[f64]) -> Derivatives {
        let p = self.p;
        let n = self.bodies.len();
        let nf = n as f64;
        let ng = self.lambda.len();
        let xi = p.width_coupling;
        let (rho, ell) = self.margins(l, r);
        let psum: f64 = rho.iter().sum();
        let qsum: f64 = ell.iter().sum();
        // sl[g][h] = Σ_{b∈h} e^{−(ℓ_b + ξw_b/2)/λ_gh}   (right partners of a glyph in g)
        // sr[g][h] = Σ_{a∈h} e^{−(ρ_a + ξw_a/2)/λ_gh}   (left partners of a glyph in g)
        let mut sl = vec![0.0; ng * ng];
        let mut sr = vec![0.0; ng * ng];
        for (i, b) in self.bodies.iter().enumerate() {
            let h = b.group;
            for g in 0..ng {
                let lam = self.lam(g, h);
                sl[g * ng + h] += ex(-(ell[i] + 0.5 * xi * b.width) / lam);
                sr[g * ng + h] += ex(-(rho[i] + 0.5 * xi * b.width) / lam);
            }
        }
        let kw = p.wall * p.spring;
        let lw = p.wall_length;
        let rhythm_h = p.rhythm * (1.0 - 1.0 / nf);
        let mut d = Derivatives { gl: vec![0.0; n], hl: vec![0.0; n], gr: vec![0.0; n], hr: vec![0.0; n] };
        for (i, b) in self.bodies.iter().enumerate() {
            let g = b.group;
            let (mut rep_r, mut rep_rh, mut rep_l, mut rep_lh) = (0.0, 0.0, 0.0, 0.0);
            for h in 0..ng {
                let lam = self.lam(g, h);
                let er = ex(-(rho[i] + 0.5 * xi * b.width) / lam) * sl[g * ng + h];
                rep_r += lam * er;
                rep_rh += er;
                let el = ex(-(ell[i] + 0.5 * xi * b.width) / lam) * sr[g * ng + h];
                rep_l += lam * el;
                rep_lh += el;
            }
            let wall_r = ex(-r[i] / lw);
            let wall_l = ex(-l[i] / lw);
            d.gr[i] = (p.spring * (nf * rho[i] + qsum) - p.repulsion * rep_r) / nf
                + 0.5 * p.rhythm * (2.0 * rho[i] - ell[i] + (qsum - 2.0 * psum) / nf)
                - 0.5 * kw * lw * wall_r;
            d.hr[i] = p.spring + p.repulsion * rep_rh / nf + rhythm_h + 0.5 * kw * wall_r;
            d.gl[i] = (p.spring * (nf * ell[i] + psum) - p.repulsion * rep_l) / nf
                + 0.5 * p.rhythm * (2.0 * ell[i] - rho[i] + (psum - 2.0 * qsum) / nf)
                - 0.5 * kw * lw * wall_l;
            d.hl[i] = p.spring + p.repulsion * rep_lh / nf + rhythm_h + 0.5 * kw * wall_l;
        }
        d
    }

    /// The triplet energy in its reduced O(N²) form (the solver only needs its
    /// gradient; the tests check both against brute-force enumeration).
    #[cfg(test)]
    pub fn energy(&self, l: &[f64], r: &[f64]) -> f64 {
        let p = self.p;
        let n = self.bodies.len();
        let nf = n as f64;
        let (rho, ell) = self.margins(l, r);
        let mut pair = 0.0;
        for (a, ba) in self.bodies.iter().enumerate() {
            for (b, bb) in self.bodies.iter().enumerate() {
                pair += self.pair_energy(rho[a] + ell[b], ba, bb);
            }
        }
        let psum: f64 = rho.iter().sum();
        let qsum: f64 = ell.iter().sum();
        let p2: f64 = rho.iter().map(|x| x * x).sum();
        let q2: f64 = ell.iter().map(|x| x * x).sum();
        let z2: f64 = rho.iter().zip(&ell).map(|(r, l)| (l - r) * (l - r)).sum();
        let rhythm = 0.25 * p.rhythm * (p2 + q2 - 2.0 * psum * qsum / nf - 2.0 * (psum - qsum).powi(2) / nf + z2);
        pair / nf + rhythm + self.wall_energy(l, r)
    }

    #[cfg(test)]
    fn pair_energy(&self, g: f64, a: &SpacingBody, b: &SpacingBody) -> f64 {
        let p = self.p;
        let lam = self.lam(a.group, b.group);
        0.5 * p.spring * g * g
            + p.repulsion * lam * lam * ex(-(g + 0.5 * p.width_coupling * (a.width + b.width)) / lam)
    }

    #[cfg(test)]
    fn wall_energy(&self, l: &[f64], r: &[f64]) -> f64 {
        let (kw, lw) = (self.p.wall * self.p.spring, self.p.wall_length);
        0.5 * kw * lw * lw * l.iter().chain(r).map(|&x| ex(-x / lw)).sum::<f64>()
    }

    /// Optical gaps only fix sums R_a + L_b: moving every right margin by +c and
    /// every left margin by −c changes no gap, so the gap physics cannot split
    /// the space between the two sides. The gauge is pinned so that the median
    /// glyph has balanced optical margins (median of ℓ − ρ is zero). Most
    /// letters are near-symmetric, so symmetric glyphs come out symmetric, and
    /// outliers held up by a wall (L, T, r) do not tilt everyone else.
    fn pin_gauge(&self, l: &mut [f64], r: &mut [f64]) {
        let (rho, ell) = self.margins(l, r);
        let mut z: Vec<f64> = ell.iter().zip(&rho).map(|(a, b)| a - b).collect();
        z.sort_by(f64::total_cmp);
        let m = z.len() / 2;
        let median = if z.len() % 2 == 1 { z[m] } else { 0.5 * (z[m - 1] + z[m]) };
        let c = 0.5 * median;
        r.iter_mut().for_each(|x| *x += c);
        l.iter_mut().for_each(|x| *x -= c);
    }

    /// Relaxes all sidebearings to the kinetic equilibrium: damped diagonal
    /// Newton on every sidebearing at once (each moves until the net force on
    /// it vanishes), with a Lagrange correction that keeps the gauge pinned.
    /// The damping of ½ is exact for the uniform mode — both sides of every gap
    /// answering the same force — and contracts every other mode.
    pub fn solve(&self, init_l: &[f64], init_r: &[f64]) -> Pass1Result {
        const OMEGA: f64 = 0.5;
        let n = self.bodies.len();
        let mut l = init_l.to_vec();
        let mut r = init_r.to_vec();
        if n == 0 {
            return Pass1Result { lsb: l, rsb: r, iterations: 0, residual: 0.0 };
        }
        self.pin_gauge(&mut l, &mut r);
        let mut iterations = 0;
        while iterations < self.p.max_iterations {
            iterations += 1;
            let d = self.derivatives(&l, &r);
            // μ keeps Σ ΔR = Σ ΔL, i.e. the mean optical margins stay balanced
            let (mut num, mut den) = (0.0, 0.0);
            for i in 0..n {
                num += d.gr[i] / d.hr[i] - d.gl[i] / d.hl[i];
                den += 1.0 / d.hr[i] + 1.0 / d.hl[i];
            }
            let mu = num / den;
            let mut max_step: f64 = 0.0;
            for i in 0..n {
                let cap = 0.25 * self.lambda[self.bodies[i].group];
                let sr = OMEGA * (-(d.gr[i] - mu) / d.hr[i]).clamp(-cap, cap);
                let sl = OMEGA * (-(d.gl[i] + mu) / d.hl[i]).clamp(-cap, cap);
                r[i] += sr;
                l[i] += sl;
                max_step = max_step.max(sr.abs()).max(sl.abs());
            }
            self.pin_gauge(&mut l, &mut r); // only corrects drift from clamped steps
            if max_step < self.p.tolerance {
                break;
            }
        }
        // residual force, not counting the pinned gauge direction
        let d = self.derivatives(&l, &r);
        let mu = (d.gr.iter().sum::<f64>() - d.gl.iter().sum::<f64>()) / (2.0 * n as f64);
        let residual = d.gr.iter().map(|g| (g - mu).abs()).chain(d.gl.iter().map(|g| (g + mu).abs())).fold(0.0, f64::max);
        Pass1Result { lsb: l, rsb: r, iterations, residual }
    }
}

// ---------------------------------------------------------------- Pass 2 --

/// The contour field of one pair, calibrated to its Pass-1 rest gap.
#[derive(Clone, Copy, Debug)]
pub struct ContourField {
    pub g_ref: f64,
    /// Depth cap C: beyond it the spring tension no longer grows.
    pub cap: f64,
    /// Decay length of the contour repulsion.
    pub lambda: f64,
    pub k_c: f64,
    /// Hard-core radius of one-sided rays and the repulsion level there.
    pub core_radius: f64,
    pub core: f64,
    /// Beyond this distance the repulsion is negligible (< 0.3 % of k_c e^{-G/λ}).
    pub search: f64,
}

impl ContourField {
    /// `cap_ratio` = C / G*, `decay_ratio` = λ_c / G*, `core_ratio` = d_core / G*.
    pub fn new(g_ref: f64, cap_ratio: f64, decay_ratio: f64, core_ratio: f64) -> Self {
        let g = g_ref.max(1e-3);
        let cap = cap_ratio.max(1.05) * g;
        let lambda = decay_ratio.max(0.02) * g;
        // φ(G*) = 0: two flat stems balance exactly at the rest gap
        let k_c = (g / cap) * (g / lambda).exp();
        let core_radius = core_ratio.clamp(0.0, 1.0) * g;
        let search = (g + 6.0 * lambda).min(cap);
        let mut f = ContourField { g_ref: g, cap, lambda, k_c, core_radius, core: 0.0, search };
        f.core = f.repulsion(core_radius);
        f
    }

    #[inline]
    pub fn repulsion(&self, d: f64) -> f64 {
        self.k_c * (-(d.max(-20.0 * self.lambda)) / self.lambda).exp()
    }

    #[inline]
    pub fn tension(&self, g: f64) -> f64 {
        g.clamp(0.0, self.cap) / self.cap
    }

    /// Force of one ray (positive pushes apart): SDF distance `d`, horizontal
    /// white `g` (meaningful when `facing`).
    #[inline]
    pub fn force(&self, d: f64, g: f64, facing: bool) -> f64 {
        if facing {
            self.repulsion(d) - self.tension(g)
        } else {
            (self.repulsion(d) - self.core).max(0.0)
        }
    }
}

/// Net contour force on a pair at bbox gap `s` (positive pushes apart).
/// `a` is the left glyph's right profile, `b` the right glyph's left profile.
pub fn contour_force(a: &SdfProfile, b: &SdfProfile, band: &PairBand, s: f64, f: &ContourField) -> f64 {
    let dx = a.extreme - b.extreme + s; // B-local x + dx = A x
    let mut total = 0.0;
    for i in 0..band.len() {
        let y = band.ys[i];
        let (xa, xb) = (band.xa[i], band.xb[i]);
        let facing = xa.is_finite() && xb.is_finite();
        let g = if facing { xb + dx - xa } else { f64::NAN };
        // A facing ray's horizontal partner point bounds its distance; a
        // one-sided ray only cares inside the hard core.
        let reach = if facing { f.search.min(g.abs() + 1e-9) } else { f.core_radius };
        if band.wa[i] > 0.0 {
            let p = Vec2::new(xa - dx, y);
            let dist = b.distance(p, reach);
            let d = if facing && p.x > xb { -dist } else { dist };
            total += band.wa[i] * f.force(d, g, facing);
        }
        if band.wb[i] > 0.0 {
            let q = Vec2::new(xb + dx, y);
            let dist = a.distance(q, reach);
            let d = if facing && q.x < xa { -dist } else { dist };
            total += band.wb[i] * f.force(d, g, facing);
        }
    }
    total
}

/// Relaxes a pair from gap `s0` to the nearest equilibrium of the contour
/// field: bracket by doubling steps in the direction of the net force, then
/// Illinois regula falsi. Returns the gap and the number of force evaluations.
#[allow(clippy::too_many_arguments)]
pub fn equilibrium_gap(
    a: &SdfProfile,
    b: &SdfProfile,
    band: &PairBand,
    f: &ContourField,
    s0: f64,
    tol: f64,
    s_min: f64,
    s_max: f64,
) -> (f64, u32) {
    let evals = Cell::new(0u32);
    let force = |s: f64| {
        evals.set(evals.get() + 1);
        contour_force(a, b, band, s, f)
    };
    let weight: f64 = band.wa.iter().chain(&band.wb).sum();
    let ftol = 1e-6 * weight.max(1e-9);
    let s0 = s0.clamp(s_min, s_max);
    let f0 = force(s0);
    if f0.abs() <= ftol {
        return (s0, evals.get());
    }
    // bracket: force(lo) ≥ 0 ≥ force(hi)
    let mut step = 0.25 * f.g_ref.max(4.0 * tol);
    let (mut lo, mut flo, mut hi, mut fhi);
    let mut s = s0;
    if f0 < 0.0 {
        hi = s0;
        fhi = f0;
        loop {
            s -= step;
            if s <= s_min {
                let fm = force(s_min);
                if fm < 0.0 {
                    return (s_min, evals.get());
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
    } else {
        lo = s0;
        flo = f0;
        loop {
            s += step;
            if s >= s_max {
                let fm = force(s_max);
                if fm > 0.0 {
                    return (s_max, evals.get());
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
    }
    // Illinois: a secant step inside the bracket; an end retained twice in a
    // row has its force halved so the bracket keeps shrinking from both sides.
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
            return (m, evals.get());
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
    (0.5 * (lo + hi), evals.get())
}

/// Profile pieces whose y-range meets `[ylo, yhi]` (pieces are sorted by y).
fn pieces_near(segs: &[(Vec2, Vec2)], ylo: f64, yhi: f64) -> impl Iterator<Item = &(Vec2, Vec2)> {
    let start = segs.partition_point(|s| s.1.y < ylo);
    segs[start..].iter().take_while(move |s| s.0.y <= yhi)
}

#[inline]
fn bump(best: &mut Option<f64>, s: f64) {
    *best = Some(best.map_or(s, |b| b.max(s)));
}

/// Smallest bbox gap at which the facing contours are at least `delta` apart
/// (exact for the profile polylines: horizontal capsule sweeps).
pub fn clearance_floor(
    a_pieces: &[(Vec2, Vec2)],
    a_extreme: f64,
    b_pieces: &[(Vec2, Vec2)],
    b_extreme: f64,
    delta: f64,
) -> Option<f64> {
    let delta = delta.max(1e-6);
    let mut best = None;
    for &(p0, p1) in a_pieces {
        for &(q0, q1) in pieces_near(b_pieces, p0.y - delta, p1.y + delta) {
            if let Some(s) = contact_shift(p0, p1, q0, q1, delta) {
                bump(&mut best, s);
            }
        }
    }
    best.map(|dx| dx - (a_extreme - b_extreme))
}

/// One facing side for the crevice test: crevice micro-disks, tip micro-disks,
/// profile pieces and the bbox edge the pieces are measured from.
pub type FacingSide<'a> = (&'a [Disk], &'a [Disk], &'a [(Vec2, Vec2)], f64);

/// Crevice repulsion: the smallest bbox gap at which no micro-disk of either
/// glyph's crevices is pressed beyond the pressure threshold `tau` by the other
/// glyph's contour or tip micro-disks. Pressure is the intrusion depth as a
/// fraction of the crevice disk's radius.
pub fn crevice_floor(a: FacingSide, b: FacingSide, tau: f64) -> Option<f64> {
    let (a_crev, a_tips, a_pieces, a_extreme) = a;
    let (b_crev, b_tips, b_pieces, b_extreme) = b;
    let keep = 1.0 - tau.clamp(0.0, 1.0);
    let mut best = None;
    // A's crevices against B's contour (B moves)
    for d in a_crev {
        let rho = d.r * keep;
        if rho <= 0.0 {
            continue;
        }
        for &(q0, q1) in pieces_near(b_pieces, d.c.y - rho, d.c.y + rho) {
            if let Some(s) = contact_shift(d.c, d.c, q0, q1, rho) {
                bump(&mut best, s);
            }
        }
        for t in b_tips {
            let rr = t.r + rho;
            let dy = t.c.y - d.c.y;
            if dy.abs() < rr {
                bump(&mut best, d.c.x - t.c.x + (rr * rr - dy * dy).sqrt());
            }
        }
    }
    // B's crevices against A's contour
    for d in b_crev {
        let rho = d.r * keep;
        if rho <= 0.0 {
            continue;
        }
        for &(p0, p1) in pieces_near(a_pieces, d.c.y - rho, d.c.y + rho) {
            if let Some(s) = contact_shift(p0, p1, d.c, d.c, rho) {
                bump(&mut best, s);
            }
        }
        for t in a_tips {
            let rr = t.r + rho;
            let dy = t.c.y - d.c.y;
            if dy.abs() < rr {
                bump(&mut best, t.c.x - d.c.x + (rr * rr - dy * dy).sqrt());
            }
        }
    }
    best.map(|dx| dx - (a_extreme - b_extreme))
}

/// Saturating response of the kerning to the field: slope 1 at zero, limits
/// `-neg` and `+pos`.
#[inline]
pub fn soft_clip(x: f64, neg: f64, pos: f64) -> f64 {
    if x < 0.0 {
        -neg * (1.0 - (x / neg).exp())
    } else {
        pos * (1.0 - (-x / pos).exp())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params() -> Pass1Params {
        Pass1Params {
            spring: 1.0,
            repulsion: 0.8,
            white_credit: 0.6,
            rhythm: 0.3,
            width_coupling: 0.07,
            wall: 4.0,
            wall_length: 10.0,
            tolerance: 1e-9,
            max_iterations: 5000,
        }
    }

    fn bodies() -> Vec<SpacingBody> {
        // a deterministic mix of groups, widths and white volumes
        (0..7)
            .map(|i| SpacingBody {
                group: i % 3,
                width: 300.0 + 47.0 * i as f64,
                omega_l: (i * 13 % 50) as f64,
                omega_r: (i * 29 % 70) as f64,
            })
            .collect()
    }

    #[test]
    fn lambert_w() {
        for &x in &[1e-6, 0.3, 0.82, 1.0, 5.0, 100.0] {
            let w = lambert_w0(x);
            assert!((w * w.exp() - x).abs() < 1e-10 * (1.0 + x));
        }
    }

    #[test]
    fn reduced_energy_equals_triplet_enumeration() {
        let b = bodies();
        let lambda = [180.0, 260.0, 140.0];
        let p = params();
        let sys = TripletSystem::new(&b, &lambda, &p);
        let n = b.len();
        let l: Vec<f64> = (0..n).map(|i| 20.0 + 7.0 * i as f64).collect();
        let r: Vec<f64> = (0..n).map(|i| 35.0 - 4.0 * i as f64).collect();

        let (rho, ell) = sys.margins(&l, &r);
        let mut brute = 0.0;
        for a in 0..n {
            for m in 0..n {
                for c in 0..n {
                    let gab = rho[a] + ell[m];
                    let gbc = rho[m] + ell[c];
                    brute += sys.pair_energy(gab, &b[a], &b[m])
                        + sys.pair_energy(gbc, &b[m], &b[c])
                        + 0.5 * p.rhythm * (gab - gbc).powi(2);
                }
            }
        }
        let brute = brute / (2.0 * (n * n) as f64) + sys.wall_energy(&l, &r);
        let reduced = sys.energy(&l, &r);
        assert!((brute - reduced).abs() < 1e-9 * brute.abs(), "{brute} vs {reduced}");

        // the factorized gradient used by the solver matches the energy
        let d = sys.derivatives(&l, &r);
        let h = 1e-4;
        for i in 0..n {
            let mut lp = l.clone();
            lp[i] += h;
            let mut lm = l.clone();
            lm[i] -= h;
            let num = (sys.energy(&lp, &r) - sys.energy(&lm, &r)) / (2.0 * h);
            assert!((num - d.gl[i]).abs() < 1e-5 * (1.0 + num.abs()), "dL{i}: {num} vs {}", d.gl[i]);
            let mut rp = r.clone();
            rp[i] += h;
            let mut rm = r.clone();
            rm[i] -= h;
            let num = (sys.energy(&l, &rp) - sys.energy(&l, &rm)) / (2.0 * h);
            assert!((num - d.gr[i]).abs() < 1e-5 * (1.0 + num.abs()), "dR{i}: {num} vs {}", d.gr[i]);
        }
    }

    #[test]
    fn relaxation_reaches_equilibrium_and_minimum() {
        let b = bodies();
        let lambda = [180.0, 260.0, 140.0];
        let p = params();
        let sys = TripletSystem::new(&b, &lambda, &p);
        let n = b.len();
        let res = sys.solve(&vec![0.0; n], &vec![0.0; n]);
        assert!(res.residual < 1e-6, "residual {}", res.residual);
        let e0 = sys.energy(&res.lsb, &res.rsb);
        for i in 0..n {
            let mut l = res.lsb.clone();
            l[i] += 0.5;
            assert!(sys.energy(&l, &res.rsb) > e0);
        }
    }

    #[test]
    fn identical_symmetric_glyphs_get_the_rest_gap() {
        // no white, no rhythm, no width coupling, soft walls far away:
        // every gap relaxes to λ·W(k_b/k_s)
        let b = vec![SpacingBody { group: 0, width: 400.0, omega_l: 0.0, omega_r: 0.0 }; 4];
        let lambda = [200.0];
        let p = Pass1Params { rhythm: 0.0, width_coupling: 0.0, wall: 0.0, ..params() };
        let res = TripletSystem::new(&b, &lambda, &p).solve(&[0.0; 4], &[0.0; 4]);
        let g = rest_gap(200.0, p.spring, p.repulsion);
        for i in 0..4 {
            assert!((res.lsb[i] + res.rsb[i] - g).abs() < 1e-6);
            assert!((res.lsb[i] - res.rsb[i]).abs() < 1e-6);
        }
    }

    #[test]
    fn contour_field_balances_flat_stems_at_the_rest_gap() {
        let f = ContourField::new(120.0, 1.6, 0.35, 0.5);
        assert!(f.force(120.0, 120.0, true).abs() < 1e-12);
        assert!(f.force(60.0, 60.0, true) > 0.0 && f.force(200.0, 200.0, true) < 0.0);
        assert_eq!(f.force(100.0, f64::NAN, false), 0.0, "one-sided rays are neutral outside the core");
        assert!(f.force(30.0, f64::NAN, false) > 0.0);
        let a = SdfProfile::probe(crate::profile::Side::Right, 0.0, 700.0, 0.0);
        let b = SdfProfile::probe(crate::profile::Side::Left, 0.0, 700.0, 0.0);
        let band = PairBand::merge(&a, &b);
        for start in [30.0, 119.0, 400.0] {
            let (s, evals) = equilibrium_gap(&a, &b, &band, &f, start, 0.01, -500.0, 1000.0);
            assert!((s - 120.0).abs() < 0.02, "{s}");
            assert!(evals < 20, "{evals} evaluations");
        }
    }

    #[test]
    fn soft_clip_shape() {
        assert!((soft_clip(0.0, 100.0, 50.0)).abs() < 1e-12);
        assert!((soft_clip(-1e-3, 100.0, 50.0) + 1e-3).abs() < 1e-8);
        assert!(soft_clip(-1e6, 100.0, 50.0) > -100.0 - 1e-9);
        assert!(soft_clip(1e6, 100.0, 50.0) < 50.0 + 1e-9);
    }
}
