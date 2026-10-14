//! Crevice-filling DMAT: multi-scale maximal disk packing of a glyph's white.
//!
//! Two kinds of white are packed with disks:
//!
//! * **Outer white** (one region per side): the white between the side profile
//!   and the bbox edge, down to a depth limit. This is the white a neighbour
//!   sees; its volume drives the optical margins of Pass 1, and its crevice
//!   disks feed the crevice repulsion of Pass 2.
//! * **Inner white**: white that is horizontally enclosed by the glyph's own ink
//!   (counters, including open ones such as n, u, H). Its volume sets the
//!   rhythm scale of each glyph group in Pass 1.
//!
//! Packing is hierarchical. Every vertex of the white's boundary where the white
//! forms a narrow wedge — an acute crevice, a stem junction, a serif bracket, a
//! diagonal meeting a stroke as in A and V — gets a converging series of
//! tangent maximal disks along the wedge bisector. For a wedge of half-angle θ,
//! a disk at distance t from the apex has radius t·sin θ and the next tangent
//! disk sits at t·(1 − sin θ)/(1 + sin θ), so the radii fall geometrically
//! towards r → 0 and wedge precisely into the point. Around each apex the
//! candidate grid is subdivided (h/2, h/4, h/8), and a greedy maximal-disk
//! packing (largest empty disk first, lazily re-validated) fills everything
//! else. Volumes are integrals of the packed disk areas, so a thin pocket keeps
//! the weight of its micro-disks instead of vanishing between grid points.

use std::collections::BinaryHeap;
use std::f64::consts::PI;

use crate::geometry::{BBox, OrdF64, Outline, SegmentGrid, Vec2};
use crate::profile::{SdfProfile, Side};

pub const DISK_BULK: u8 = 0;
pub const DISK_REFINED: u8 = 1;
pub const DISK_CREVICE: u8 = 2;
pub const DISK_TIP: u8 = 3;

#[derive(Clone, Copy, Debug)]
pub struct Disk {
    pub c: Vec2,
    pub r: f64,
    pub kind: u8,
}

impl Disk {
    #[inline]
    pub fn area(&self) -> f64 {
        PI * self.r * self.r
    }
}

#[derive(Clone, Debug)]
pub struct DmatConfig {
    /// Bulk candidate grid spacing.
    pub spacing: f64,
    /// White wedges narrower than this are crevices.
    pub crevice_angle: f64,
    /// Ink corners sharper than this are tips (protrusions).
    pub tip_angle: f64,
    /// The r → 0 limit of the converging series.
    pub micro_radius: f64,
    /// Subdivision depth of the candidate grid around wedge apexes.
    pub refine_levels: u32,
    pub max_series: usize,
}

impl DmatConfig {
    pub fn for_upm(upm: f64) -> Self {
        let s = (upm / 1000.0).max(0.05);
        DmatConfig {
            spacing: 6.0 * s,
            crevice_angle: 125f64.to_radians(),
            tip_angle: 150f64.to_radians(),
            micro_radius: 0.12 * s,
            refine_levels: 3,
            max_series: 24,
        }
    }
}

/// A narrow wedge at a boundary vertex: white (crevice) or ink (tip).
#[derive(Clone, Copy, Debug)]
pub struct Wedge {
    pub apex: Vec2,
    /// Unit bisector pointing into the wedge.
    pub dir: Vec2,
    /// Half opening angle.
    pub half: f64,
    /// Usable length along the shorter side.
    pub reach: f64,
    /// Bounded by ink on both sides (false: the bbox edge is one side).
    pub real: bool,
}

/// Margin (outer) white of one side, inside the spacing zone.
#[derive(Clone, Debug)]
pub struct SideWhite {
    pub side: Side,
    /// The bbox edge the depth is measured from.
    pub extreme: f64,
    pub depth_limit: f64,
    /// Every packed disk, glyph coordinates.
    pub disks: Vec<Disk>,
    /// Real (ink-bounded) crevices inside the zone.
    pub crevices: u32,
    /// Exact area of the region, for coverage statistics.
    pub region_area: f64,
}

impl SideWhite {
    pub fn empty(side: Side, extreme: f64, depth_limit: f64) -> Self {
        SideWhite {
            side,
            extreme,
            depth_limit,
            disks: Vec::new(),
            crevices: 0,
            region_area: 0.0,
        }
    }

