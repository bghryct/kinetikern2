//! The slant a design leans by, and when to measure along it. Italics,
//! obliques and scripts lean, and the model measures the white between
//! letters as if they were upright, so a slanted design looks crammed to it
//! even where its letters never touch. A font that declares an italic angle
//! is measured along it (the callers shear the outlines upright about half
//! the x-height). A font that declares none can still lean: `stem_slant`
//! reads the slant off its stems, and `lean_wins` decides from a check both
//! ways whether to measure along it. Spacing QA and the plugins share both,
//! so the library check and the spacing tool measure a design the same way.

use crate::api::{Vec2, NODE_CURVE, NODE_OFFCURVE, NODE_QCURVE};

/// Glyphs whose first ink run, between a quarter and three quarters of the
/// x-height, is a stem in most designs: `stem_slant` takes their outlines in
/// this order.
pub const STEMS: [&str; 10] = ["l", "i", "h", "n", "m", "u", "r", "k", "b", "p"];

/// Below this a slant is taken as upright (degrees): a declared italic angle
/// and a measured slant alike.
pub const MIN_DEGREES: f64 = 3.0;

/// A design that leans without declaring an italic angle is measured along
/// its slant when that leaves at most this share of the shape error upright.
pub const LEAN_GAIN: f64 = 0.8;

/// The best-fit Looseness is searched within ±FIT_LIMIT
/// (`Engine::fit_looseness`): a fit at the limit means the font is tighter
/// (looser) than anything the model makes, measured that way.
pub const FIT_LIMIT: f64 = 6.0;

/// An outline flattened to polygons (the pen writes a quadratic as one
/// off-curve point and its end, a cubic as two and its end).
pub fn polygons(contours: &[Vec<(Vec2, u32)>]) -> Vec<Vec<(f64, f64)>> {
    const STEPS: usize = 12;
    contours
        .iter()
        .filter(|c| !c.is_empty())
        .map(|c| {
            let mut out: Vec<(f64, f64)> = Vec::new();
            let mut cur = c[0].0;
            out.push((cur.x, cur.y));
            let mut offs: Vec<Vec2> = Vec::new();
            for &(p, kind) in &c[1..] {
                match kind {
                    k if k == NODE_QCURVE && offs.len() == 1 => {
                        let q = offs[0];
                        for s in 1..=STEPS {
                            let t = s as f64 / STEPS as f64;
                            let u = 1.0 - t;
                            out.push((u * u * cur.x + 2.0 * u * t * q.x + t * t * p.x, u * u * cur.y + 2.0 * u * t * q.y + t * t * p.y));
                        }
                        offs.clear();
                    }
                    k if k == NODE_CURVE && offs.len() == 2 => {
                        let (a, b) = (offs[0], offs[1]);
                        for s in 1..=STEPS {
                            let t = s as f64 / STEPS as f64;
                            let u = 1.0 - t;
                            out.push((
                                u * u * u * cur.x + 3.0 * u * u * t * a.x + 3.0 * u * t * t * b.x + t * t * t * p.x,
                                u * u * u * cur.y + 3.0 * u * u * t * a.y + 3.0 * u * t * t * b.y + t * t * t * p.y,
                            ));
                        }
                        offs.clear();
                    }
                    k if k == NODE_QCURVE || k == NODE_CURVE => {
                        out.push((p.x, p.y)); // a form the pen does not write: its end
                        offs.clear();
                    }
                    k if k == NODE_OFFCURVE => {
                        offs.push(p);
                        continue;
                    }
                    _ => out.push((p.x, p.y)),
                }
                cur = p;
            }
            out
        })
        .collect()
}

fn crossings(polys: &[Vec<(f64, f64)>], y: f64) -> Vec<f64> {
    let mut xs = Vec::new();
    for poly in polys {
        for k in 0..poly.len() {
            let (x0, y0) = poly[k];
            let (x1, y1) = poly[(k + 1) % poly.len()];
            if (y0 <= y && y < y1) || (y1 <= y && y < y0) {
                xs.push(x0 + (y - y0) * (x1 - x0) / (y1 - y0));
            }
        }
    }
    xs.sort_by(|a, b| a.total_cmp(b));
    xs
}

/// The top of an outline (its highest point, off-curve points included), the
/// x-height `stem_slant` takes from the x.
pub fn top(contours: &[Vec<(Vec2, u32)>]) -> Option<f64> {
    contours.iter().flatten().map(|(p, _)| p.y).fold(None, |m, y| Some(m.map_or(y, |v: f64| v.max(y))))
}

