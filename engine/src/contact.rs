//! Ink contact between two glyphs set side by side: the join checker's
//! geometry (Spacing QA and the plugin use the same).
//!
//! A glyph's ink is kept as nonzero-rule intervals on scanlines one unit per
//! 1000 em apart, at the same absolute heights for every glyph, so two glyphs
//! compare row by row. Moving the second glyph right by `dx`, its interval
//! [b0, b1] meets [a0, a1] of the first exactly when dx lies in
//! [a0 − b1, a1 − b0]. The union of these ranges over all rows is the set of
//! offsets at which the two inks touch, read off in one pass:
//!
//! * a pair **joins** when its offset lies in that set;
//! * its **window** is the piece of the set around the offset: how far the
//!   second letter may move left (close) or right (open) before the inks part;
//! * a pair that does not join has a **gap**: how far the second letter must
//!   move left to touch;
//! * the **contact height** is the mean height of the overlap;
//! * a **crossing** is enclosed white that neither glyph has alone (strokes
//!   that cross), or a counter of either glyph that the other one changes (a
//!   body pushed into a bowl).
//!
//! Ink on neighbouring scanlines touches too, so strokes that meet flush
//! along a horizontal cut join; within a unit per 1000 em either way is
//! touching (callers pass one scanline spacing, `InkRows::step`), so a
//! window's ends are a unit generous at most.

use crate::geometry::{Outline, Vec2};

/// A glyph's ink on the scanlines `(k + ½)·step` for k = k0, k0 + 1, …
#[derive(Clone, Debug, Default)]
pub struct InkRows {
    pub step: f64,
    pub k0: i64,
    /// Row r's intervals are `spans[starts[r]..starts[r + 1]]`, sorted, disjoint.
    starts: Vec<u32>,
    spans: Vec<(f32, f32)>,
    /// Extreme ink of each row (+∞ / −∞ for a row without ink).
    lo: Vec<f32>,
    hi: Vec<f32>,
}

/// The scanline spacing for a font: one unit per 1000 em.
pub fn row_step(upm: f64) -> f64 {
    if upm.is_finite() && upm > 0.0 {
        upm / 1000.0
    } else {
        1.0
    }
}

impl InkRows {
    /// The ink of contours in the engine's node format, moved by (dx, dy).
    pub fn of_contours(contours: &[Vec<(Vec2, u32)>], step: f64, dx: f64, dy: f64) -> InkRows {
        let outline = Outline::from_contours(contours, 0.05 * step);
        let mut cross: Vec<(i64, f64, i32)> = Vec::new();
        for e in &outline.edges {
            let (a, b) = (Vec2::new(e.a.x + dx, e.a.y + dy), Vec2::new(e.b.x + dx, e.b.y + dy));
            if a.y == b.y || !(a.is_finite() && b.is_finite()) {
                continue;
            }
            let up = a.y < b.y;
            let (ylo, yhi) = if up { (a.y, b.y) } else { (b.y, a.y) };
            // rows with ylo ≤ y < yhi (the half-open rule of Outline::ink_intervals)
            let k_lo = (ylo / step - 0.5).ceil() as i64;
            let k_hi = (yhi / step - 0.5).ceil() as i64;
            for k in k_lo..k_hi {
                let y = (k as f64 + 0.5) * step;
                let x = a.x + (y - a.y) * (b.x - a.x) / (b.y - a.y);
                cross.push((k, x, if up { 1 } else { -1 }));
            }
        }
        Self::from_crossings(cross, step)
    }

    fn from_crossings(mut cross: Vec<(i64, f64, i32)>, step: f64) -> InkRows {
        if cross.is_empty() {
            return InkRows { step, ..InkRows::default() };
        }
        cross.sort_by(|p, q| p.0.cmp(&q.0).then(p.1.total_cmp(&q.1)));
        let k0 = cross[0].0;
        let k1 = cross[cross.len() - 1].0;
        let rows = (k1 - k0 + 1) as usize;
        let mut out = InkRows { step, k0, starts: Vec::with_capacity(rows + 1), ..InkRows::default() };
        let mut i = 0;
        for k in k0..=k1 {
            out.starts.push(out.spans.len() as u32);
            let mut wn = 0;
            let mut start = 0.0;
            while i < cross.len() && cross[i].0 == k {
                let (_, x, d) = cross[i];
                let was = wn != 0;
                wn += d;
                if !was && wn != 0 {
                    start = x;
                } else if was && wn == 0 {
                    push_span(&mut out.spans, out.starts[out.starts.len() - 1] as usize, start, x);
                }
                i += 1;
            }
        }
        out.starts.push(out.spans.len() as u32);
        out.finish_extremes();
        out
    }