    #[inline]
    fn depth_of(&self, x: f64) -> f64 {
        match self.side {
            Side::Right => self.extreme - x,
            Side::Left => x - self.extreme,
        }
    }

    /// Outer_White_Volume: integrated disk area within `depth` of the bbox edge.
    pub fn volume_within(&self, depth: f64) -> f64 {
        let cut = depth.min(self.depth_limit);
        self.disks.iter().map(|d| disk_area_below(self.depth_of(d.c.x), d.r, cut)).sum()
    }

    #[cfg(test)]
    pub fn volume(&self) -> f64 {
        self.disks.iter().map(Disk::area).sum()
    }
}

/// Inner white (counters).
#[derive(Clone, Debug, Default)]
pub struct InnerWhite {
    pub disks: Vec<Disk>,
    /// Inner_White_Volume: integrated disk area.
    pub volume: f64,
    pub crevices: u32,
}

/// Area of the part of a disk (center coordinate `u`, radius `r`) with
/// coordinate ≤ `cut`.
fn disk_area_below(u: f64, r: f64, cut: f64) -> f64 {
    let h = cut - u;
    if h >= r {
        return PI * r * r;
    }
    if h <= -r {
        return 0.0;
    }
    // area of the cap cut off by a line at distance a from the center
    let cap = |a: f64| r * r * (a / r).clamp(-1.0, 1.0).acos() - a * (r * r - a * a).max(0.0).sqrt();
    if h >= 0.0 {
        PI * r * r - cap(h)
    } else {
        cap(-h)
    }
}

/// Spatial hash of placed disks answering "how far is p from every disk".
struct Packer {
    bounds: BBox,
    cell: f64,
    nx: usize,
    ny: usize,
    buckets: Vec<Vec<u32>>,
    disks: Vec<Disk>,
}

impl Packer {
    fn new(bounds: BBox, cell: f64) -> Self {
        let mut cell = cell.max(1e-3);
        while (bounds.width() / cell).ceil() * (bounds.height() / cell).ceil() > 65_536.0 {
            cell *= 2.0;
        }
        let nx = ((bounds.width() / cell).ceil() as usize).max(1);
        let ny = ((bounds.height() / cell).ceil() as usize).max(1);
        Packer { bounds, cell, nx, ny, buckets: vec![Vec::new(); nx * ny], disks: Vec::new() }
    }

    #[inline]
    fn span(&self, lo: f64, hi: f64, origin: f64, n: usize) -> (usize, usize) {
        let top = (n - 1) as f64;
        let a = ((lo - origin) / self.cell).floor().clamp(0.0, top) as usize;
        let b = ((hi - origin) / self.cell).floor().clamp(0.0, top) as usize;
        (a, b)
    }

    /// `min(upper, min over disks of |p − c| − r)`.
    fn clearance(&self, p: Vec2, upper: f64) -> f64 {
        let mut best = upper;
        if self.disks.is_empty() || !(upper > 0.0) {
            return best;
        }
        let (i0, i1) = self.span(p.x - upper, p.x + upper, self.bounds.x0, self.nx);
        let (j0, j1) = self.span(p.y - upper, p.y + upper, self.bounds.y0, self.ny);
        for j in j0..=j1 {
            for i in i0..=i1 {
                for &k in &self.buckets[j * self.nx + i] {
                    let d = &self.disks[k as usize];
                    best = best.min((p - d.c).len() - d.r);
                }
            }
        }
        best
    }

    fn add(&mut self, d: Disk) {
        let k = self.disks.len() as u32;
        let (i0, i1) = self.span(d.c.x - d.r, d.c.x + d.r, self.bounds.x0, self.nx);
        let (j0, j1) = self.span(d.c.y - d.r, d.c.y + d.r, self.bounds.y0, self.ny);
        for j in j0..=j1 {
            for i in i0..=i1 {
                self.buckets[j * self.nx + i].push(k);
            }
        }
        self.disks.push(d);
    }
}

/// A packing candidate: center, boundary clearance, minimum radius, kind.
#[derive(Clone, Copy)]
struct Candidate {
    p: Vec2,
    sdf: f64,
    r_min: f64,
    kind: u8,
}

