//! Pre-compute pass for Pass 2: adaptive raycasting into per-glyph `SdfProfile`s.
//!
//! A profile is the extreme ink x of one side of a glyph as a function of y,
//! sampled by horizontal rays. Instead of a fixed 10-unit comb, the rays are
//! planned from the Bézier segments that actually form the extreme contour:
//!
//! * straight pieces (vertical stems, diagonals, curves with collinear handles)
//!   get rays only at their ends — the profile is exactly linear between them;
//! * curves are marched in y with an interval chosen from the *profile
//!   curvature* `|d²x/dy²| = |x″y′ − x′y″| / |y′|³`. Linear interpolation over an
//!   interval h errs by h²·|d²x/dy²|/8, so the interval that keeps the error
//!   under the tolerance is snapped down the tiers 10 → 5 → 2 → 1 units.
//!   Where the curve flattens horizontally (y′ → 0) the curvature diverges and
//!   the comb automatically tightens to 1 unit;
//! * nodes, x-extrema and guard rays around discontinuities (arm/stem jumps,
//!   gaps such as the i-dot) are always kept.
//!
//! A candidate ray produced by a segment is kept only if that segment really is
//! the extreme at that height, so counters and hidden strokes cost nothing.
//! For a pair, the two profiles are merged into one y-array (`PairBand`) and the
//! physics only ever evaluates distance fields at those heights.

use crate::geometry::{dist2_point_seg, BBox, Outline, Segment, Vec2};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    Left,
    Right,
}

impl Side {
    #[inline]
    pub fn pick(self, lo: f64, hi: f64) -> f64 {
        match self {
            Side::Left => lo,
            Side::Right => hi,
        }
    }
}

/// Rays from nodes, extrema, straight-segment ends and discontinuity guards.
pub const TIER_STRUCTURAL: u8 = 0;
/// Number of curvature tiers (10, 5, 2, 1 units at 1000 UPM).
pub const CURVATURE_TIERS: usize = 4;

#[derive(Clone, Copy, Debug)]
pub struct Ray {
    pub y: f64,
    /// Extreme ink x on this ray; NaN where the ray meets no ink (a gap).
    pub x: f64,
    pub tier: u8,
}

impl Ray {
    #[inline]
    pub fn has_ink(&self) -> bool {
        self.x.is_finite()
    }
    #[inline]
    fn pt(&self) -> Vec2 {
        Vec2::new(self.x, self.y)
    }
}

/// Ray-planning constants, scaled to the font's units per em.
#[derive(Clone, Debug)]
pub struct RayPlan {
    pub intervals: [f64; CURVATURE_TIERS],
    /// Allowed linear-interpolation error of the profile.
    pub tolerance: f64,
    /// Offset of the guard rays placed just above/below nodes.
    pub guard: f64,
    /// A guard is kept when the profile moves more than this across it.
    pub jump: f64,
}

impl RayPlan {
    pub fn for_upm(upm: f64) -> Self {
        let s = (upm / 1000.0).max(0.05);
        RayPlan {
            intervals: [10.0 * s, 5.0 * s, 2.0 * s, 1.0 * s],
            tolerance: 0.2 * s,
            guard: 0.05 * s,
            jump: 0.5 * s,
        }
    }

    /// Ray interval and tier from the profile curvature at parameter `t`.
    fn interval_at(&self, seg: &Segment, t: f64) -> (f64, u8) {
        let d1 = seg.d1(t);
        let d2 = seg.d2(t);
        let dy = d1.y.abs();
        let kappa = if dy < 1e-9 {
            f64::INFINITY
        } else {
            (d2.x * d1.y - d1.x * d2.y).abs() / (dy * dy * dy)
        };
        let h_opt = if kappa > 0.0 { (8.0 * self.tolerance / kappa).sqrt() } else { f64::INFINITY };
        for (i, &h) in self.intervals.iter().enumerate() {
            if h_opt >= h {
                return (h, i as u8 + 1);
            }
        }
        (self.intervals[CURVATURE_TIERS - 1], CURVATURE_TIERS as u8)
    }
}