    fn finish_extremes(&mut self) {
        let rows = self.rows();
        self.lo = vec![f32::INFINITY; rows];
        self.hi = vec![f32::NEG_INFINITY; rows];
        for r in 0..rows {
            let (a, b) = (self.starts[r] as usize, self.starts[r + 1] as usize);
            if b > a {
                self.lo[r] = self.spans[a].0;
                self.hi[r] = self.spans[b - 1].1;
            }
        }
    }

    /// Several glyphs as one ink (each one's nonzero rule on its own, then
    /// their union): a letter and the marks or connectors set with it.
    pub fn union(parts: &[InkRows]) -> InkRows {
        let parts: Vec<&InkRows> = parts.iter().filter(|p| !p.is_empty()).collect();
        let Some(first) = parts.first() else {
            return InkRows::default();
        };
        let step = first.step;
        let k0 = parts.iter().map(|p| p.k0).min().unwrap_or(0);
        let k1 = parts.iter().map(|p| p.k0 + p.rows() as i64).max().unwrap_or(k0);
        let mut out = InkRows { step, k0, ..InkRows::default() };
        let mut row: Vec<(f32, f32)> = Vec::new();
        for k in k0..k1 {
            out.starts.push(out.spans.len() as u32);
            row.clear();
            for p in &parts {
                row.extend_from_slice(p.at(k));
            }
            row.sort_by(|a, b| a.0.total_cmp(&b.0));
            let from = out.spans.len();
            for &(a, b) in &row {
                push_span(&mut out.spans, from, a as f64, b as f64);
            }
        }
        out.starts.push(out.spans.len() as u32);
        out.finish_extremes();
        out
    }

    pub fn is_empty(&self) -> bool {
        self.spans.is_empty()
    }

    pub fn rows(&self) -> usize {
        self.starts.len().saturating_sub(1)
    }

    fn row(&self, r: usize) -> &[(f32, f32)] {
        &self.spans[self.starts[r] as usize..self.starts[r + 1] as usize]
    }

    /// Intervals on absolute row k (empty outside the glyph).
    pub fn at(&self, k: i64) -> &[(f32, f32)] {
        let r = k - self.k0;
        if r < 0 || r as usize >= self.rows() {
            &[]
        } else {
            self.row(r as usize)
        }
    }

    /// Height of absolute row k.
    pub fn y(&self, k: i64) -> f64 {
        (k as f64 + 0.5) * self.step
    }

    /// One past the last absolute row.
    pub fn k_end(&self) -> i64 {
        self.k0 + self.rows() as i64
    }

    /// Leftmost and rightmost ink over all rows ((∞, −∞) without ink).
    pub fn x_range(&self) -> (f64, f64) {
        let lo = self.lo.iter().copied().fold(f32::INFINITY, f32::min);
        let hi = self.hi.iter().copied().fold(f32::NEG_INFINITY, f32::max);
        (lo as f64, hi as f64)
    }

    /// The same ink moved right by `dx` (whole rows keep their heights).
    pub fn shifted(&self, dx: f64) -> InkRows {
        let d = dx as f32;
        let mut out = self.clone();
        for s in out.spans.iter_mut() {
            s.0 += d;
            s.1 += d;
        }
        for v in out.lo.iter_mut().chain(out.hi.iter_mut()) {
            *v += d;
        }
        out
    }

    /// The absolute rows both glyphs have.
    fn common(&self, b: &InkRows) -> std::ops::Range<i64> {
        let lo = self.k0.max(b.k0);
        let hi = (self.k0 + self.rows() as i64).min(b.k0 + b.rows() as i64);
        lo..hi.max(lo)
    }