/// Greedy maximal-disk packing: always place the largest empty disk left.
/// Radii shrink as disks are placed, so heap entries are re-validated lazily.
fn greedy(packer: &mut Packer, cands: &[Candidate]) {
    let mut heap: BinaryHeap<(OrdF64, u32)> = BinaryHeap::with_capacity(cands.len());
    for (i, c) in cands.iter().enumerate() {
        let r = packer.clearance(c.p, c.sdf);
        if r >= c.r_min {
            heap.push((OrdF64(r), i as u32));
        }
    }
    while let Some((OrdF64(r), i)) = heap.pop() {
        let c = cands[i as usize];
        let now = packer.clearance(c.p, r);
        if now < c.r_min {
            continue;
        }
        if now < r - 1e-9 {
            heap.push((OrdF64(now), i));
            continue;
        }
        packer.add(Disk { c: c.p, r: now, kind: c.kind });
    }
}

/// The converging series of tangent maximal disks inside a wedge (r → 0).
fn wedge_series(w: &Wedge, cfg: &DmatConfig, kind: u8, bound: &dyn Fn(Vec2) -> f64, packer: Option<&Packer>) -> Vec<Disk> {
    let (s, c) = (w.half.sin(), w.half.cos());
    let mut out = Vec::new();
    if !(s > 1e-3 && c > 1e-3 && w.reach > 0.0) {
        return out;
    }
    let q = (1.0 - s) / (1.0 + s);
    let mut t = w.reach / c; // tangency points stay on the wedge sides
    for _ in 0..cfg.max_series {
        let ideal = t * s;
        if ideal < cfg.micro_radius {
            break;
        }
        let center = w.apex + w.dir * t;
        let mut r = ideal.min(bound(center));
        if let Some(p) = packer {
            r = r.min(p.clearance(center, ideal));
        }
        if r >= 0.5 * ideal && r >= cfg.micro_radius {
            out.push(Disk { c: center, r, kind });
        }
        t *= q;
    }
    out
}

/// Length walked from vertex `i` along the polyline (direction `step`) while
/// the direction stays within ~20° of the first edge.
fn reach_along(pts: &[Vec2], i: usize, step: isize, max_len: f64) -> f64 {
    let n = pts.len() as isize;
    let j = i as isize + step;
    if j < 0 || j >= n || !pts[j as usize].is_finite() {
        return 0.0;
    }
    let Some(d0) = (pts[j as usize] - pts[i]).normalized() else { return 0.0 };
    let mut len = 0.0;
    let mut k = i as isize;
    loop {
        let k2 = k + step;
        if k2 < 0 || k2 >= n {
            break;
        }
        let (a, b) = (pts[k as usize], pts[k2 as usize]);
        if !(a.is_finite() && b.is_finite()) {
            break;
        }
        let e = b - a;
        let el = e.len();
        if el > 0.0 {
            if e.dot(d0) / el < 0.94 {
                break;
            }
            len += el;
        }
        if len >= max_len {
            break;
        }
        k = k2;
    }
    len.min(max_len)
}

/// Adds sub-grids of halving spacing around a wedge apex.
fn refine_around(apex: Vec2, cfg: &DmatConfig, sdf: &dyn Fn(Vec2) -> f64, out: &mut Vec<Candidate>) {
    let h = cfg.spacing;
    for level in 1..=cfg.refine_levels {
        let hl = h / f64::powi(2.0, level as i32);
        let half = 2.0 * h / f64::powi(2.0, level as i32 - 1);
        let n = (half / hl).ceil() as i32;
        for j in -n..=n {
            for i in -n..=n {
                let p = apex + Vec2::new(i as f64 * hl + 0.5 * hl, j as f64 * hl + 0.5 * hl);
                let s = sdf(p);
                let r_min = 0.5 * hl;
                if s >= r_min {
                    out.push(Candidate { p, sdf: s, r_min, kind: DISK_REFINED });
                }
            }
        }
    }
}

#[inline]
fn depth_of(side: Side, edge: f64, x: f64) -> f64 {
    match side {
        Side::Right => edge - x,
        Side::Left => x - edge,
    }
}