/// The adaptive SDF profile of one side of a glyph.
#[derive(Clone, Debug)]
pub struct SdfProfile {
    pub side: Side,
    /// Sorted by y. Gap rays (no ink) mark where a run of ink ends.
    pub rays: Vec<Ray>,
    /// The bbox edge on this side (x_min for Left, x_max for Right).
    pub extreme: f64,
    /// Runs of consecutive pieces with their bounds, for pruned distance queries.
    chunks: Vec<Chunk>,
    /// Runs of consecutive chunks (`first..=last` chunk indices) with their
    /// bounds: dense rays (1-unit tiers) make many chunks per unit height.
    supers: Vec<Chunk>,
}

/// Pieces `first..last` (ray indices, `last` inclusive) and their bounds.
#[derive(Clone, Copy, Debug)]
struct Chunk {
    first: usize,
    last: usize,
    ymin: f64,
    ymax: f64,
    bounds: BBox,
}

const CHUNK_PIECES: usize = 8;
const SUPER_CHUNKS: usize = 8;

#[derive(Clone, Copy)]
struct Candidate {
    y: f64,
    tier: u8,
    seg: u32,
}

const NO_SEG: u32 = u32::MAX;

impl SdfProfile {
    pub fn empty(side: Side) -> Self {
        SdfProfile { side, rays: Vec::new(), extreme: 0.0, chunks: Vec::new(), supers: Vec::new() }
    }

    /// A flat vertical wall at `x` spanning `[y0, y1]` — the neutral partner
    /// used to measure a glyph's own (non-interacting) response.
    pub fn probe(side: Side, y0: f64, y1: f64, x: f64) -> Self {
        SdfProfile {
            side,
            rays: vec![Ray { y: y0, x, tier: 0 }, Ray { y: y1, x, tier: 0 }],
            extreme: x,
            chunks: Vec::new(),
            supers: Vec::new(),
        }
        .with_chunks()
    }

    fn with_chunks(mut self) -> Self {
        let n = self.rays.len();
        let mut first = 0;
        while first + 1 < n {
            let last = (first + CHUNK_PIECES).min(n - 1);
            let mut bounds = BBox::EMPTY;
            for r in &self.rays[first..=last] {
                if r.has_ink() {
                    bounds.include(r.pt());
                }
            }
            self.chunks.push(Chunk { first, last, ymin: self.rays[first].y, ymax: self.rays[last].y, bounds });
            first = last;
        }
        let nc = self.chunks.len();
        let mut k = 0;
        while k < nc {
            let end = (k + SUPER_CHUNKS).min(nc) - 1;
            let mut bounds = BBox::EMPTY;
            for c in &self.chunks[k..=end] {
                if c.bounds.is_valid() {
                    bounds.include(Vec2::new(c.bounds.x0, c.bounds.y0));
                    bounds.include(Vec2::new(c.bounds.x1, c.bounds.y1));
                }
            }
            self.supers.push(Chunk { first: k, last: end, ymin: self.chunks[k].ymin, ymax: self.chunks[end].ymax, bounds });
            k = end + 1;
        }
        self
    }

