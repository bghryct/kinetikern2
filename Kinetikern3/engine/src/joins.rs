//! A connected script's joins, found in the font's own spacing (Spacing QA
//! and the plugin use the same rule). A family is connected when its letters
//! actually join: at least half its lowercase letters overlap at least half
//! of their lowercase partners at some height, as spaced and kerned. Text
//! faces never are (an italic f's hook is not a join), nor are scripts and
//! casual hands whose strokes reach past the advance but stop short of the
//! next letter (checked on 365 families: treating those as joined made a
//! third of them less even). In a connected family, each letter side's join
//! band covers the heights where its ink overlaps at least half of its
//! partners and those where it reaches past its own advance (right) or
//! before its origin (left) — a join stroke, often diagonal, that meets each
//! partner at a different height. The bands are what `GlyphInput::join_left`
//! and `join_right` take.

use crate::geometry::{Outline, Vec2};

/// What a glyph is to the joins: for the detector only letters join, and the partners
/// (`Lower`) are the letters every letter's sides are measured against: the
/// basic lowercase a–z (Spacing QA's, and the plugin's), whatever else the
/// font has — accented and alternate letters are `Upper` here, measured but
/// not partners, which keeps the detection to milliseconds on large fonts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JoinKind {
    Other,
    Upper,
    Lower,
}

pub struct JoinGlyph<'a> {
    pub contours: &'a [Vec<(Vec2, u32)>],
    pub advance: f64,
    pub kind: JoinKind,
}

/// Which signals make a join band (whether the family is connected is
/// decided by the overlaps either way).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JoinRule {
    /// Ink past the advance (or before the origin), or overlaps with most partners.
    Both,
    /// Ink past the advance (or before the origin) only.
    Overhang,
    /// Overlaps with most partners only.
    Overlaps,
}

/// A glyph's join bands, (left, right), font units.
pub type Bands = (Option<(f64, f64)>, Option<(f64, f64)>);

/// Height bands sampled between −0.3 and 1.5 x-heights (exit strokes can
/// rise past the x-height).
const N: usize = 60;
const Y_FROM: f64 = -0.3;
const Y_SPAN: f64 = 1.8;
/// A side joins at a height where at least this share of its pairs overlap.
const SHARE: f64 = 0.5;
/// A family is connected when at least this share of its lowercase letters
/// overlap their partners (on the right).
const CONNECTED: f64 = 0.5;
/// Padding of a join band, in em: the stroke's own edges are left out too.
const PAD: f64 = 0.02;