#[inline]
fn x_of(side: Side, edge: f64, u: f64) -> f64 {
    match side {
        Side::Right => edge - u,
        Side::Left => edge + u,
    }
}

/// The profile as (u, y) points, u = depth from `edge` into the ink (NaN on
/// gap rays). Walking upward along it, the white lies on the left.
fn depth_polyline(profile: &SdfProfile, edge: f64) -> Vec<Vec2> {
    profile
        .rays
        .iter()
        .map(|r| Vec2::new(if r.has_ink() { depth_of(profile.side, edge, r.x) } else { f64::NAN }, r.y))
        .collect()
}

/// White wedges (crevices) and ink wedges (tips) at the vertices of a depth
/// polyline whose y lies in `y_range`.
fn polyline_wedges(pts: &[Vec2], cfg: &DmatConfig, max_reach: f64, y_range: (f64, f64)) -> (Vec<Wedge>, Vec<Wedge>) {
    let (mut crevices, mut tips) = (Vec::new(), Vec::new());
    let n = pts.len();
    for i in 1..n.saturating_sub(1) {
        let (vp, v, vn) = (pts[i - 1], pts[i], pts[i + 1]);
        if !(vp.is_finite() && v.is_finite() && vn.is_finite()) || v.y < y_range.0 || v.y > y_range.1 {
            continue;
        }
        let (d1, d2) = (v - vp, vn - v);
        if d1.len() < 1e-9 || d2.len() < 1e-9 {
            continue;
        }
        let turn = d1.cross(d2).atan2(d1.dot(d2));
        let white = PI - turn;
        let (Some(e1), Some(e2)) = ((vp - v).normalized(), (vn - v).normalized()) else { continue };
        let Some(dir) = (e1 + e2).normalized() else { continue };
        let reach = reach_along(pts, i, -1, max_reach).min(reach_along(pts, i, 1, max_reach));
        if white < cfg.crevice_angle {
            crevices.push(Wedge { apex: v, dir, half: 0.5 * white, reach, real: true });
        } else if 2.0 * PI - white < cfg.tip_angle {
            tips.push(Wedge { apex: v, dir, half: 0.5 * (2.0 * PI - white), reach, real: true });
        }
    }
    (crevices, tips)
}

/// ∫ min(u, d) dy over an interval where u runs linearly from `ua` to `ub`.
fn clipped_trapezoid(ua: f64, ub: f64, d: f64, dy: f64) -> f64 {
    if ua <= d && ub <= d {
        0.5 * (ua + ub) * dy
    } else if ua >= d && ub >= d {
        d * dy
    } else {
        let t = (d - ua) / (ub - ua);
        let shallow = if ua < ub { t } else { 1.0 - t };
        0.5 * (ua.min(ub) + d) * shallow * dy + d * (1.0 - shallow) * dy
    }
}