    /// Plans and casts the adaptive rays for one side of `outline`.
    pub fn build(outline: &Outline, side: Side, plan: &RayPlan) -> Self {
        if outline.is_empty() {
            return SdfProfile::empty(side);
        }
        let bb = outline.bbox;
        let mut cands: Vec<Candidate> = Vec::new();
        for (si, seg) in outline.segments.iter().enumerate() {
            cands.push(Candidate { y: seg.start().y, tier: TIER_STRUCTURAL, seg: NO_SEG });
            cands.push(Candidate { y: seg.end().y, tier: TIER_STRUCTURAL, seg: NO_SEG });
            for t in seg.x_extrema() {
                cands.push(Candidate { y: seg.eval(t).y, tier: TIER_STRUCTURAL, seg: NO_SEG });
            }
            if seg.is_straight() {
                continue;
            }
            let mut ts = vec![0.0];
            ts.extend(seg.y_extrema());
            ts.push(1.0);
            for w in ts.windows(2) {
                let mut ys = Vec::new();
                march(plan, seg, w[0], w[1], &mut ys);
                cands.extend(ys.into_iter().map(|(y, tier)| Candidate { y, tier, seg: si as u32 }));
            }
        }
        cands.sort_by(|a, b| a.y.total_cmp(&b.y).then(a.tier.cmp(&b.tier)));

        // Candidates at (nearly) the same height form a group; the ray takes the
        // coarsest tier among the group's *visible* candidates. Visibility must be
        // decided before merging: mirrored segments (the two lower quarters of a
        // bowl) plan identical heights, and the hidden one must not shadow the other.
        let mut rays: Vec<Ray> = Vec::with_capacity(cands.len());
        let mut i = 0;
        while i < cands.len() {
            let y = cands[i].y;
            let mut j = i + 1;
            while j < cands.len() && cands[j].y - y < 1e-7 {
                j += 1;
            }
            if let Some(hit) = outline.scan(y) {
                let x = side.pick(hit.xmin, hit.xmax);
                let visible = |c: &Candidate| {
                    c.seg == NO_SEG
                        || outline.scan_segment(c.seg as usize, y).is_some_and(|own| {
                            (side.pick(own.xmin, own.xmax) - x).abs() <= 1e-6 * (1.0 + x.abs())
                        })
                };
                if let Some(c) = cands[i..j].iter().find(|c| visible(c)) {
                    rays.push(Ray { y, x, tier: c.tier });
                }
            }
            i = j;
        }

        // Guard rays catch jumps of the profile (T arm over stem) and the ends
        // of ink runs (i stem below its dot) right next to the node that causes them.
        let mut guards = Vec::new();
        for r in rays.iter().filter(|r| r.tier == TIER_STRUCTURAL) {
            for dy in [-plan.guard, plan.guard] {
                let y2 = r.y + dy;
                match outline.scan(y2) {
                    None => guards.push(Ray { y: y2, x: f64::NAN, tier: TIER_STRUCTURAL }),
                    Some(h) => {
                        let x2 = side.pick(h.xmin, h.xmax);
                        if (x2 - r.x).abs() > plan.jump {
                            guards.push(Ray { y: y2, x: x2, tier: TIER_STRUCTURAL });
                        }
                    }
                }
            }
        }
        rays.extend(guards);
        rays.sort_by(|a, b| a.y.total_cmp(&b.y));
        rays.dedup_by(|later, kept| (later.y - kept.y).abs() < 1e-7);
        while rays.first().is_some_and(|r| !r.has_ink()) {
            rays.remove(0);
        }
        while rays.last().is_some_and(|r| !r.has_ink()) {
            rays.pop();
        }
        rays.dedup_by(|later, kept| !later.has_ink() && !kept.has_ink());

        SdfProfile { side, rays, extreme: side.pick(bb.x0, bb.x1), chunks: Vec::new(), supers: Vec::new() }
            .with_chunks()
    }

    pub fn is_empty(&self) -> bool {
        self.rays.len() < 2
    }

    pub fn ink_ray_count(&self) -> usize {
        self.rays.iter().filter(|r| r.has_ink()).count()
    }

    /// Rays a fixed 10-unit comb would need for the same ink height.
    pub fn fixed_comb_equivalent(&self, interval: f64) -> usize {
        let h: f64 = self.ink_segments().map(|(a, b)| (b.y - a.y).abs()).sum();
        (h / interval).ceil() as usize + 1
    }

    /// Profile x at `y` by linear interpolation between rays.
    pub fn eval(&self, y: f64) -> Option<f64> {
        let r = &self.rays;
        let n = r.len();
        if n == 0 || y < r[0].y || y > r[n - 1].y {
            return None;
        }
        let k = r.partition_point(|q| q.y < y);
        if k < n && r[k].y == y {
            return r[k].has_ink().then_some(r[k].x);
        }
        if k == 0 {
            return None;
        }
        let (a, b) = (r[k - 1], r[k]);
        if !(a.has_ink() && b.has_ink()) {
            return None;
        }
        let t = (y - a.y) / (b.y - a.y);
        Some(a.x + t * (b.x - a.x))
    }

