//! Planar geometry shared by every pass of the engine.
//!
//! Outlines arrive as Glyphs nodes (line / curve / off-curve / qcurve) and are
//! rebuilt as exact Bézier [`Segment`]s. Raycasting and inside tests run on a
//! flattened copy whose [`Edge`]s remember the segment they came from, while the
//! adaptive ray planner in `profile.rs` reads the analytic derivatives of the
//! segments themselves. The capsule helpers at the bottom give *exact* contact
//! shifts between polylines swept horizontally, which the collision and crevice
//! constraints of Pass 2 are built on.

use std::cmp::Ordering;
use std::ops::{Add, Mul, Neg, Sub};

/// Node kinds as sent by the plugin (mirrors `GSNode.type`).
pub const NODE_LINE: u32 = 0;
#[allow(dead_code)] // listed for the ABI; a curve node is any on-curve node after two handles
pub const NODE_CURVE: u32 = 1;
pub const NODE_OFFCURVE: u32 = 2;
pub const NODE_QCURVE: u32 = 3;

pub(crate) const EPS: f64 = 1e-9;

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Vec2 {
    pub x: f64,
    pub y: f64,
}

impl Vec2 {
    #[inline]
    pub const fn new(x: f64, y: f64) -> Self {
        Self { x, y }
    }
    #[inline]
    pub fn dot(self, o: Self) -> f64 {
        self.x * o.x + self.y * o.y
    }
    #[inline]
    pub fn cross(self, o: Self) -> f64 {
        self.x * o.y - self.y * o.x
    }
    #[inline]
    pub fn len2(self) -> f64 {
        self.dot(self)
    }
    #[inline]
    pub fn len(self) -> f64 {
        self.len2().sqrt()
    }
    #[inline]
    pub fn lerp(self, o: Self, t: f64) -> Self {
        self + (o - self) * t
    }
    #[inline]
    pub fn normalized(self) -> Option<Self> {
        let l = self.len();
        (l > EPS).then(|| self * (1.0 / l))
    }
    #[inline]
    pub fn is_finite(self) -> bool {
        self.x.is_finite() && self.y.is_finite()
    }
}

impl Add for Vec2 {
    type Output = Vec2;
    #[inline]
    fn add(self, o: Vec2) -> Vec2 {
        Vec2::new(self.x + o.x, self.y + o.y)
    }
}
impl Sub for Vec2 {
    type Output = Vec2;
    #[inline]
    fn sub(self, o: Vec2) -> Vec2 {
        Vec2::new(self.x - o.x, self.y - o.y)
    }
}
impl Mul<f64> for Vec2 {
    type Output = Vec2;
    #[inline]
    fn mul(self, s: f64) -> Vec2 {
        Vec2::new(self.x * s, self.y * s)
    }
}
impl Neg for Vec2 {
    type Output = Vec2;
    #[inline]
    fn neg(self) -> Vec2 {
        Vec2::new(-self.x, -self.y)
    }
}