/// Packs the margin white of one side: inside the spacing zone `zone`
/// (heights), between the profile and `edge` — the zone's extreme ink x — and
/// at most `depth_limit` deep. Heights where the profile meets no ink (the gap
/// of an i, between the dots of a colon) are not margin white.
pub fn pack_outer(profile: &SdfProfile, zone: (f64, f64), edge: f64, depth_limit: f64, cfg: &DmatConfig) -> SideWhite {
    let side = profile.side;
    let mut out = SideWhite::empty(side, edge, depth_limit);
    let (y0, y1) = zone;
    let d = depth_limit;
    if profile.is_empty() || !(y1 - y0 > 4.0 * cfg.micro_radius) || !(d > 4.0 * cfg.micro_radius) {
        return out;
    }
    let pts = depth_polyline(profile, edge);
    let mut bsegs = Vec::new();
    for w in pts.windows(2) {
        if w[0].is_finite() && w[1].is_finite() && w[1].y >= y0 - d && w[0].y <= y1 + d {
            bsegs.push((w[0], w[1]));
        }
    }
    // lids: where a run of ink ends, the region ends with it
    for i in 0..pts.len() {
        if !pts[i].is_finite() {
            continue;
        }
        let gap_before = i > 0 && !pts[i - 1].is_finite();
        let gap_after = i + 1 < pts.len() && !pts[i + 1].is_finite();
        if gap_before || gap_after {
            bsegs.push((Vec2::new(0.0, pts[i].y), Vec2::new(pts[i].x.clamp(0.0, d), pts[i].y)));
        }
    }
    let grid = SegmentGrid::new(bsegs, 2.0 * cfg.spacing);
    let prof_u = |y: f64| profile.eval(y).map(|x| depth_of(side, edge, x));
    let sdf = |p: Vec2| -> f64 {
        if !(p.x > 0.0 && p.x < d && p.y > y0 && p.y < y1) {
            return -1.0;
        }
        match prof_u(p.y) {
            Some(u) if p.x < u => p.x.min(d - p.x).min(p.y - y0).min(y1 - p.y).min(grid.distance(p, d)),
            _ => -1.0,
        }
    };

    // exact area of the region, for coverage statistics
    let mut area = 0.0;
    for w in pts.windows(2) {
        if !(w[0].is_finite() && w[1].is_finite()) || w[1].y <= w[0].y {
            continue;
        }
        let (ya, yb) = (w[0].y.max(y0), w[1].y.min(y1));
        if yb <= ya {
            continue;
        }
        let at = |y: f64| (w[0].x + (y - w[0].y) / (w[1].y - w[0].y) * (w[1].x - w[0].x)).max(0.0);
        area += clipped_trapezoid(at(ya), at(yb), d, yb - ya);
    }
    out.region_area = area;

    let (mut wedges, _) = polyline_wedges(&pts, cfg, d, zone);
    // Where the profile leaves the zone edge (A, V feet and apexes) the white
    // between the edge line and the stroke is an acute pocket as well.
    let n = pts.len();
    for i in 0..n {
        let v = pts[i];
        if !v.is_finite() || v.x.abs() > 1e-6 || v.y < y0 || v.y > y1 {
            continue;
        }
        for j in [i.wrapping_sub(1), i + 1] {
            if j >= n || !pts[j].is_finite() {
                continue;
            }
            let e = pts[j] - v;
            if e.x <= 1e-9 {
                continue;
            }
            let line = Vec2::new(0.0, (pts[j].y - v.y).signum());
            let Some(en) = e.normalized() else { continue };
            let ang = en.dot(line).clamp(-1.0, 1.0).acos();
            if ang > 1e-3 && ang < cfg.crevice_angle {
                if let Some(dir) = (en + line).normalized() {
                    let reach = reach_along(&pts, i, j as isize - i as isize, d);
                    wedges.push(Wedge { apex: v, dir, half: 0.5 * ang, reach, real: false });
                }
            }
        }
    }

    out.crevices = wedges.iter().filter(|w| w.real).count() as u32;
    let h = cfg.spacing;
    let mut packer = Packer::new(BBox { x0: -h, y0: y0 - h, x1: d + h, y1: y1 + h }, 2.0 * h);
    for w in &wedges {
        for disk in wedge_series(w, cfg, DISK_CREVICE, &sdf, Some(&packer)) {
            packer.add(disk);
        }
    }
    let mut cands = Vec::new();
    let mut y = y0 + 0.5 * h;
    while y < y1 {
        let mut u = 0.5 * h;
        while u < d {
            let p = Vec2::new(u, y);
            let s = sdf(p);
            if s >= 0.5 * h {
                cands.push(Candidate { p, sdf: s, r_min: 0.5 * h, kind: DISK_BULK });
            }
            u += h;
        }
        y += h;
    }
    for w in &wedges {
        refine_around(w.apex, cfg, &sdf, &mut cands);
    }
    greedy(&mut packer, &cands);
    out.disks = packer.disks.iter().map(|k| Disk { c: Vec2::new(x_of(side, edge, k.c.x), k.c.y), ..*k }).collect();
    out
}