    /// Ink polyline pieces between consecutive inked rays.
    pub fn ink_segments(&self) -> impl Iterator<Item = (Vec2, Vec2)> + '_ {
        self.rays
            .windows(2)
            .filter(|w| w[0].has_ink() && w[1].has_ink())
            .map(|w| (w[0].pt(), w[1].pt()))
    }

    /// Euclidean distance from `p` to the profile polyline, capped at `cap`.
    #[inline]
    pub fn distance(&self, p: Vec2, cap: f64) -> f64 {
        self.distance_near(p, cap).0
    }

    /// `distance`, plus what lets a later query nearby skip the walk: the
    /// chunk holding the nearest piece (`u32::MAX` if none was within `cap`)
    /// and a lower bound of the distance to every piece outside it.
    ///
    /// Chunks are visited outward from `p.y`; the walk stops once the vertical
    /// offset alone exceeds the best distance, and a chunk whose bounds are
    /// farther than that is skipped without touching its pieces. Works on
    /// squared distances: √ is monotone and correctly rounded, so min(cap,
    /// √(min d²)) is exactly the minimum of the pieces' distances. A chunk is
    /// skipped only when it is farther than the best by a relative 1e-12, so
    /// rounding of the squares never skips one that could hold a nearer piece.
    pub fn distance_near(&self, p: Vec2, cap: f64) -> (f64, u32, f64) {
        let ch = &self.chunks;
        if ch.is_empty() {
            return (cap, u32::MAX, f64::INFINITY);
        }
        const SLACK: f64 = 1.0 + 1e-12;
        // Seed with the chunk at p's height (usually holds the nearest piece),
        // then walk outward over runs of chunks and, inside a run, over its
        // chunks, nearest first; both levels stop once the vertical offset
        // alone exceeds the best distance. The result is exactly min(cap,
        // distance to every piece) — only provably farther pieces are skipped.
        let kc = ch.partition_point(|c| c.ymax < p.y).min(ch.len() - 1);
        let mut w = ChunkWalk { best2: cap * cap, near: f64::INFINITY, chunk: u32::MAX, second: f64::INFINITY, skipped: f64::INFINITY };
        let beyond = |dy: f64, w: &mut ChunkWalk| {
            let far = dy > 0.0 && dy * dy > w.best2 * SLACK;
            if far {
                w.skipped = w.skipped.min(dy * dy);
            }
            far
        };
        let visit = |k: usize, w: &mut ChunkWalk| {
            let c = &ch[k];
            let b2 = c.bounds.distance2(p);
            if b2 <= w.best2 * SLACK {
                let m = self.scan_chunk(c, p);
                if m < w.near {
                    w.second = w.near;
                    w.near = m;
                    w.chunk = k as u32;
                } else {
                    w.second = w.second.min(m);
                }
                w.best2 = w.best2.min(m);
            } else {
                w.skipped = w.skipped.min(b2);
            }
        };
        visit(kc, &mut w);
        let sup = &self.supers;
        let ks = sup.partition_point(|s| s.last < kc).min(sup.len() - 1);
        // the run holding the seed: outward from it
        {
            let s = &sup[ks];
            for k in kc + 1..=s.last.max(kc) {
                if beyond(ch[k].ymin - p.y, &mut w) {
                    break;
                }
                visit(k, &mut w);
            }
            for k in (s.first.min(kc)..kc).rev() {
                if beyond(p.y - ch[k].ymax, &mut w) {
                    break;
                }
                visit(k, &mut w);
            }
        }
        for s in &sup[ks + 1..] {
            if beyond(s.ymin - p.y, &mut w) {
                break;
            }
            let b2 = s.bounds.distance2(p);
            if b2 > w.best2 * SLACK {
                w.skipped = w.skipped.min(b2);
                continue;
            }
            for k in s.first..=s.last {
                if beyond(ch[k].ymin - p.y, &mut w) {
                    break;
                }
                visit(k, &mut w);
            }
        }
        for s in sup[..ks].iter().rev() {
            if beyond(p.y - s.ymax, &mut w) {
                break;
            }
            let b2 = s.bounds.distance2(p);
            if b2 > w.best2 * SLACK {
                w.skipped = w.skipped.min(b2);
                continue;
            }
            for k in (s.first..=s.last).rev() {
                if beyond(p.y - ch[k].ymax, &mut w) {
                    break;
                }
                visit(k, &mut w);
            }
        }
        (cap.min(w.near.sqrt()), w.chunk, w.second.min(w.skipped).sqrt())
    }

    /// Distance from `p` to the nearest piece of chunk `k`, as `distance`
    /// computes it.
    #[inline]
    pub fn chunk_distance(&self, k: u32, p: Vec2) -> f64 {
        self.scan_chunk(&self.chunks[k as usize], p).sqrt()
    }

    /// The smallest squared distance from `p` to the chunk's pieces (∞ if none).
    #[inline]
    fn scan_chunk(&self, c: &Chunk, p: Vec2) -> f64 {
        let r = &self.rays;
        let mut best = f64::INFINITY;
        for i in c.first..c.last {
            if r[i].has_ink() && r[i + 1].has_ink() {
                best = best.min(dist2_point_seg(p, r[i].pt(), r[i + 1].pt()));
            }
        }
        best
    }
}