    /// Pairs of rows (k of self, k + d of b, d in −1..=1) where the two may
    /// touch: ink on neighbouring scanlines touches too, so strokes that meet
    /// flush along a horizontal cut (which no scanline lands on) join.
    fn facing(&self, b: &InkRows) -> impl Iterator<Item = (i64, i64)> + '_ {
        let (lo, hi) = (self.k0.max(b.k0 - 1), self.k_end().min(b.k_end() + 1));
        let (b0, b1) = (b.k0, b.k_end());
        (lo..hi.max(lo)).flat_map(move |k| (-1..=1).map(move |d| (k, k + d)).filter(move |&(_, kb)| kb >= b0 && kb < b1))
    }

    fn extremes(&self, k: i64) -> (f64, f64) {
        let r = (k - self.k0) as usize;
        (self.lo[r] as f64, self.hi[r] as f64)
    }

    /// Areas of the enclosed white shapes (counters), font units².
    pub fn holes(&self) -> Vec<f64> {
        hole_areas(self.k0, self.k0 + self.rows() as i64, self.step, |k, out| out.extend_from_slice(self.at(k)))
    }
}

/// Appends [a, b] to the sorted spans of the current row (from `from`),
/// merging it with the last one when they touch.
fn push_span(spans: &mut Vec<(f32, f32)>, from: usize, a: f64, b: f64) {
    let (a, b) = (a as f32, b as f32);
    if spans.len() > from {
        let last = spans.last_mut().unwrap();
        if a <= last.1 {
            last.1 = last.1.max(b);
            return;
        }
    }
    spans.push((a, b));
}

/// True when the ink of `a` and that of `b` moved right by `dx` touch (come
/// within `tol` of each other on a scanline or across neighbouring ones).
pub fn touches(a: &InkRows, b: &InkRows, dx: f64, tol: f64) -> bool {
    for (k, kb) in a.facing(b) {
        let (alo, ahi) = a.extremes(k);
        let (blo, bhi) = b.extremes(kb);
        if blo + dx > ahi + tol || bhi + dx + tol < alo {
            continue;
        }
        let (ra, rb) = (a.at(k), b.at(kb));
        let (mut i, mut j) = (0, 0);
        while i < ra.len() && j < rb.len() {
            let (a0, a1) = (ra[i].0 as f64, ra[i].1 as f64);
            let (b0, b1) = (rb[j].0 as f64 + dx, rb[j].1 as f64 + dx);
            if a1 + tol < b0 {
                i += 1;
            } else if b1 + tol < a0 {
                j += 1;
            } else {
                return true;
            }
        }
    }
    false
}

/// The lowest and highest heights at which the two inks touch at offset dx
/// (within `tol`; None: they do not).
pub fn touch_heights(a: &InkRows, b: &InkRows, dx: f64, tol: f64) -> Option<(f64, f64)> {
    let mut out: Option<(f64, f64)> = None;
    for (k, kb) in a.facing(b) {
        let (alo, ahi) = a.extremes(k);
        let (blo, bhi) = b.extremes(kb);
        if blo + dx > ahi + tol || bhi + dx + tol < alo {
            continue;
        }
        let (ra, rb) = (a.at(k), b.at(kb));
        let (mut i, mut j) = (0, 0);
        while i < ra.len() && j < rb.len() {
            let (a0, a1) = (ra[i].0 as f64, ra[i].1 as f64);
            let (b0, b1) = (rb[j].0 as f64 + dx, rb[j].1 as f64 + dx);
            if a1 + tol < b0 {
                i += 1;
            } else if b1 + tol < a0 {
                j += 1;
            } else {
                let y = a.y(k);
                out = Some(out.map_or((y, y), |(lo, hi)| (lo.min(y), hi.max(y))));
                break;
            }
        }
    }
    out
}

/// Every offset at which the two inks touch: sorted, disjoint closed ranges
/// (`flush`: also across neighbouring scanlines, see `touches`).
pub fn touch_set_rows(a: &InkRows, b: &InkRows, flush: bool) -> Vec<(f64, f64)> {
    let mut ranges: Vec<(f64, f64)> = Vec::new();
    for (k, kb) in a.facing(b) {
        if !flush && k != kb {
            continue;
        }
        let (ra, rb) = (a.at(k), b.at(kb));
        for &(a0, a1) in ra {
            for &(b0, b1) in rb {
                ranges.push((a0 as f64 - b1 as f64, a1 as f64 - b0 as f64));
            }
        }
    }
    ranges.sort_by(|p, q| p.0.total_cmp(&q.0));
    let mut out: Vec<(f64, f64)> = Vec::with_capacity(16);
    for (lo, hi) in ranges {
        match out.last_mut() {
            Some(last) if lo <= last.1 => last.1 = last.1.max(hi),
            _ => out.push((lo, hi)),
        }
    }
    out
}