/// Crevice and tip micro-disk series along the whole facing profile (glyph
/// coordinates). Pass 2's crevice repulsion compares these between
/// neighbours, so descender and ascender corners count here even though they
/// lie outside the spacing zone. Returns (crevice disks, tip disks, crevices).
pub fn facing_series(profile: &SdfProfile, cfg: &DmatConfig, max_reach: f64) -> (Vec<Disk>, Vec<Disk>, u32) {
    if profile.is_empty() {
        return (Vec::new(), Vec::new(), 0);
    }
    let (side, edge) = (profile.side, profile.extreme);
    let pts = depth_polyline(profile, edge);
    let segs: Vec<(Vec2, Vec2)> =
        pts.windows(2).filter(|w| w[0].is_finite() && w[1].is_finite()).map(|w| (w[0], w[1])).collect();
    let mut bounds = BBox::EMPTY;
    for &(a, b) in &segs {
        bounds.include(a);
        bounds.include(b);
    }
    if !bounds.is_valid() {
        return (Vec::new(), Vec::new(), 0);
    }
    let grid = SegmentGrid::new(segs, 2.0 * cfg.spacing);
    // A crevice disk belongs to the glyph's own pocket: it may not reach past
    // the bbox edge (u = 0) into the space a neighbour occupies.
    let white_sdf = |p: Vec2| -> f64 {
        if p.x <= 0.0 {
            return -1.0;
        }
        match profile.eval(p.y).map(|x| depth_of(side, edge, x)) {
            Some(u) if p.x >= u => -1.0,
            _ => grid.distance(p, f64::INFINITY).min(p.x),
        }
    };
    let (crevices, tips) = polyline_wedges(&pts, cfg, max_reach, (f64::NEG_INFINITY, f64::INFINITY));
    let m = max_reach;
    let mut packer = Packer::new(
        BBox { x0: bounds.x0 - m, y0: bounds.y0 - m, x1: bounds.x1 + m, y1: bounds.y1 + m },
        2.0 * cfg.spacing,
    );
    let to_glyph = |k: Disk| Disk { c: Vec2::new(x_of(side, edge, k.c.x), k.c.y), ..k };
    let mut crevice_disks = Vec::new();
    for w in &crevices {
        for disk in wedge_series(w, cfg, DISK_CREVICE, &white_sdf, Some(&packer)) {
            packer.add(disk);
            crevice_disks.push(to_glyph(disk));
        }
    }
    let unbounded = |_: Vec2| f64::INFINITY;
    let tip_disks = tips.iter().flat_map(|w| wedge_series(w, cfg, DISK_TIP, &unbounded, None)).map(to_glyph).collect();
    (crevice_disks, tip_disks, crevices.len() as u32)
}