/// State of one distance walk (squared distances): the pruning bound, the
/// nearest chunk and its distance, the next nearest scanned chunk and the
/// nearest skipped chunk or run (its bounds, or the vertical offset that
/// ended a walk).
struct ChunkWalk {
    best2: f64,
    near: f64,
    chunk: u32,
    second: f64,
    skipped: f64,
}

/// Marches a y-monotone piece `[ta, tb]` of a curve, emitting interior rays.
fn march(plan: &RayPlan, seg: &Segment, ta: f64, tb: f64, out: &mut Vec<(f64, u8)>) {
    let ya = seg.eval(ta).y;
    let yb = seg.eval(tb).y;
    let finest = plan.intervals[CURVATURE_TIERS - 1];
    if (yb - ya).abs() < finest {
        return;
    }
    let dir = if yb >= ya { 1.0 } else { -1.0 };
    let (mut y, mut t) = (ya, ta);
    for _ in 0..200_000 {
        let (h0, l0) = plan.interval_at(seg, t);
        // look ahead: if the curve bends harder at the predicted ray, use its tier
        let y_pred = y + dir * h0;
        let (h, tier) = if (yb - y_pred) * dir > 0.0 {
            let (h1, l1) = plan.interval_at(seg, solve_t(seg, y_pred, t, tb));
            if h1 < h0 {
                (h1, l1)
            } else {
                (h0, l0)
            }
        } else {
            (h0, l0)
        };
        let y_next = y + dir * h;
        if (yb - y_next) * dir < 0.25 * h {
            break; // the end node's ray covers the rest
        }
        t = solve_t(seg, y_next, t, tb);
        y = y_next;
        out.push((y, tier));
    }
}

/// Parameter in the y-monotone range `[lo, hi]` where the curve reaches `y`.
fn solve_t(seg: &Segment, y: f64, mut lo: f64, mut hi: f64) -> f64 {
    let inc = seg.eval(hi).y >= seg.eval(lo).y;
    for _ in 0..64 {
        let mid = 0.5 * (lo + hi);
        if (seg.eval(mid).y < y) == inc {
            lo = mid;
        } else {
            hi = mid;
        }
        if hi - lo < 1e-14 {
            break;
        }
    }
    0.5 * (lo + hi)
}

/// Monotone evaluator for increasing y queries.
struct Cursor<'a> {
    rays: &'a [Ray],
    k: usize,
}

impl<'a> Cursor<'a> {
    fn new(p: &'a SdfProfile) -> Self {
        Cursor { rays: &p.rays, k: 0 }
    }
    fn eval(&mut self, y: f64) -> f64 {
        let r = self.rays;
        let n = r.len();
        if n == 0 || y < r[0].y || y > r[n - 1].y {
            return f64::NAN;
        }
        while self.k < n && r[self.k].y < y {
            self.k += 1;
        }
        let k = self.k;
        if k < n && r[k].y == y {
            return r[k].x;
        }
        if k == 0 || k >= n {
            return f64::NAN;
        }
        let (a, b) = (r[k - 1], r[k]);
        if !(a.has_ink() && b.has_ink()) {
            return f64::NAN;
        }
        a.x + (y - a.y) / (b.y - a.y) * (b.x - a.x)
    }
}