/// The pair's touch set, flush contacts included: the offsets at which the
/// two inks touch on the same scanlines or on neighbouring ones (strokes that
/// meet along a horizontal cut). `contact_both` takes windows from the
/// same-scanline set where there is contact there.
pub fn touch_set(a: &InkRows, b: &InkRows) -> Vec<(f64, f64)> {
    touch_set_rows(a, b, true)
}

/// How a pair of glyphs meets at one offset.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Contact {
    pub joins: bool,
    /// Joined: how far the second glyph may move left (close) and right
    /// (open) before the inks part. Not joined: NaN.
    pub close: f64,
    pub open: f64,
    /// Not joined: how far the second glyph must move left to touch (∞ if it
    /// never does). Joined: 0.
    pub gap: f64,
    /// Not joined: the offset change that would join it (negative: closer),
    /// a few units into the nearest contact so the touch is not a hairline
    /// (NaN if it never touches). Joined: 0.
    pub fix: f64,
}

/// The pair's contact at offset `dx` (the second glyph moved right by dx
/// from where `b` was built). `fragile` (font units): room a fix leaves
/// on the opening side when the contact allows it; `tol`: how close counts
/// as touching (one scanline spacing: strokes that meet at a tangent touch
/// between two scanlines, and a gap under a unit is invisible). The room to
/// close and open comes from contact on the same scanlines (exact) where
/// there is some at `dx`, else from the flush contact across neighbouring
/// ones.
pub fn contact(a: &InkRows, b: &InkRows, dx: f64, fragile: f64, tol: f64) -> Contact {
    contact_both(&touch_set_rows(a, b, false), &touch_set_rows(a, b, true), dx, fragile, tol)
}

/// `contact` from the two touch sets (same scanlines, and with neighbours).
pub fn contact_both(same: &[(f64, f64)], flush: &[(f64, f64)], dx: f64, fragile: f64, tol: f64) -> Contact {
    let c = contact_in(same, dx, fragile, tol);
    if c.joins {
        return c;
    }
    let f = contact_in(flush, dx, fragile, tol);
    if f.joins {
        f
    } else {
        c
    }
}

/// `contact` from a precomputed `touch_set`. Within `tol` of a contact the
/// pair joins with no room on that side.
pub fn contact_in(set: &[(f64, f64)], dx: f64, fragile: f64, tol: f64) -> Contact {
    if let Some(&(lo, hi)) = set.iter().find(|&&(lo, hi)| lo - tol <= dx && dx <= hi + tol) {
        return Contact { joins: true, close: (dx - lo).max(0.0), open: (hi - dx).max(0.0), gap: 0.0, fix: 0.0 };
    }
    // the nearest contact on the closing side (the second glyph moves left)
    match set.iter().rev().find(|&&(_, hi)| hi < dx) {
        Some(&(lo, hi)) => {
            let target = hi - fragile.min(0.5 * (hi - lo)).max(0.0);
            Contact { joins: false, close: f64::NAN, open: f64::NAN, gap: dx - hi, fix: target - dx }
        }
        None => Contact { joins: false, close: f64::NAN, open: f64::NAN, gap: f64::INFINITY, fix: f64::NAN },
    }
}

/// Mean height of the overlap of the two inks at offset dx (each row
/// weighted by its overlap); NaN where they do not overlap.
pub fn contact_height(a: &InkRows, b: &InkRows, dx: f64) -> f64 {
    let (mut sum, mut w, mut touch_y, mut touch_n) = (0.0, 0.0, 0.0, 0usize);
    for k in a.common(b) {
        let (ra, rb) = (a.at(k), b.at(k));
        let mut row = 0.0;
        let mut touched = false;
        for &(a0, a1) in ra {
            for &(b0, b1) in rb {
                let o = (a1 as f64).min(b1 as f64 + dx) - (a0 as f64).max(b0 as f64 + dx);
                if o >= 0.0 {
                    touched = true;
                    row += o;
                }
            }
        }
        if touched {
            touch_y += a.y(k);
            touch_n += 1;
        }
        sum += row * a.y(k);
        w += row;
    }
    if w > 0.0 {
        sum / w
    } else if touch_n > 0 {
        touch_y / touch_n as f64
    } else {
        // met only across neighbouring scanlines (a flush cut)
        touch_heights(a, b, dx, 0.0).map_or(f64::NAN, |(lo, hi)| 0.5 * (lo + hi))
    }
}