/// tan of the slant a design's stems show (+ leans right), or 0 for an
/// upright design: the median over the stems of the slope of the first ink
/// run's centre, when at least four near-straight stems roughly agree
/// (interquartile range up to 0.25: a hand-drawn script leans by 11–30° from
/// letter to letter) on at least `MIN_DEGREES`. `stems` holds the outlines
/// of the glyphs named in `STEMS`, in that order (no contours for a glyph
/// the font lacks); `x_height` is the top of the x (`top`).
pub fn stem_slant(stems: &[&[Vec<(Vec2, u32)>]], x_height: f64, units_per_em: f64) -> f64 {
    if !(x_height > 0.0) {
        return 0.0;
    }
    let xh = x_height;
    let mut slopes = Vec::new();
    for contours in stems {
        let polys = polygons(contours);
        let pts: Vec<(f64, f64)> = (0..6)
            .filter_map(|k| {
                let y = xh * (0.25 + 0.1 * k as f64) + 0.37; // off any node
                let xs = crossings(&polys, y);
                (xs.len() >= 2).then(|| (y, 0.5 * (xs[0] + xs[1])))
            })
            .collect();
        if pts.len() < 5 {
            continue;
        }
        // x = c + t·y by least squares; a near-straight stem only (no point
        // more than 2 % of the em off the line: a loop or a bowl is not one)
        let n = pts.len() as f64;
        let my = pts.iter().map(|p| p.0).sum::<f64>() / n;
        let mx = pts.iter().map(|p| p.1).sum::<f64>() / n;
        let syy: f64 = pts.iter().map(|p| (p.0 - my).powi(2)).sum();
        if syy <= 0.0 {
            continue;
        }
        let t = pts.iter().map(|p| (p.0 - my) * (p.1 - mx)).sum::<f64>() / syy;
        let worst = pts.iter().map(|p| (p.1 - (mx + t * (p.0 - my))).abs()).fold(0.0, f64::max);
        if worst < 0.02 * units_per_em {
            slopes.push(t);
        }
    }
    if slopes.len() < 4 {
        return 0.0;
    }
    slopes.sort_by(|a, b| a.total_cmp(b));
    let q = |f: f64| slopes[((slopes.len() - 1) as f64 * f).round() as usize];
    let (median, iqr) = (q(0.5), q(0.75) - q(0.25));
    if iqr > 0.25 || median.atan().to_degrees().abs() < MIN_DEGREES {
        0.0
    } else {
        median
    }
}

/// Whether a design that leans without declaring an italic angle is measured
/// along the slant its stems show, from (shape error, best-fit Looseness) of
/// a check upright and along the slant: where that leaves at most
/// `LEAN_GAIN` of the shape error upright, or where upright the fit stops at
/// the limit of the Looseness range and along the slant it does not. A fit at
/// the limit says the model cannot follow the font measured that way (it is
/// tighter, or looser, than anything the model makes), so its shape error is
/// no measure to compare with: a hand that leans hard can look crammed
/// upright where along its slant it is set tight but in range (Carattere:
/// −6.00 upright, −1.50 along its 22.5°).
pub fn lean_wins(upright: (f64, f64), lean: (f64, f64)) -> bool {
    let at_limit = |t: f64| t.abs() >= FIT_LIMIT - 1e-6;
    lean.0 <= LEAN_GAIN * upright.0 || (at_limit(upright.1) && !at_limit(lean.1))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::NODE_LINE;

    /// The stems of a design leaning by `t`: one bar per stem glyph, 700 high.
    fn bars(t: f64) -> Vec<Vec<Vec<(Vec2, u32)>>> {
        let bar = |x: f64, h: f64| {
            let pts = [(x, 0.0), (x + 80.0, 0.0), (x + 80.0 + h * t, h), (x + h * t, h)];
            vec![pts.iter().map(|&(x, y)| (Vec2::new(x, y), NODE_LINE)).collect::<Vec<_>>()]
        };
        STEMS.iter().map(|_| bar(50.0, 700.0)).collect()
    }

    fn refs(g: &[Vec<Vec<(Vec2, u32)>>]) -> Vec<&[Vec<(Vec2, u32)>]> {
        g.iter().map(|c| &c[..]).collect()
    }

    #[test]
    fn upright_stems_are_upright_and_slanted_ones_are_measured() {
        let up = bars(0.0);
        assert_eq!(stem_slant(&refs(&up), 500.0, 1000.0), 0.0);
        let lean = bars(0.2);
        assert!((stem_slant(&refs(&lean), 500.0, 1000.0) - 0.2).abs() < 1e-6);
        // too few stems, or no x-height: upright
        let few = refs(&lean)[..3].to_vec();
        assert_eq!(stem_slant(&few, 500.0, 1000.0), 0.0);
        assert_eq!(stem_slant(&refs(&lean), 0.0, 1000.0), 0.0);
        // below MIN_DEGREES: upright
        let slight = bars(0.03);
        assert_eq!(stem_slant(&refs(&slight), 500.0, 1000.0), 0.0);
    }

    #[test]
    fn a_lean_is_taken_where_it_fits_clearly_better_or_only_it_stays_in_range() {
        // clearly better along the slant, or not
        assert!(lean_wins((50.0, -0.5), (39.0, -0.4)));
        assert!(!lean_wins((50.0, -0.5), (45.0, -0.4)));
        // upright beyond the model's reach, in range along the slant (Carattere)
        assert!(lean_wins((77.2, -6.0), (90.4, -1.5)));
        // beyond its reach either way (Allison), or only along the slant (Playwrite US Trad)
        assert!(!lean_wins((73.0, -6.0), (85.7, -6.0)));
        assert!(!lean_wins((50.0, -1.8), (60.0, -6.0)));
        // the loose end of the range too
        assert!(lean_wins((40.0, 6.0), (45.0, 2.0)));
    }
}