/// The join bands of every glyph; all None unless the family is connected.
/// `x_height` in font units; `kern(a, b)` the font's kerning of a pair (0 if
/// none; only asked for letter pairs).
pub fn detect(glyphs: &[JoinGlyph], upm: f64, x_height: f64, kern: &dyn Fn(usize, usize) -> f64, rule: JoinRule) -> Vec<Bands> {
    let n = glyphs.len();
    let none = vec![(None, None); n];
    if !(x_height.is_finite() && x_height > 0.0 && upm > 0.0) {
        return none;
    }
    let ys: Vec<f64> = (0..N).map(|k| x_height * (Y_FROM + Y_SPAN * (k as f64 + 0.5) / N as f64) + 0.37).collect();
    // each letter's leftmost and rightmost ink at every height
    let profiles: Vec<Option<(Vec<f64>, Vec<f64>)>> = glyphs
        .iter()
        .map(|g| {
            if g.kind == JoinKind::Other || g.contours.is_empty() {
                return None;
            }
            let outline = Outline::from_contours(g.contours, 0.002 * upm);
            let (mut l, mut r) = (vec![f64::NAN; N], vec![f64::NAN; N]);
            for (k, &y) in ys.iter().enumerate() {
                if let Some(hit) = outline.scan(y) {
                    l[k] = hit.xmin;
                    r[k] = hit.xmax;
                }
            }
            Some((l, r))
        })
        .collect();
    let partners: Vec<usize> = (0..n).filter(|&i| glyphs[i].kind == JoinKind::Lower && profiles[i].is_some()).collect();
    if partners.len() < 10 {
        return none;
    }
    // overlaps by height: every letter's right side against each partner
    // after it, and its left side against each partner before it
    let mut right_hits = vec![[0u32; N]; n];
    let mut left_hits = vec![[0u32; N]; n];
    let mut right_pairs = vec![0u32; n];
    let mut left_pairs = vec![0u32; n];
    let count = |a: usize, b: usize, hits: &mut [u32; N]| {
        let ((_, ra), (lb, _)) = (profiles[a].as_ref().unwrap(), profiles[b].as_ref().unwrap());
        let shift = glyphs[a].advance + kern(a, b);
        for k in 0..N {
            if ra[k].is_finite() && lb[k].is_finite() && shift + lb[k] - ra[k] < 0.0 {
                hits[k] += 1;
            }
        }
    };
    for i in (0..n).filter(|&i| profiles[i].is_some()) {
        for &p in &partners {
            count(i, p, &mut right_hits[i]);
            count(p, i, &mut left_hits[i]);
        }
        right_pairs[i] = partners.len() as u32;
        left_pairs[i] = partners.len() as u32;
    }
    let overlaps = |hits: &[u32; N], pairs: u32, k: usize| pairs > 0 && hits[k] as f64 >= SHARE * pairs as f64;
    // connected: most lowercase letters overlap most of their partners somewhere
    let overlapping = partners.iter().filter(|&&i| (0..N).any(|k| overlaps(&right_hits[i], right_pairs[i], k))).count();
    if (overlapping as f64) < CONNECTED * partners.len() as f64 {
        return none;
    }
    let step = Y_SPAN * x_height / N as f64;
    let pad = PAD * upm;
    let band = |i: usize, right: bool| -> Option<(f64, f64)> {
        let (l, r) = profiles[i].as_ref()?;
        let (hits, pairs) = if right { (&right_hits[i], right_pairs[i]) } else { (&left_hits[i], left_pairs[i]) };
        let adv = glyphs[i].advance;
        let on: Vec<usize> = (0..N)
            .filter(|&k| {
                let overlap = overlaps(hits, pairs, k);
                let reaches = if right { r[k].is_finite() && r[k] > adv } else { l[k].is_finite() && l[k] < 0.0 };
                match rule {
                    JoinRule::Both => overlap || reaches,
                    JoinRule::Overhang => reaches,
                    JoinRule::Overlaps => overlap,
                }
            })
            .collect();
        let (lo, hi) = (*on.first()?, *on.last()?);
        Some((ys[lo] - 0.5 * step - pad, ys[hi] + 0.5 * step + pad))
    };
    (0..n).map(|i| (band(i, false), band(i, true))).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geometry::{Vec2, NODE_LINE};

    fn rect(x0: f64, y0: f64, x1: f64, y1: f64) -> Vec<(Vec2, u32)> {
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)].iter().map(|&(x, y)| (Vec2::new(x, y), NODE_LINE)).collect()
    }

    #[test]
    fn a_script_joins_and_a_text_face_does_not() {
        // twelve lowercase letters: bodies, with or without strokes past the advance
        let script: Vec<Vec<Vec<(Vec2, u32)>>> = (0..12)
            .map(|_| vec![rect(-20.0, 0.0, 100.0, 40.0), rect(100.0, 0.0, 400.0, 500.0), rect(400.0, 0.0, 520.0, 40.0)])
            .collect();
        let text: Vec<Vec<Vec<(Vec2, u32)>>> = (0..12).map(|_| vec![rect(60.0, 0.0, 440.0, 500.0)]).collect();
        fn glyphs(c: &[Vec<Vec<(Vec2, u32)>>]) -> Vec<JoinGlyph<'_>> {
            c.iter().map(|c| JoinGlyph { contours: c, advance: 500.0, kind: JoinKind::Lower }).collect()
        }
        let zero = |_: usize, _: usize| 0.0;
        // strokes past the advance that stop short of the next letter (its
        // body starts 80 units in): spaced, not joined
        let spaced: Vec<Vec<Vec<(Vec2, u32)>>> =
            (0..12).map(|_| vec![rect(80.0, 0.0, 400.0, 500.0), rect(400.0, 0.0, 520.0, 40.0)]).collect();
        for rule in [JoinRule::Both, JoinRule::Overhang, JoinRule::Overlaps] {
            assert!(detect(&glyphs(&spaced), 1000.0, 500.0, &zero, rule).iter().all(|b| *b == (None, None)), "{rule:?}");
        }
        for rule in [JoinRule::Both, JoinRule::Overhang, JoinRule::Overlaps] {
            let s = detect(&glyphs(&script), 1000.0, 500.0, &zero, rule);
            let (l, r) = s[0];
            let (l, r) = (l.unwrap(), r.unwrap());
            // the bands cover the strokes (0–40), padded, and not the body above
            assert!(l.0 <= 0.0 && l.1 >= 40.0 && l.1 < 100.0, "{rule:?} {l:?}");
            assert!(r.0 <= 0.0 && r.1 >= 40.0 && r.1 < 100.0, "{rule:?} {r:?}");
            assert!(detect(&glyphs(&text), 1000.0, 500.0, &zero, rule).iter().all(|b| *b == (None, None)));
        }
    }
}