/// White shapes the pair encloses at offset dx that neither glyph has alone
/// (`own`: both glyphs' `holes()`), and counters of either that the other
/// changes by more than 1 %. Returns their areas (font units²); slivers
/// under `min_area` are left out. Empty: no crossing.
pub fn crossings(a: &InkRows, b: &InkRows, dx: f64, own: &[f64], min_area: f64) -> Vec<f64> {
    let k0 = a.k0.min(b.k0);
    let k1 = (a.k0 + a.rows() as i64).max(b.k0 + b.rows() as i64);
    let dxf = dx as f32;
    let mut row: Vec<(f32, f32)> = Vec::new();
    let holes = hole_areas(k0, k1, a.step, |k, out| {
        row.clear();
        row.extend_from_slice(a.at(k));
        row.extend(b.at(k).iter().map(|&(p, q)| (p + dxf, q + dxf)));
        row.sort_by(|p, q| p.0.total_cmp(&q.0));
        let from = out.len();
        for &(p, q) in &row {
            if out.len() > from && p <= out[out.len() - 1].1 {
                let last = out.len() - 1;
                out[last].1 = out[last].1.max(q);
            } else {
                out.push((p, q));
            }
        }
    });
    // a counter that changes by 1 % or less (and a cell) is the same
    // counter: a stroke that only grazes it
    let cell = a.step * a.step;
    let mut left: Vec<f64> = own.to_vec();
    let mut extra = Vec::new();
    for h in holes {
        if let Some(i) = left.iter().position(|&o| (o - h).abs() <= 0.01 * o.max(h) + cell) {
            left.swap_remove(i);
        } else if h >= min_area {
            extra.push(h);
        }
    }
    // a counter the other glyph fills or reshapes
    extra.extend(left.into_iter().filter(|&o| o >= min_area));
    extra
}

