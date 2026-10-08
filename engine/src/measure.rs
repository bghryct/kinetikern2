//! The font's spacing as it is, measured against a solve: for every pair, the
//! visible gap now (current sidebearings plus current kerning) and the
//! model's, and their difference once the font's overall tightness is taken
//! out. The extremes are the pairs the font sets loosest and tightest
//! relative to its own rhythm — the pairs panel of the plugin and the Spacing
//! QA reports.

use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};

use rayon::prelude::*;

use crate::engine::{Context, GLYPH_RTL, NONE};
use crate::run::{KIND_CLASS_CLASS, KIND_CLASS_GLYPH, KIND_GLYPH_CLASS, KIND_GLYPH_GLYPH};

/// One entry of the font's current kerning; class sides are the caller's
/// group ids (`GlyphInput::right_group` on the left, `left_group` on the right).
#[derive(Clone, Copy, Debug)]
pub struct KernIn {
    pub kind: u8,
    pub left: u32,
    pub right: u32,
    pub value: f64,
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct PairOut {
    pub left: u32,
    pub right: u32,
    /// Visible gap now (bbox to bbox, font units).
    pub current: f64,
    /// The model's gap for the pair.
    pub model: f64,
    /// current − model − offset: positive = looser than the font's own rhythm.
    pub residual: f64,
}

#[derive(Clone, Debug, Default)]
pub struct Measured {
    pub pairs: u64,
    /// Mean of current − model (the overall tightness difference).
    pub offset: f64,
    pub mae: f64,
    pub rms: f64,
    /// Most positive residuals first.
    pub loosest: Vec<PairOut>,
    /// Most negative residuals first.
    pub tightest: Vec<PairOut>,
}

/// The font's current kerning with the usual precedence (glyph–glyph,
/// glyph–class, class–glyph, class–class).
pub struct CurrentKerning {
    gg: HashMap<(u32, u32), f64>,
    gc: HashMap<(u32, u32), f64>,
    cg: HashMap<(u32, u32), f64>,
    cc: HashMap<(u32, u32), f64>,
}

impl CurrentKerning {
    pub fn new(entries: &[KernIn]) -> Self {
        let mut k = CurrentKerning { gg: HashMap::new(), gc: HashMap::new(), cg: HashMap::new(), cc: HashMap::new() };
        for e in entries.iter().filter(|e| e.value.is_finite()) {
            let map = match e.kind {
                KIND_GLYPH_GLYPH => &mut k.gg,
                KIND_GLYPH_CLASS => &mut k.gc,
                KIND_CLASS_GLYPH => &mut k.cg,
                KIND_CLASS_CLASS => &mut k.cc,
                _ => continue,
            };
            map.insert((e.left, e.right), e.value);
        }
        k
    }