/// The merged adaptive ray array of a pair: glyph A's right profile against
/// glyph B's left profile, both evaluated at the union of their ray heights.
#[derive(Clone, Debug, Default)]
pub struct PairBand {
    pub ys: Vec<f64>,
    /// A's right-profile x (A coordinates); NaN where A has no ink.
    pub xa: Vec<f64>,
    /// B's left-profile x (B coordinates); NaN where B has no ink.
    pub xb: Vec<f64>,
    /// Trapezoid weights of A's and B's ink at each height.
    pub wa: Vec<f64>,
    pub wb: Vec<f64>,
}

impl PairBand {
    pub fn merge(a: &SdfProfile, b: &SdfProfile) -> Self {
        let (ra, rb) = (&a.rays, &b.rays);
        let mut ys: Vec<f64> = Vec::with_capacity(ra.len() + rb.len());
        let (mut i, mut j) = (0, 0);
        while i < ra.len() || j < rb.len() {
            let take_a = j >= rb.len() || (i < ra.len() && ra[i].y <= rb[j].y);
            let y = if take_a {
                i += 1;
                ra[i - 1].y
            } else {
                j += 1;
                rb[j - 1].y
            };
            if ys.last().map_or(true, |&l| y - l > 1e-7) {
                ys.push(y);
            }
        }
        let mut ca = Cursor::new(a);
        let mut cb = Cursor::new(b);
        let xa: Vec<f64> = ys.iter().map(|&y| ca.eval(y)).collect();
        let xb: Vec<f64> = ys.iter().map(|&y| cb.eval(y)).collect();
        let n = ys.len();
        let mut wa = vec![0.0; n];
        let mut wb = vec![0.0; n];
        for k in 0..n.saturating_sub(1) {
            let half = 0.5 * (ys[k + 1] - ys[k]);
            if xa[k].is_finite() && xa[k + 1].is_finite() {
                wa[k] += half;
                wa[k + 1] += half;
            }
            if xb[k].is_finite() && xb[k + 1].is_finite() {
                wb[k] += half;
                wb[k + 1] += half;
            }
        }
        PairBand { ys, xa, xb, wa, wb }
    }

    /// `merge` into existing buffers (no allocation once they have grown).
    /// Produces exactly the arrays `merge` does.
    pub fn merge_into(&mut self, a: &SdfProfile, b: &SdfProfile) {
        let (ra, rb) = (&a.rays, &b.rays);
        self.ys.clear();
        self.xa.clear();
        self.xb.clear();
        self.wa.clear();
        self.wb.clear();
        let ys = &mut self.ys;
        let (mut i, mut j) = (0, 0);
        while i < ra.len() || j < rb.len() {
            let take_a = j >= rb.len() || (i < ra.len() && ra[i].y <= rb[j].y);
            let y = if take_a {
                i += 1;
                ra[i - 1].y
            } else {
                j += 1;
                rb[j - 1].y
            };
            if ys.last().map_or(true, |&l| y - l > 1e-7) {
                ys.push(y);
            }
        }
        let mut ca = Cursor::new(a);
        let mut cb = Cursor::new(b);
        self.xa.extend(self.ys.iter().map(|&y| ca.eval(y)));
        self.xb.extend(self.ys.iter().map(|&y| cb.eval(y)));
        let n = self.ys.len();
        self.wa.resize(n, 0.0);
        self.wb.resize(n, 0.0);
        for k in 0..n.saturating_sub(1) {
            let half = 0.5 * (self.ys[k + 1] - self.ys[k]);
            if self.xa[k].is_finite() && self.xa[k + 1].is_finite() {
                self.wa[k] += half;
                self.wa[k + 1] += half;
            }
            if self.xb[k].is_finite() && self.xb[k + 1].is_finite() {
                self.wb[k] += half;
                self.wb[k + 1] += half;
            }
        }
    }