/// Enclosed white of the ink `row(k)` gives on rows k0..k1: the white
/// stretches between ink on each row, joined to those they overlap on the
/// rows above and below; a group that reaches the outside is not enclosed.
fn hole_areas(k0: i64, k1: i64, step: f64, mut row: impl FnMut(i64, &mut Vec<(f32, f32)>)) -> Vec<f64> {
    // white stretches per row: (row, x0, x1)
    let mut white: Vec<(f32, f32)> = Vec::new();
    let mut starts: Vec<usize> = Vec::new();
    // whether the row's outside white reaches (its ink extremes)
    let mut ink: Vec<(f32, f32)> = Vec::new();
    let mut extremes: Vec<Option<(f32, f32)>> = Vec::new();
    for k in k0..k1 {
        ink.clear();
        row(k, &mut ink);
        starts.push(white.len());
        extremes.push(ink.first().map(|f| (f.0, ink[ink.len() - 1].1)));
        for w in ink.windows(2) {
            if w[1].0 > w[0].1 {
                white.push((w[0].1, w[1].0));
            }
        }
    }
    starts.push(white.len());
    let n = white.len();
    if n == 0 {
        return Vec::new();
    }
    let mut parent: Vec<usize> = (0..n).collect();
    let mut outside = vec![false; n];
    fn find(p: &mut [usize], mut i: usize) -> usize {
        while p[i] != i {
            p[i] = p[p[i]];
            i = p[i];
        }
        i
    }
    let rows = (k1 - k0) as usize;
    for r in 0..rows {
        let cur = starts[r]..starts[r + 1];
        // the first and last rows border the outside
        if r == 0 || r + 1 == rows {
            for i in cur.clone() {
                outside[i] = true;
            }
        }
        for (nb, near) in [(r.wrapping_sub(1), r > 0), (r + 1, r + 1 < rows)] {
            if !near {
                continue;
            }
            match extremes[nb] {
                // a row without ink is all outside
                None => {
                    for i in cur.clone() {
                        outside[i] = true;
                    }
                }
                Some((lo, hi)) => {
                    for i in cur.clone() {
                        if white[i].0 < lo || white[i].1 > hi {
                            outside[i] = true;
                        }
                    }
                    if nb > r {
                        // overlaps with the next row's stretches (sweep)
                        let next = starts[nb]..starts[nb + 1];
                        let (mut i, mut j) = (cur.start, next.start);
                        while i < cur.end && j < next.end {
                            let (a, b) = (white[i], white[j]);
                            if a.0.max(b.0) < a.1.min(b.1) {
                                let (ri, rj) = (find(&mut parent, i), find(&mut parent, j));
                                if ri != rj {
                                    parent[ri] = rj;
                                }
                            }
                            if a.1 < b.1 {
                                i += 1;
                            } else {
                                j += 1;
                            }
                        }
                    }
                }
            }
        }
    }
    let mut area = vec![0.0f64; n];
    let mut out_root = vec![false; n];
    for i in 0..n {
        let r = find(&mut parent, i);
        area[r] += (white[i].1 - white[i].0) as f64 * step;
        out_root[r] |= outside[i];
    }
    (0..n).filter(|&i| parent[i] == i && !out_root[i] && area[i] > 0.0).map(|i| area[i]).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geometry::NODE_LINE;

    fn rect(x0: f64, y0: f64, x1: f64, y1: f64) -> Vec<(Vec2, u32)> {
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)].iter().map(|&(x, y)| (Vec2::new(x, y), NODE_LINE)).collect()
    }

    fn ink(c: &[Vec<(Vec2, u32)>]) -> InkRows {
        InkRows::of_contours(c, 1.0, 0.0, 0.0)
    }

    #[test]
    fn two_boxes_touch_over_their_shared_heights() {
        let a = ink(&[rect(0.0, 0.0, 100.0, 100.0)]);
        let b = ink(&[rect(0.0, 50.0, 50.0, 150.0)]);
        let set = touch_set(&a, &b);
        assert_eq!(set.len(), 1);
        assert!((set[0].0 + 50.0).abs() < 1e-4 && (set[0].1 - 100.0).abs() < 1e-4, "{set:?}");
        for dx in [-50.0, -10.0, 0.0, 99.0, 100.0] {
            assert!(touches(&a, &b, dx, 0.0), "{dx}");
        }
        for dx in [-50.5, 100.5, 300.0] {
            assert!(!touches(&a, &b, dx, 0.0), "{dx}");
        }
        let c = contact(&a, &b, 90.0, 5.0, 0.0);
        assert!(c.joins && (c.open - 10.0).abs() < 1e-4 && (c.close - 140.0).abs() < 1e-4, "{c:?}");
        // apart by 20: the nearest touch is 20 to the left; the fix goes 5 in
        let c = contact(&a, &b, 120.0, 5.0, 0.0);
        assert!(!c.joins && (c.gap - 20.0).abs() < 1e-4 && (c.fix + 25.0).abs() < 1e-4, "{c:?}");
        // the overlap lies between heights 50 and 100
        assert!((contact_height(&a, &b, 90.0) - 75.0).abs() < 0.6, "{}", contact_height(&a, &b, 90.0));
        // b's top half never meets a: no touch above 100
        let high = ink(&[rect(0.0, 120.0, 50.0, 150.0)]);
        assert!(touch_set(&a, &high).is_empty());
        assert!(contact(&a, &high, 10.0, 5.0, 0.0).gap.is_infinite());
        // half a unit apart counts as touching within a unit
        assert!(!touches(&a, &b, 100.5, 0.0) && touches(&a, &b, 100.5, 1.0));
        let c = contact(&a, &b, 100.5, 5.0, 1.0);
        assert!(c.joins && c.open == 0.0, "{c:?}");
    }

    #[test]
    fn a_window_ends_where_a_diagonal_entry_leaves_the_exit() {
        // a: an exit stroke along the baseline to x = 120; b: a diagonal entry
        // hairline rising from (−30, 0) to (30, 300), 10 wide
        let a = ink(&[rect(0.0, 0.0, 120.0, 30.0)]);
        let diag = vec![
            (Vec2::new(-30.0, 0.0), NODE_LINE),
            (Vec2::new(-20.0, 0.0), NODE_LINE),
            (Vec2::new(40.0, 300.0), NODE_LINE),
            (Vec2::new(30.0, 300.0), NODE_LINE),
        ];
        let b = ink(&[diag]);
        // brute force: the inks touch at dx when b's hairline at some y in
        // [0, 30] lies over [0, 120]
        let brute = |dx: f64| (0..300).any(|i| {
            let y = i as f64 * 0.1;
            let x0 = -30.0 + y * 0.2 + dx;
            x0 <= 120.0 && x0 + 10.0 >= 0.0
        });
        let set = touch_set(&a, &b);
        for i in -600..=600 {
            let dx = i as f64 * 0.5;
            // rows sit at half units: allow the row spacing at the ends
            let near_end = set.iter().any(|&(lo, hi)| (dx - lo).abs() < 0.6 || (dx - hi).abs() < 0.6);
            if !near_end {
                assert_eq!(touches(&a, &b, dx, 0.0), brute(dx), "{dx}");
            }
        }
    }

    #[test]
    fn strokes_that_meet_flush_along_a_cut_join() {
        // a stroke below y = 169 and one above it, overlapping in x
        let a = ink(&[rect(0.0, 150.0, 100.0, 169.0)]);
        let b = ink(&[rect(0.0, 169.0, 50.0, 200.0)]);
        assert!(touches(&a, &b, 60.0, 0.0));
        assert!(contact(&a, &b, 60.0, 5.0, 0.0).joins);
        assert!((contact_height(&a, &b, 60.0) - 169.0).abs() < 1.0);
        // a unit and a half apart they do not
        let c = ink(&[rect(0.0, 170.5, 50.0, 200.0)]);
        assert!(!touches(&a, &c, 60.0, 0.0));
    }

    #[test]
    fn crossing_strokes_enclose_white() {
        // two hooks that together close a ring: neither has a hole alone
        let a = ink(&[rect(0.0, 0.0, 100.0, 20.0), rect(0.0, 0.0, 20.0, 100.0), rect(0.0, 80.0, 100.0, 100.0)]);
        let b = ink(&[rect(0.0, 0.0, 20.0, 100.0)]);
        let own: Vec<f64> = a.holes().into_iter().chain(b.holes()).collect();
        assert!(own.is_empty(), "{own:?}");
        // b set at 80 closes the ring: a 60 × 60 white square
        let x = crossings(&a, &b, 80.0, &own, 4.0);
        assert_eq!(x.len(), 1, "{x:?}");
        assert!((x[0] - 3600.0).abs() < 130.0, "{x:?}");
        // touching the arms' ends still closes it (touching counts as joined)
        assert_eq!(crossings(&a, &b, 100.0, &own, 4.0).len(), 1);
        // a unit further out the white escapes between the arms and b
        assert!(crossings(&a, &b, 101.0, &own, 4.0).is_empty());
    }

    #[test]
    fn a_body_pushed_into_a_counter_changes_it() {
        // an o (a square ring) and a box that enters its counter from the right
        let o = ink(&[
            rect(0.0, 0.0, 200.0, 200.0),
            // the counter, drawn the other way round
            [(50.0, 50.0), (50.0, 150.0), (150.0, 150.0), (150.0, 50.0)]
                .iter()
                .map(|&(x, y)| (Vec2::new(x, y), NODE_LINE))
                .collect(),
        ]);
        let holes = o.holes();
        assert_eq!(holes.len(), 1);
        assert!((holes[0] - 10000.0).abs() < 1.0, "{holes:?}");
        let b = ink(&[rect(0.0, 80.0, 60.0, 120.0)]);
        let own: Vec<f64> = holes.iter().copied().chain(b.holes()).collect();
        // b next to the o: nothing changes
        assert!(crossings(&o, &b, 200.0, &own, 4.0).is_empty());
        // b pushed 40 units into the counter (through the right stem)
        let x = crossings(&o, &b, 110.0, &own, 4.0);
        assert!(!x.is_empty(), "{x:?}");
    }

    #[test]
    fn a_union_counts_ink_once() {
        let a = ink(&[rect(0.0, 0.0, 100.0, 50.0)]);
        let b = InkRows::of_contours(&[rect(0.0, 0.0, 50.0, 50.0)], 1.0, 80.0, 20.0);
        let u = InkRows::union(&[a.clone(), b]);
        // row at y = 30.5: one merged interval 0..130
        let k = 30;
        assert_eq!(u.at(k), &[(0.0f32, 130.0f32)]);
        // y = 60.5: b only
        assert_eq!(u.at(60), &[(80.0f32, 130.0f32)]);
        assert!(u.holes().is_empty());
    }
}