/// Packs the counters: white horizontally enclosed by the glyph's own ink.
pub fn pack_inner(outline: &Outline, left: &SdfProfile, right: &SdfProfile, cfg: &DmatConfig) -> InnerWhite {
    let mut out = InnerWhite::default();
    if outline.is_empty() || left.is_empty() || right.is_empty() {
        return out;
    }
    let bb = outline.bbox;
    let h = cfg.spacing;
    let cap = bb.width().max(bb.height());
    let grid = SegmentGrid::new(outline.edges.iter().map(|e| (e.a, e.b)).collect(), 3.0 * h);
    let enclosed = |p: Vec2| -> bool {
        match (left.eval(p.y), right.eval(p.y)) {
            (Some(xl), Some(xr)) => p.x > xl && p.x < xr,
            _ => false,
        }
    };
    let sdf = |p: Vec2| -> f64 {
        if !(p.y > bb.y0 && p.y < bb.y1 && enclosed(p)) || outline.contains(p) {
            return -1.0;
        }
        grid.distance(p, cap)
            .min(left.distance(p, cap))
            .min(right.distance(p, cap))
            .min(p.y - bb.y0)
            .min(bb.y1 - p.y)
    };
    // boundary distance for candidates already known to be in white
    let sdf_white = |p: Vec2| -> f64 {
        grid.distance(p, cap)
            .min(left.distance(p, cap))
            .min(right.distance(p, cap))
            .min(p.y - bb.y0)
            .min(bb.y1 - p.y)
    };

    let mut cands = Vec::new();
    let mut y = bb.y0 + 0.5 * h;
    while y < bb.y1 {
        let ink = outline.ink_intervals(y);
        if let (Some(xl), Some(xr)) = (left.eval(y), right.eval(y)) {
            for w in ink.windows(2) {
                let (g0, g1) = (w[0].1.max(xl), w[1].0.min(xr));
                if g1 - g0 < 0.5 * h {
                    continue;
                }
                let mut k = ((g0 - bb.x0) / h - 0.5).ceil();
                loop {
                    let x = bb.x0 + (k + 0.5) * h;
                    if x >= g1 {
                        break;
                    }
                    if x > g0 {
                        let p = Vec2::new(x, y);
                        let s = sdf_white(p);
                        if s >= 0.5 * h {
                            cands.push(Candidate { p, sdf: s, r_min: 0.5 * h, kind: DISK_BULK });
                        }
                    }
                    k += 1.0;
                }
            }
        }
        y += h;
    }

    // Crevices of the counters: sharp contour corners whose narrow side is white.
    let mut wedges = Vec::new();
    for &(e0, e1) in &outline.contour_edges {
        let ring = &outline.edges[e0 as usize..e1 as usize];
        let m = ring.len();
        if m < 3 {
            continue;
        }
        let pts: Vec<Vec2> = ring.iter().map(|e| e.a).collect();
        for k in 0..m {
            let (vp, v, vn) = (pts[(k + m - 1) % m], pts[k], pts[(k + 1) % m]);
            let (Some(e1), Some(e2)) = ((vp - v).normalized(), (vn - v).normalized()) else { continue };
            let ang = e1.dot(e2).clamp(-1.0, 1.0).acos();
            if ang >= cfg.crevice_angle {
                continue;
            }
            let Some(dir) = (e1 + e2).normalized() else { continue };
            let probe = v + dir * cfg.micro_radius;
            if outline.contains(probe) || !enclosed(probe) {
                continue;
            }
            // unroll the ring locally so reach_along can walk both ways
            let window: Vec<Vec2> = (0..m.min(64)).map(|o| pts[(k + m - m.min(64) / 2 + o) % m]).collect();
            let c = m.min(64) / 2;
            let reach = reach_along(&window, c, -1, cap).min(reach_along(&window, c, 1, cap));
            wedges.push(Wedge { apex: v, dir, half: 0.5 * ang, reach, real: true });
        }
    }
    out.crevices = wedges.len() as u32;

    let bounds = BBox { x0: bb.x0 - h, y0: bb.y0 - h, x1: bb.x1 + h, y1: bb.y1 + h };
    let mut packer = Packer::new(bounds, 2.0 * h);
    for w in &wedges {
        for disk in wedge_series(w, cfg, DISK_CREVICE, &sdf, Some(&packer)) {
            packer.add(disk);
        }
        refine_around(w.apex, cfg, &sdf, &mut cands);
    }
    greedy(&mut packer, &cands);
    out.volume = packer.disks.iter().map(Disk::area).sum();
    out.disks = packer.disks;
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geometry::NODE_LINE;
    use crate::profile::RayPlan;

    fn poly(pts: &[(f64, f64)]) -> Vec<(Vec2, u32)> {
        pts.iter().map(|&(x, y)| (Vec2::new(x, y), NODE_LINE)).collect()
    }

    #[test]
    fn disk_area_clip() {
        assert!((disk_area_below(0.0, 1.0, 5.0) - PI).abs() < 1e-12);
        assert!(disk_area_below(0.0, 1.0, -5.0).abs() < 1e-12);
        assert!((disk_area_below(0.0, 1.0, 0.0) - 0.5 * PI).abs() < 1e-12);
        let a = disk_area_below(0.0, 1.0, 0.3) + disk_area_below(0.0, 1.0, -0.3);
        assert!((a - PI).abs() < 1e-12);
    }

    #[test]
    fn wedge_series_converges_into_the_apex() {
        let cfg = DmatConfig::for_upm(1000.0);
        let w = Wedge { apex: Vec2::new(0.0, 0.0), dir: Vec2::new(1.0, 0.0), half: 15f64.to_radians(), reach: 100.0, real: true };
        let s = wedge_series(&w, &cfg, DISK_CREVICE, &|_| f64::INFINITY, None);
        assert!(s.len() >= 8, "acute wedges get a long series: {}", s.len());
        for pair in s.windows(2) {
            // successive disks are tangent and shrink geometrically
            let gap = (pair[0].c - pair[1].c).len() - pair[0].r - pair[1].r;
            assert!(gap.abs() < 1e-6, "gap {gap}");
            assert!(pair[1].r < pair[0].r);
        }
        assert!(s.last().unwrap().r < 3.0 * cfg.micro_radius);
        // every disk touches both wedge sides
        let side = Vec2::new(w.half.cos(), w.half.sin());
        for d in &s {
            assert!((d.c.cross(side).abs() - d.r).abs() < 1e-6);
        }
    }

    #[test]
    fn stem_has_no_outer_white_and_square_counter_is_filled() {
        let plan = RayPlan::for_upm(1000.0);
        let cfg = DmatConfig::for_upm(1000.0);
        // a square ring: 400×400 with a 240×240 counter
        let o = Outline::from_contours(
            &[
                poly(&[(0.0, 0.0), (400.0, 0.0), (400.0, 400.0), (0.0, 400.0)]),
                poly(&[(80.0, 80.0), (80.0, 320.0), (320.0, 320.0), (320.0, 80.0)]),
            ],
            0.05,
        );
        let l = SdfProfile::build(&o, Side::Left, &plan);
        let r = SdfProfile::build(&o, Side::Right, &plan);
        let side = pack_outer(&r, (0.0, 400.0), r.extreme, 150.0, &cfg);
        assert!(side.volume() < 1.0, "flat side has no margin white");
        let inner = pack_inner(&o, &l, &r, &cfg);
        let exact = 240.0 * 240.0;
        let cover = inner.volume / exact;
        assert!(cover > 0.6 && cover <= 1.0 + 1e-9, "coverage {cover}");
        // the four 90° counter corners are crevices and hold micro-disks
        assert_eq!(inner.crevices, 4);
        assert!(inner.disks.iter().any(|d| d.kind == DISK_CREVICE && d.r < 3.0));
        for d in &inner.disks {
            assert!(!o.contains(d.c));
            assert!(d.c.x - d.r > 80.0 - 1e-6 && d.c.x + d.r < 320.0 + 1e-6);
            assert!(d.c.y - d.r > 80.0 - 1e-6 && d.c.y + d.r < 320.0 + 1e-6);
        }
    }

    #[test]
    fn t_junction_crevice_and_arm_tip() {
        let plan = RayPlan::for_upm(1000.0);
        let cfg = DmatConfig::for_upm(1000.0);
        let t = Outline::from_contours(
            &[poly(&[
                (260.0, 0.0),
                (340.0, 0.0),
                (340.0, 620.0),
                (600.0, 620.0),
                (600.0, 700.0),
                (0.0, 700.0),
                (0.0, 620.0),
                (260.0, 620.0),
            ])],
            0.05,
        );
        let r = SdfProfile::build(&t, Side::Right, &plan);
        let white = pack_outer(&r, (0.0, 700.0), 600.0, 300.0, &cfg);
        assert!(white.crevices >= 1, "stem/arm junction is a crevice");
        let cover = white.volume_within(300.0) / white.region_area;
        assert!(cover > 0.6 && cover < 1.0 + 1e-9, "coverage {cover}");
        assert!(white.disks.iter().all(|d| d.c.x + d.r <= 600.0 + 1e-6 && d.c.x - d.r >= 300.0 - 1e-6));

        let (crev, tips, n) = facing_series(&r, &cfg, 150.0);
        assert!(n >= 1 && crev.len() >= 4, "{crev:?}");
        let smallest = crev.iter().map(|d| d.r).fold(f64::INFINITY, f64::min);
        assert!(smallest < 0.5, "series reaches micro scale: {smallest}");
        // the crevice disks hug the junction at (340, 620)
        for d in &crev {
            assert!(d.c.x > 340.0 && d.c.y < 620.0);
            assert!(t.winding(d.c) == 0);
        }
        assert!(!tips.is_empty(), "arm end corners are tips");

        // with the zone below the arm the void is margin white; a gap is not
        let i = Outline::from_contours(&[poly(&[(0.0, 0.0), (80.0, 0.0), (80.0, 500.0), (0.0, 500.0)]), poly(&[(0.0, 600.0), (80.0, 600.0), (80.0, 700.0), (0.0, 700.0)])], 0.05);
        let ri = SdfProfile::build(&i, Side::Right, &plan);
        let gap = pack_outer(&ri, (0.0, 700.0), 80.0, 100.0, &cfg);
        assert!(gap.volume() < 1.0, "the gap of an i is not margin white: {}", gap.volume());
    }
}