    pub fn len(&self) -> usize {
        self.ys.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ys.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geometry::{NODE_CURVE, NODE_LINE, NODE_OFFCURVE};

    fn rect(x0: f64, y0: f64, x1: f64, y1: f64) -> Vec<(Vec2, u32)> {
        vec![
            (Vec2::new(x0, y0), NODE_LINE),
            (Vec2::new(x1, y0), NODE_LINE),
            (Vec2::new(x1, y1), NODE_LINE),
            (Vec2::new(x0, y1), NODE_LINE),
        ]
    }

    fn circle(cx: f64, cy: f64, r: f64) -> Vec<(Vec2, u32)> {
        let k = 0.5522847498 * r;
        let p = |x: f64, y: f64, t| (Vec2::new(cx + x, cy + y), t);
        vec![
            p(r, 0.0, NODE_CURVE),
            p(r, k, NODE_OFFCURVE),
            p(k, r, NODE_OFFCURVE),
            p(0.0, r, NODE_CURVE),
            p(-k, r, NODE_OFFCURVE),
            p(-r, k, NODE_OFFCURVE),
            p(-r, 0.0, NODE_CURVE),
            p(-r, -k, NODE_OFFCURVE),
            p(-k, -r, NODE_OFFCURVE),
            p(0.0, -r, NODE_CURVE),
            p(k, -r, NODE_OFFCURVE),
            p(r, -k, NODE_OFFCURVE),
        ]
    }

    #[test]
    fn stem_needs_only_structural_rays() {
        let o = Outline::from_contours(&[rect(0.0, 0.0, 80.0, 700.0)], 0.05);
        let p = SdfProfile::build(&o, Side::Right, &RayPlan::for_upm(1000.0));
        assert!(p.rays.len() <= 4, "a vertical stem is two rays: {:?}", p.rays);
        assert!(p.rays.iter().all(|r| r.tier == TIER_STRUCTURAL && (r.x - 80.0).abs() < 1e-9));
        assert!(p.fixed_comb_equivalent(10.0) >= 70);
    }

    #[test]
    fn bowl_tightens_where_it_flattens() {
        let o = Outline::from_contours(&[circle(0.0, 250.0, 250.0)], 0.02);
        let p = SdfProfile::build(&o, Side::Right, &RayPlan::for_upm(1000.0));
        // The ray polyline stays within tolerance of the true bowl everywhere
        // (Euclidean: near the poles x(y) is steep, but the chords hug the arc).
        for i in 0..=720 {
            let th = -std::f64::consts::FRAC_PI_2 + std::f64::consts::PI * i as f64 / 720.0;
            let q = Vec2::new(250.0 * th.cos(), 250.0 + 250.0 * th.sin());
            let d = p.distance(q, 1e9);
            assert!(d < 0.35, "theta={th} dist={d}");
        }
        for i in 0..=380 {
            let y = 60.0 + i as f64;
            let exact = (250.0f64.powi(2) - (y - 250.0).powi(2)).sqrt();
            assert!((p.eval(y).unwrap() - exact).abs() < 0.35, "y={y}");
        }
        let fine = p.rays.iter().filter(|r| r.tier == CURVATURE_TIERS as u8).count();
        let coarse = p.rays.iter().filter(|r| r.tier == 1).count();
        assert!(fine > 0 && coarse > 0, "tiers used: fine {fine}, coarse {coarse}");
        // the 1-unit rays sit near the top and bottom, the 10-unit ones in the middle
        for r in &p.rays {
            if r.tier == CURVATURE_TIERS as u8 {
                assert!((r.y - 250.0).abs() > 200.0, "fine ray at {}", r.y);
            }
            if r.tier == 1 {
                assert!((r.y - 250.0).abs() < 215.0, "coarse ray at {}", r.y);
            }
        }
        // counter rays of an 'o' are hidden and dropped
        let o2 = Outline::from_contours(&[circle(0.0, 250.0, 250.0), circle(0.0, 250.0, 150.0)], 0.02);
        let p2 = SdfProfile::build(&o2, Side::Right, &RayPlan::for_upm(1000.0));
        assert!(p2.rays.len() <= p.rays.len() + 12);
    }

    #[test]
    fn t_arm_jump_and_i_gap() {
        // T: arm 0..600 at 620..700, stem 260..340
        let t = Outline::from_contours(
            &[vec![
                (Vec2::new(260.0, 0.0), NODE_LINE),
                (Vec2::new(340.0, 0.0), NODE_LINE),
                (Vec2::new(340.0, 620.0), NODE_LINE),
                (Vec2::new(600.0, 620.0), NODE_LINE),
                (Vec2::new(600.0, 700.0), NODE_LINE),
                (Vec2::new(0.0, 700.0), NODE_LINE),
                (Vec2::new(0.0, 620.0), NODE_LINE),
                (Vec2::new(260.0, 620.0), NODE_LINE),
            ]],
            0.05,
        );
        let p = SdfProfile::build(&t, Side::Right, &RayPlan::for_upm(1000.0));
        assert!((p.eval(300.0).unwrap() - 340.0).abs() < 1e-6);
        assert!((p.eval(650.0).unwrap() - 600.0).abs() < 1e-6);
        assert!((p.eval(619.9).unwrap() - 340.0).abs() < 1e-6);
        // i: stem 0..500, dot 600..700
        let i = Outline::from_contours(&[rect(0.0, 0.0, 80.0, 500.0), rect(0.0, 600.0, 80.0, 700.0)], 0.05);
        let pi = SdfProfile::build(&i, Side::Left, &RayPlan::for_upm(1000.0));
        assert!(pi.eval(550.0).is_none());
        assert!(pi.eval(499.0).is_some() && pi.eval(601.0).is_some());
    }

    #[test]
    fn merged_band_integrates_both_profiles() {
        let a = Outline::from_contours(&[rect(0.0, 0.0, 80.0, 700.0)], 0.05);
        let b = Outline::from_contours(&[circle(250.0, 250.0, 250.0)], 0.02);
        let plan = RayPlan::for_upm(1000.0);
        let pa = SdfProfile::build(&a, Side::Right, &plan);
        let pb = SdfProfile::build(&b, Side::Left, &plan);
        let band = PairBand::merge(&pa, &pb);
        let ha: f64 = band.wa.iter().sum();
        let hb: f64 = band.wb.iter().sum();
        assert!((ha - 700.0).abs() < 1e-6, "A ink height {ha}");
        assert!((hb - 500.0).abs() < 0.2, "B ink height {hb}");
        // every B ray height is present, so the stem is sampled at the bowl's comb
        assert!(band.len() >= pb.rays.len());
        assert!(band.xa.iter().all(|x| (x - 80.0).abs() < 1e-9));
    }

    #[test]
    fn merge_into_matches_merge() {
        let a = Outline::from_contours(&[rect(0.0, 0.0, 80.0, 700.0)], 0.05);
        let b = Outline::from_contours(&[circle(250.0, 250.0, 250.0)], 0.02);
        let plan = RayPlan::for_upm(1000.0);
        let pa = SdfProfile::build(&a, Side::Right, &plan);
        let pb = SdfProfile::build(&b, Side::Left, &plan);
        let fresh = PairBand::merge(&pa, &pb);
        let mut reused = PairBand::merge(&pb, &pa); // dirty buffers of another size
        reused.merge_into(&pa, &pb);
        let bits = |v: &[f64]| v.iter().map(|x| x.to_bits()).collect::<Vec<_>>();
        assert_eq!(bits(&fresh.ys), bits(&reused.ys));
        assert_eq!(bits(&fresh.xa), bits(&reused.xa));
        assert_eq!(bits(&fresh.xb), bits(&reused.xb));
        assert_eq!(bits(&fresh.wa), bits(&reused.wa));
        assert_eq!(bits(&fresh.wb), bits(&reused.wb));
    }

    #[test]
    fn distance_to_profile() {
        let a = Outline::from_contours(&[rect(0.0, 0.0, 80.0, 700.0)], 0.05);
        let pa = SdfProfile::build(&a, Side::Right, &RayPlan::for_upm(1000.0));
        assert!((pa.distance(Vec2::new(100.0, 300.0), 1e9) - 20.0).abs() < 1e-9);
        assert!((pa.distance(Vec2::new(83.0, 704.0), 1e9) - 5.0).abs() < 1e-9);
        assert_eq!(pa.distance(Vec2::new(1000.0, 300.0), 50.0), 50.0);
    }
}