/// `f64` with a total order, for heaps and sorts.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct OrdF64(pub f64);
impl Eq for OrdF64 {}
impl PartialOrd for OrdF64 {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for OrdF64 {
    fn cmp(&self, o: &Self) -> Ordering {
        self.0.total_cmp(&o.0)
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct BBox {
    pub x0: f64,
    pub y0: f64,
    pub x1: f64,
    pub y1: f64,
}

impl BBox {
    pub const EMPTY: BBox = BBox {
        x0: f64::INFINITY,
        y0: f64::INFINITY,
        x1: f64::NEG_INFINITY,
        y1: f64::NEG_INFINITY,
    };
    #[inline]
    pub fn include(&mut self, p: Vec2) {
        self.x0 = self.x0.min(p.x);
        self.y0 = self.y0.min(p.y);
        self.x1 = self.x1.max(p.x);
        self.y1 = self.y1.max(p.y);
    }
    #[inline]
    pub fn is_valid(&self) -> bool {
        self.x0 <= self.x1 && self.y0 <= self.y1
    }
    #[inline]
    pub fn width(&self) -> f64 {
        (self.x1 - self.x0).max(0.0)
    }
    #[inline]
    pub fn height(&self) -> f64 {
        (self.y1 - self.y0).max(0.0)
    }
    /// Euclidean distance from `p` to the box (0 inside).
    #[inline]
    pub fn distance(&self, p: Vec2) -> f64 {
        self.distance2(p).sqrt()
    }
    /// Its square.
    #[inline]
    pub fn distance2(&self, p: Vec2) -> f64 {
        let dx = (self.x0 - p.x).max(p.x - self.x1).max(0.0);
        let dy = (self.y0 - p.y).max(p.y - self.y1).max(0.0);
        dx * dx + dy * dy
    }
}

/// One Bézier piece of a contour.
#[derive(Clone, Copy, Debug)]
pub enum Segment {
    Line(Vec2, Vec2),
    Quad(Vec2, Vec2, Vec2),
    Cubic(Vec2, Vec2, Vec2, Vec2),
}

impl Segment {
    #[inline]
    pub fn start(&self) -> Vec2 {
        match *self {
            Segment::Line(a, _) | Segment::Quad(a, _, _) | Segment::Cubic(a, _, _, _) => a,
        }
    }
    #[inline]
    pub fn end(&self) -> Vec2 {
        match *self {
            Segment::Line(_, b) => b,
            Segment::Quad(_, _, c) => c,
            Segment::Cubic(_, _, _, d) => d,
        }
    }

    pub fn eval(&self, t: f64) -> Vec2 {
        let mt = 1.0 - t;
        match *self {
            Segment::Line(a, b) => a.lerp(b, t),
            Segment::Quad(a, b, c) => a * (mt * mt) + b * (2.0 * mt * t) + c * (t * t),
            Segment::Cubic(a, b, c, d) => {
                a * (mt * mt * mt) + b * (3.0 * mt * mt * t) + c * (3.0 * mt * t * t) + d * (t * t * t)
            }
        }
    }

    /// First derivative with respect to the curve parameter.
    pub fn d1(&self, t: f64) -> Vec2 {
        let mt = 1.0 - t;
        match *self {
            Segment::Line(a, b) => b - a,
            Segment::Quad(a, b, c) => (b - a) * (2.0 * mt) + (c - b) * (2.0 * t),
            Segment::Cubic(a, b, c, d) => {
                (b - a) * (3.0 * mt * mt) + (c - b) * (6.0 * mt * t) + (d - c) * (3.0 * t * t)
            }
        }
    }

    /// Second derivative with respect to the curve parameter.
    pub fn d2(&self, t: f64) -> Vec2 {
        match *self {
            Segment::Line(..) => Vec2::default(),
            Segment::Quad(a, b, c) => (a - b * 2.0 + c) * 2.0,
            Segment::Cubic(a, b, c, d) => {
                (c - b * 2.0 + a) * (6.0 * (1.0 - t)) + (d - c * 2.0 + b) * (6.0 * t)
            }
        }
    }

    /// A line, or a curve whose handles lie on its chord. Straight pieces are
    /// sampled at their ends only: their profile is exactly linear in y.
    pub fn is_straight(&self) -> bool {
        let (a, b) = (self.start(), self.end());
        let chord = b - a;
        let len = chord.len();
        let off = |p: Vec2| -> bool {
            if len < EPS {
                (p - a).len() > 1e-6
            } else {
                (chord.cross(p - a) / len).abs() > 1e-6 * (1.0 + len)
            }
        };
        match *self {
            Segment::Line(..) => true,
            Segment::Quad(_, c, _) => !off(c),
            Segment::Cubic(_, c1, c2, _) => !off(c1) && !off(c2),
        }
    }

    pub fn is_degenerate(&self) -> bool {
        let a = self.start();
        let close = |p: Vec2| (p - a).len2() < 1e-18;
        match *self {
            Segment::Line(_, b) => close(b),
            Segment::Quad(_, b, c) => close(b) && close(c),
            Segment::Cubic(_, b, c, d) => close(b) && close(c) && close(d),
        }
    }

    fn extrema(&self, pick: fn(Vec2) -> f64) -> Vec<f64> {
        let mut out = Vec::new();
        match *self {
            Segment::Line(..) => {}
            Segment::Quad(a, b, c) => {
                let den = pick(a) - 2.0 * pick(b) + pick(c);
                if den.abs() > EPS {
                    push_unit(&mut out, (pick(a) - pick(b)) / den);
                }
            }
            Segment::Cubic(a, b, c, d) => {
                // B'(t) / 3 = qa t² + qb t + qc
                let (p0, p1, p2, p3) = (pick(a), pick(b), pick(c), pick(d));
                let qa = p3 - 3.0 * p2 + 3.0 * p1 - p0;
                let qb = 2.0 * (p2 - 2.0 * p1 + p0);
                let qc = p1 - p0;
                solve_quadratic(qa, qb, qc, |t| push_unit(&mut out, t));
            }
        }
        out.sort_by(f64::total_cmp);
        out.dedup_by(|a, b| (*a - *b).abs() < 1e-12);
        out
    }

    /// Parameters in (0, 1) where y'(t) = 0.
    pub fn y_extrema(&self) -> Vec<f64> {
        self.extrema(|p| p.y)
    }
    /// Parameters in (0, 1) where x'(t) = 0.
    pub fn x_extrema(&self) -> Vec<f64> {
        self.extrema(|p| p.x)
    }

    /// Uniform flattening with the classic second-difference bound: the chord
    /// error of n pieces is at most max|B''| / (8 n²). Pushes the end points of
    /// the pieces (not the segment start).
    pub fn flatten(&self, tol: f64, out: &mut Vec<Vec2>) {
        let n = match *self {
            Segment::Line(..) => 1,
            Segment::Quad(a, b, c) => {
                let m = (a - b * 2.0 + c).len();
                ((0.25 * m / tol).sqrt().ceil() as usize).clamp(1, 256)
            }
            Segment::Cubic(a, b, c, d) => {
                let m = (a - b * 2.0 + c).len().max((b - c * 2.0 + d).len());
                ((0.75 * m / tol).sqrt().ceil() as usize).clamp(1, 512)
            }
        };
        for i in 1..=n {
            out.push(self.eval(i as f64 / n as f64));
        }
    }
}

fn push_unit(out: &mut Vec<f64>, t: f64) {
    if t > 1e-9 && t < 1.0 - 1e-9 {
        out.push(t);
    }
}

/// Real roots of a t² + b t + c (numerically stable form).
fn solve_quadratic(a: f64, b: f64, c: f64, mut f: impl FnMut(f64)) {
    let scale = a.abs().max(b.abs()).max(c.abs());
    if scale < 1e-300 {
        return;
    }
    if a.abs() <= 1e-12 * scale {
        if b.abs() > 1e-12 * scale {
            f(-c / b);
        }
        return;
    }
    let disc = b * b - 4.0 * a * c;
    if disc < 0.0 {
        return;
    }
    let q = -0.5 * (b + b.signum() * disc.sqrt());
    if q != 0.0 {
        f(q / a);
        f(c / q);
    } else {
        f(0.0);
    }
}

/// A flattened edge that remembers its source segment.
#[derive(Clone, Copy, Debug)]
pub struct Edge {
    pub a: Vec2,
    pub b: Vec2,
    pub seg: u32,
}

/// Result of a closed-interval horizontal scan: the extreme ink crossings and
/// the segments that own them.
#[derive(Clone, Copy, Debug)]
pub struct ScanHit {
    pub xmin: f64,
    pub seg_min: u32,
    pub xmax: f64,
    pub seg_max: u32,
}

/// A glyph outline: exact segments plus a flattened, closed polyline copy.
#[derive(Clone, Debug)]
pub struct Outline {
    pub segments: Vec<Segment>,
    /// Edge index range `[start, end)` of every segment.
    pub seg_edges: Vec<(u32, u32)>,
    /// Edge index range of every contour (closed rings).
    pub contour_edges: Vec<(u32, u32)>,
    pub edges: Vec<Edge>,
    pub bbox: BBox,
}

impl Outline {
    /// Builds an outline from contours of `(position, node kind)`. Contours are
    /// closed cyclically; off-curve points before an on-curve node belong to the
    /// segment that ends at that node (Glyphs' convention).
    pub fn from_contours(contours: &[Vec<(Vec2, u32)>], flatten_tol: f64) -> Outline {
        let mut segments = Vec::new();
        let mut contour_segs = Vec::with_capacity(contours.len());
        for c in contours {
            let s0 = segments.len();
            build_contour_segments(c, &mut segments);
            if segments.len() > s0 {
                contour_segs.push((s0, segments.len()));
            }
        }
        let mut edges = Vec::new();
        let mut seg_edges = Vec::with_capacity(segments.len());
        let mut contour_edges = Vec::with_capacity(contour_segs.len());
        let mut bbox = BBox::EMPTY;
        let mut pts = Vec::new();
        for &(s0, s1) in &contour_segs {
            let e0 = edges.len() as u32;
            for (si, seg) in segments.iter().enumerate().take(s1).skip(s0) {
                let start = edges.len() as u32;
                pts.clear();
                seg.flatten(flatten_tol, &mut pts);
                let mut prev = seg.start();
                bbox.include(prev);
                for &p in &pts {
                    if (p - prev).len2() > 0.0 {
                        edges.push(Edge { a: prev, b: p, seg: si as u32 });
                    }
                    prev = p;
                }
                // exact extrema keep the bbox tight for curves
                for t in seg.x_extrema().into_iter().chain(seg.y_extrema()) {
                    bbox.include(seg.eval(t));
                }
                bbox.include(seg.end());
                seg_edges.push((start, edges.len() as u32));
            }
            contour_edges.push((e0, edges.len() as u32));
        }
        Outline { segments, seg_edges, contour_edges, edges, bbox }
    }

    pub fn is_empty(&self) -> bool {
        self.edges.is_empty() || !self.bbox.is_valid()
    }

    /// Nonzero winding number of `p` (half-open crossing rule).
    pub fn winding(&self, p: Vec2) -> i32 {
        let mut wn = 0;
        for e in &self.edges {
            if e.a.y <= p.y {
                if e.b.y > p.y && (e.b - e.a).cross(p - e.a) > 0.0 {
                    wn += 1;
                }
            } else if e.b.y <= p.y && (e.b - e.a).cross(p - e.a) < 0.0 {
                wn -= 1;
            }
        }
        wn
    }

    #[inline]
    pub fn contains(&self, p: Vec2) -> bool {
        self.winding(p) != 0
    }

    /// Ink intervals `[x_start, x_end]` on the scanline `y` under the nonzero
    /// rule, sorted by x. Used to enumerate counter (inner white) candidates.
    pub fn ink_intervals(&self, y: f64) -> Vec<(f64, f64)> {
        let mut xs: Vec<(f64, i32)> = Vec::new();
        for e in &self.edges {
            let up = e.a.y <= y && e.b.y > y;
            let down = e.b.y <= y && e.a.y > y;
            if up || down {
                let t = (y - e.a.y) / (e.b.y - e.a.y);
                xs.push((e.a.x + t * (e.b.x - e.a.x), if up { 1 } else { -1 }));
            }
        }
        xs.sort_by(|a, b| a.0.total_cmp(&b.0));
        let mut out = Vec::new();
        let mut wn = 0;
        let mut start = 0.0;
        for (x, d) in xs {
            let was = wn != 0;
            wn += d;
            let is = wn != 0;
            if !was && is {
                start = x;
            } else if was && !is {
                out.push((start, x));
            }
        }
        out
    }

    /// Extreme ink crossings of the closed scanline `y` (horizontal edges lying
    /// on the line count with their full extent).
    pub fn scan(&self, y: f64) -> Option<ScanHit> {
        scan_edges(&self.edges, y)
    }

    /// Like [`Outline::scan`] but restricted to the edges of one segment.
    pub fn scan_segment(&self, seg: usize, y: f64) -> Option<ScanHit> {
        let (a, b) = self.seg_edges[seg];
        scan_edges(&self.edges[a as usize..b as usize], y)
    }
}

fn scan_edges(edges: &[Edge], y: f64) -> Option<ScanHit> {
    let mut hit: Option<ScanHit> = None;
    for e in edges {
        let (ylo, yhi) = if e.a.y <= e.b.y { (e.a.y, e.b.y) } else { (e.b.y, e.a.y) };
        if y < ylo || y > yhi {
            continue;
        }
        let dy = e.b.y - e.a.y;
        let (xl, xr) = if dy.abs() < 1e-12 {
            (e.a.x.min(e.b.x), e.a.x.max(e.b.x))
        } else {
            let t = ((y - e.a.y) / dy).clamp(0.0, 1.0);
            let x = e.a.x + t * (e.b.x - e.a.x);
            (x, x)
        };
        match hit.as_mut() {
            None => {
                hit = Some(ScanHit { xmin: xl, seg_min: e.seg, xmax: xr, seg_max: e.seg });
            }
            Some(h) => {
                if xl < h.xmin {
                    h.xmin = xl;
                    h.seg_min = e.seg;
                }
                if xr > h.xmax {
                    h.xmax = xr;
                    h.seg_max = e.seg;
                }
            }
        }
    }
    hit
}

fn build_contour_segments(nodes: &[(Vec2, u32)], out: &mut Vec<Segment>) {
    let n = nodes.len();
    if n < 2 {
        return;
    }
    let mut seq: Vec<(Vec2, u32)> = Vec::with_capacity(n + 2);
    match nodes.iter().position(|&(_, k)| k != NODE_OFFCURVE) {
        Some(s) => {
            seq.extend_from_slice(&nodes[s..]);
            seq.extend_from_slice(&nodes[..s]);
        }
        None => {
            // TrueType contour made only of off-curve points: start at an
            // implied on-curve midpoint.
            seq.push((nodes[n - 1].0.lerp(nodes[0].0, 0.5), NODE_QCURVE));
            seq.extend_from_slice(nodes);
        }
    }
    let first = seq[0];
    seq.push(first);
    let mut cur = seq[0].0;
    let mut offs: Vec<Vec2> = Vec::new();
    for &(p, kind) in &seq[1..] {
        if kind == NODE_OFFCURVE {
            offs.push(p);
            continue;
        }
        push_segments(cur, &offs, p, kind, out);
        offs.clear();
        cur = p;
    }
}

fn push_segments(from: Vec2, offs: &[Vec2], to: Vec2, kind: u32, out: &mut Vec<Segment>) {
    let single = match offs.len() {
        0 => Some(Segment::Line(from, to)),
        1 => Some(Segment::Quad(from, offs[0], to)),
        2 if kind != NODE_QCURVE => Some(Segment::Cubic(from, offs[0], offs[1], to)),
        _ => None,
    };
    if let Some(s) = single {
        if !s.is_degenerate() {
            out.push(s);
        }
        return;
    }
    // TrueType quadratic B-spline: implied on-curve points halfway between handles.
    let mut p = from;
    for i in 0..offs.len() {
        let end = if i + 1 < offs.len() { offs[i].lerp(offs[i + 1], 0.5) } else { to };
        let s = Segment::Quad(p, offs[i], end);
        if !s.is_degenerate() {
            out.push(s);
        }
        p = end;
    }
}

#[inline]
pub fn dist_point_seg(p: Vec2, a: Vec2, b: Vec2) -> f64 {
    dist2_point_seg(p, a, b).sqrt()
}

/// Its square (`dist_point_seg` is exactly its square root).
#[inline]
pub fn dist2_point_seg(p: Vec2, a: Vec2, b: Vec2) -> f64 {
    let ab = b - a;
    let l2 = ab.len2();
    // the clamped projection; the division only when it falls inside (same
    // values as clamping the quotient)
    let d = (p - a).dot(ab);
    let t = if d <= 0.0 || l2 <= 0.0 {
        0.0
    } else if d >= l2 {
        1.0
    } else {
        d / l2
    };
    (p - (a + ab * t)).len2()
}

/// Uniform bucket grid over line segments for nearest-distance queries.
pub struct SegmentGrid {
    bounds: BBox,
    cell: f64,
    nx: usize,
    ny: usize,
    cells: Vec<Vec<u32>>,
    segs: Vec<(Vec2, Vec2)>,
}

impl SegmentGrid {
    pub fn new(segs: Vec<(Vec2, Vec2)>, cell: f64) -> Self {
        let mut bounds = BBox::EMPTY;
        for &(a, b) in &segs {
            bounds.include(a);
            bounds.include(b);
        }
        if !bounds.is_valid() {
            return SegmentGrid { bounds, cell: 1.0, nx: 0, ny: 0, cells: Vec::new(), segs };
        }
        let mut cell = cell.max(1e-3);
        let (w, h) = (bounds.width().max(cell), bounds.height().max(cell));
        while (w / cell).ceil() * (h / cell).ceil() > 262_144.0 {
            cell *= 2.0;
        }
        let nx = ((w / cell).ceil() as usize).max(1);
        let ny = ((h / cell).ceil() as usize).max(1);
        let mut cells = vec![Vec::new(); nx * ny];
        for (i, &(a, b)) in segs.iter().enumerate() {
            let (cx0, cy0) = Self::cell_of(&bounds, cell, nx, ny, Vec2::new(a.x.min(b.x), a.y.min(b.y)));
            let (cx1, cy1) = Self::cell_of(&bounds, cell, nx, ny, Vec2::new(a.x.max(b.x), a.y.max(b.y)));
            for cy in cy0..=cy1 {
                for cx in cx0..=cx1 {
                    cells[cy * nx + cx].push(i as u32);
                }
            }
        }
        SegmentGrid { bounds, cell, nx, ny, cells, segs }
    }

    #[inline]
    fn cell_of(bounds: &BBox, cell: f64, nx: usize, ny: usize, p: Vec2) -> (usize, usize) {
        let cx = ((p.x - bounds.x0) / cell).floor().clamp(0.0, (nx - 1) as f64) as usize;
        let cy = ((p.y - bounds.y0) / cell).floor().clamp(0.0, (ny - 1) as f64) as usize;
        (cx, cy)
    }

    /// Distance from `p` to the nearest segment, or `cap` if none is closer.
    pub fn distance(&self, p: Vec2, cap: f64) -> f64 {
        if self.nx == 0 {
            return cap;
        }
        let box_d = self.bounds.distance(p);
        let mut best = cap;
        if box_d >= best {
            return best;
        }
        let (ci, cj) = Self::cell_of(&self.bounds, self.cell, self.nx, self.ny, p);
        let max_r = self.nx.max(self.ny);
        for r in 0..=max_r {
            let lower = box_d.max((r as f64 - 1.0).max(0.0) * self.cell);
            if lower >= best {
                break;
            }
            let (i0, i1) = (ci as isize - r as isize, ci as isize + r as isize);
            let (j0, j1) = (cj as isize - r as isize, cj as isize + r as isize);
            for j in j0..=j1 {
                if j < 0 || j >= self.ny as isize {
                    continue;
                }
                let on_edge_row = j == j0 || j == j1;
                let mut i = i0;
                while i <= i1 {
                    if i >= 0 && i < self.nx as isize {
                        for &s in &self.cells[j as usize * self.nx + i as usize] {
                            let (a, b) = self.segs[s as usize];
                            best = best.min(dist_point_seg(p, a, b));
                        }
                    }
                    // interior rows only visit the two ring columns
                    i = if on_edge_row || i == i1 { i + 1 } else { i1 };
                }
            }
        }
        best
    }
}

/// Horizontal extent of the δ-capsule around segment `a`–`b` on the line y = `y0`.
pub fn capsule_hspan(a: Vec2, b: Vec2, delta: f64, y0: f64) -> Option<(f64, f64)> {
    let mut lo = f64::INFINITY;
    let mut hi = f64::NEG_INFINITY;
    let mut add = |x: f64| {
        lo = lo.min(x);
        hi = hi.max(x);
    };
    for e in [a, b] {
        let dy = y0 - e.y;
        if dy.abs() <= delta {
            let h = (delta * delta - dy * dy).max(0.0).sqrt();
            add(e.x - h);
            add(e.x + h);
        }
    }
    let ab = b - a;
    let len = ab.len();
    if len > EPS {
        if ab.y.abs() > EPS {
            let n = Vec2::new(-ab.y / len, ab.x / len);
            for s in [-1.0, 1.0] {
                let off = n * (s * delta);
                let t = (y0 - (a.y + off.y)) / ab.y;
                if (0.0..=1.0).contains(&t) {
                    add(a.x + off.x + t * ab.x);
                }
            }
        } else if (y0 - a.y).abs() <= delta {
            add(a.x.min(b.x));
            add(a.x.max(b.x));
        }
    }
    (lo <= hi).then_some((lo, hi))
}

/// Largest horizontal shift `s` at which segment q (moved by +s) is still
/// within `delta` of segment p. `None` when no horizontal shift brings them that
/// close. For every larger shift the two segments are at least `delta` apart,
/// because the distance of a translated convex pair is convex in the shift.
pub fn contact_shift(p0: Vec2, p1: Vec2, q0: Vec2, q1: Vec2, delta: f64) -> Option<f64> {
    let mut best: Option<f64> = None;
    let mut bump = |s: f64| {
        best = Some(best.map_or(s, |b: f64| b.max(s)));
    };
    for q in [q0, q1] {
        if let Some((_, hi)) = capsule_hspan(p0, p1, delta, q.y) {
            bump(hi - q.x);
        }
    }
    for p in [p0, p1] {
        if let Some((lo, _)) = capsule_hspan(q0, q1, delta, p.y) {
            bump(p.x - lo);
        }
    }
    best
}

#[cfg(test)]
mod tests {
    use super::*;

    fn square(x0: f64, y0: f64, s: f64) -> Vec<(Vec2, u32)> {
        vec![
            (Vec2::new(x0, y0), NODE_LINE),
            (Vec2::new(x0 + s, y0), NODE_LINE),
            (Vec2::new(x0 + s, y0 + s), NODE_LINE),
            (Vec2::new(x0, y0 + s), NODE_LINE),
        ]
    }

    #[test]
    fn square_outline_bbox_and_winding() {
        let o = Outline::from_contours(&[square(0.0, 0.0, 100.0)], 0.1);
        assert_eq!(o.segments.len(), 4);
        assert_eq!(o.bbox, BBox { x0: 0.0, y0: 0.0, x1: 100.0, y1: 100.0 });
        assert!(o.contains(Vec2::new(50.0, 50.0)));
        assert!(!o.contains(Vec2::new(150.0, 50.0)));
        let iv = o.ink_intervals(50.0);
        assert_eq!(iv.len(), 1);
        assert!((iv[0].0 - 0.0).abs() < 1e-9 && (iv[0].1 - 100.0).abs() < 1e-9);
    }

    #[test]
    fn offcurve_before_first_oncurve_is_wrapped() {
        // A closed cubic contour whose node list starts with its handles, the
        // way Glyphs stores a path whose start node is the last one.
        let k = 0.5522847498 * 100.0;
        let nodes = vec![
            (Vec2::new(100.0, k), NODE_OFFCURVE),
            (Vec2::new(k, 100.0), NODE_OFFCURVE),
            (Vec2::new(0.0, 100.0), NODE_CURVE),
            (Vec2::new(-k, 100.0), NODE_OFFCURVE),
            (Vec2::new(-100.0, k), NODE_OFFCURVE),
            (Vec2::new(-100.0, 0.0), NODE_CURVE),
            (Vec2::new(-100.0, -k), NODE_OFFCURVE),
            (Vec2::new(-k, -100.0), NODE_OFFCURVE),
            (Vec2::new(0.0, -100.0), NODE_CURVE),
            (Vec2::new(k, -100.0), NODE_OFFCURVE),
            (Vec2::new(100.0, -k), NODE_OFFCURVE),
            (Vec2::new(100.0, 0.0), NODE_CURVE),
        ];
        let o = Outline::from_contours(&[nodes], 0.05);
        assert_eq!(o.segments.len(), 4);
        assert!((o.bbox.x0 + 100.0).abs() < 1e-6 && (o.bbox.y1 - 100.0).abs() < 1e-6);
        assert!(o.contains(Vec2::new(0.0, 0.0)));
        let h = o.scan(0.0).unwrap();
        assert!((h.xmax - 100.0).abs() < 0.06);
    }

    #[test]
    fn truetype_implied_points() {
        // Four off-curve points around a diamond: an all-off-curve quadratic ring.
        let nodes = vec![
            (Vec2::new(100.0, 0.0), NODE_OFFCURVE),
            (Vec2::new(0.0, 100.0), NODE_OFFCURVE),
            (Vec2::new(-100.0, 0.0), NODE_OFFCURVE),
            (Vec2::new(0.0, -100.0), NODE_OFFCURVE),
        ];
        let o = Outline::from_contours(&[nodes], 0.05);
        assert_eq!(o.segments.len(), 4);
        assert!(o.contains(Vec2::new(0.0, 0.0)));
    }

    #[test]
    fn capsule_span_matches_geometry() {
        let (lo, hi) = capsule_hspan(Vec2::new(0.0, 0.0), Vec2::new(0.0, 10.0), 2.0, 5.0).unwrap();
        assert!((lo + 2.0).abs() < 1e-9 && (hi - 2.0).abs() < 1e-9);
        let (lo, hi) = capsule_hspan(Vec2::new(0.0, 0.0), Vec2::new(10.0, 10.0), 1.0, 5.0).unwrap();
        assert!((hi - lo - 2.0 * 2f64.sqrt()).abs() < 1e-9);
        assert!(capsule_hspan(Vec2::new(0.0, 0.0), Vec2::new(0.0, 10.0), 2.0, 13.0).is_none());
    }

    #[test]
    fn contact_shift_parallel_stems() {
        // Two vertical stems: q must sit at least 5 units right of p.
        let s = contact_shift(
            Vec2::new(0.0, 0.0),
            Vec2::new(0.0, 100.0),
            Vec2::new(0.0, 0.0),
            Vec2::new(0.0, 100.0),
            5.0,
        )
        .unwrap();
        assert!((s - 5.0).abs() < 1e-9);
        // Vertically separated by more than delta: never in contact.
        assert!(contact_shift(
            Vec2::new(0.0, 0.0),
            Vec2::new(0.0, 10.0),
            Vec2::new(0.0, 20.0),
            Vec2::new(0.0, 30.0),
            5.0
        )
        .is_none());
    }

    #[test]
    fn grid_distance_matches_bruteforce() {
        let o = Outline::from_contours(&[square(0.0, 0.0, 100.0), square(30.0, 30.0, 40.0)], 0.1);
        let segs: Vec<_> = o.edges.iter().map(|e| (e.a, e.b)).collect();
        let grid = SegmentGrid::new(segs.clone(), 7.0);
        for &(x, y) in &[(5.0, 5.0), (50.0, 50.0), (-40.0, 20.0), (99.0, 150.0), (29.0, 50.0)] {
            let p = Vec2::new(x, y);
            let bf = segs.iter().map(|&(a, b)| dist_point_seg(p, a, b)).fold(f64::INFINITY, f64::min);
            assert!((grid.distance(p, 1e9) - bf).abs() < 1e-9, "at {x},{y}");
        }
    }
}