    pub fn value(&self, ctx: &Context, a: usize, b: usize) -> f64 {
        let (ga, gb) = (&ctx.glyphs[a], &ctx.glyphs[b]);
        if let Some(&v) = self.gg.get(&(a as u32, b as u32)) {
            return v;
        }
        let (ra, lb) = (ga.right_group_in, gb.left_group_in);
        if lb != NONE {
            if let Some(&v) = self.gc.get(&(a as u32, lb)) {
                return v;
            }
        }
        if ra != NONE {
            if let Some(&v) = self.cg.get(&(ra, b as u32)) {
                return v;
            }
            if lb != NONE {
                if let Some(&v) = self.cc.get(&(ra, lb)) {
                    return v;
                }
            }
        }
        0.0
    }
}

/// Max-heap entry ordered by `key`.
#[derive(Clone, Copy)]
struct Ranked {
    key: f64,
    out: PairOut,
}
impl PartialEq for Ranked {
    fn eq(&self, o: &Self) -> bool {
        self.key.total_cmp(&o.key) == Ordering::Equal
    }
}
impl Eq for Ranked {}
impl PartialOrd for Ranked {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for Ranked {
    fn cmp(&self, o: &Self) -> Ordering {
        self.key.total_cmp(&o.key)
    }
}

/// Keeps the `cap` largest keys: a min-heap by key (via reversed order).
struct TopK {
    cap: usize,
    heap: BinaryHeap<std::cmp::Reverse<Ranked>>,
}
impl TopK {
    fn new(cap: usize) -> Self {
        TopK { cap, heap: BinaryHeap::with_capacity(cap + 1) }
    }
    fn push(&mut self, key: f64, out: PairOut) {
        if self.cap == 0 {
            return;
        }
        if self.heap.len() < self.cap {
            self.heap.push(std::cmp::Reverse(Ranked { key, out }));
        } else if let Some(min) = self.heap.peek() {
            if key > min.0.key {
                self.heap.pop();
                self.heap.push(std::cmp::Reverse(Ranked { key, out }));
            }
        }
    }
    fn merge(mut self, other: TopK) -> TopK {
        for r in other.heap {
            self.push(r.0.key, r.0.out);
        }
        self
    }
    fn sorted(self) -> Vec<PairOut> {
        let mut v: Vec<Ranked> = self.heap.into_iter().map(|r| r.0).collect();
        v.sort_by(|a, b| b.key.total_cmp(&a.key));
        v.into_iter().map(|r| r.out).collect()
    }
}

/// See the module documentation. `lsb` / `rsb` are the model's
/// sidebearings, `model_kern(a, b)` its kerning; `mask` selects the glyphs.
#[allow(clippy::too_many_arguments)]
pub fn measure(
    ctx: &Context,
    lsb: &[f64],
    rsb: &[f64],
    model_kern: &(dyn Fn(u32, u32) -> f64 + Sync),
    current: &[KernIn],
    mask: &[bool],
    scope_scripts: bool,
    cap: usize,
) -> Measured {
    let cur = CurrentKerning::new(current);
    let glyphs = &ctx.glyphs;
    let idx: Vec<usize> = (0..glyphs.len())
        .filter(|&i| {
            let g = &glyphs[i];
            mask.get(i).copied().unwrap_or(false)
                && g.kernable()
                && g.flags & GLYPH_RTL == 0
                && g.cur_lsb.is_finite()
                && g.cur_rsb.is_finite()
                && lsb.get(i).is_some_and(|x| x.is_finite())
                && rsb.get(i).is_some_and(|x| x.is_finite())
        })
        .collect();
    let scope = |a: usize, b: usize| {
        let (ga, gb) = (&glyphs[a], &glyphs[b]);
        !scope_scripts || ga.script == 0 || gb.script == 0 || ga.script == gb.script
    };
    let pair = |a: usize, b: usize| -> (f64, f64) {
        let now = glyphs[a].cur_rsb + glyphs[b].cur_lsb + cur.value(ctx, a, b);
        let model = rsb[a] + lsb[b] + model_kern(a as u32, b as u32);
        (now, model)
    };
    // pass 1: the overall offset
    let (sum, count) = idx
        .par_iter()
        .map(|&a| {
            let mut s = 0.0;
            let mut c = 0u64;
            for &b in &idx {
                if scope(a, b) {
                    let (now, model) = pair(a, b);
                    s += now - model;
                    c += 1;
                }
            }
            (s, c)
        })
        .reduce(|| (0.0, 0), |x, y| (x.0 + y.0, x.1 + y.1));
    if count == 0 {
        return Measured::default();
    }
    let offset = sum / count as f64;
    // pass 2: residuals and their extremes
    let (abs, sq, loose, tight) = idx
        .par_iter()
        .fold(
            || (0.0, 0.0, TopK::new(cap), TopK::new(cap)),
            |(mut abs, mut sq, mut loose, mut tight), &a| {
                for &b in &idx {
                    if !scope(a, b) {
                        continue;
                    }
                    let (now, model) = pair(a, b);
                    let r = now - model - offset;
                    abs += r.abs();
                    sq += r * r;
                    let out = PairOut { left: a as u32, right: b as u32, current: now, model, residual: r };
                    loose.push(r, out);
                    tight.push(-r, out);
                }
                (abs, sq, loose, tight)
            },
        )
        .reduce(
            || (0.0, 0.0, TopK::new(cap), TopK::new(cap)),
            |x, y| (x.0 + y.0, x.1 + y.1, x.2.merge(y.2), x.3.merge(y.3)),
        );
    let n = count as f64;
    Measured { pairs: count, offset, mae: abs / n, rms: (sq / n).sqrt(), loosest: loose.sorted(), tightest: tight.sorted() }
}
