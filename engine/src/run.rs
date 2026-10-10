//! A solve: Pass 1, Pass 2 over the pair scope (glyph pairs or classes),
//! thresholding, exception compression and the pair budget.

use std::collections::HashMap;
use std::sync::Mutex;
use crate::clock::Instant;

use rayon::prelude::*;

use std::sync::Arc;

use crate::classes::{Classes, PART_FROZEN};
use crate::engine::{Context, GlyphOpt, Pass1, PreparedGlyph, SolveOptions, GLYPH_RTL, GLYPH_ZONE, NONE};
use crate::job::{pool, Cancelled, JobError, Progress};
use crate::pass2::{
    Fields, Kernel, Knobs, PairOut, Probes, Scratch, Solver, Verify, Windows, PAIR_BOUNDED, PAIR_CLEARANCE, PAIR_CREVICE,
    PAIR_FALLBACK, PAIR_SATURATED, PAIR_WINDOW,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Every glyph pair on its own (v1's output; reference and tools).
    Pairs,
    /// Class pairs plus exceptions.
    Classes,
}

pub const KIND_GLYPH_GLYPH: u8 = 0;
pub const KIND_CLASS_CLASS: u8 = 1;
pub const KIND_GLYPH_CLASS: u8 = 2;
pub const KIND_CLASS_GLYPH: u8 = 3;

/// Most class pairs a class-mode solve takes on. Each costs about 70 bytes
/// while the solve runs (its pair, representative result and exception list)
/// and a full solve; Arial has 0.8 million. A font with ten thousand distinct
/// shapes in one script (a CJK font) would have 10^8: the solve fails with a
/// message instead of filling the memory of the application it runs in.
pub const MAX_CLASS_PAIRS: u64 = 10_000_000;

#[derive(Clone, Debug)]
pub struct Params {
    pub options: SolveOptions,
    pub mode: Mode,
    pub solver: Solver,
    /// Pairs only within one script (plus Common/Inherited).
    pub scope_scripts: bool,
    /// Results with |value| below this are dropped (font units).
    pub threshold: f64,
    /// Maximum entries written; 0 = unlimited.
    pub budget: usize,
    /// Classes: a member's difference from its representative matters within
    /// this many rest gaps of the partner's ink.
    pub radius_ratio: f64,
    pub threads: usize,
    /// Per-glyph options (frozen glyphs, section Looseness offsets).
    pub glyph_opts: Option<Arc<Vec<GlyphOpt>>>,
    /// Move the solve's Looseness to the frozen glyphs' own tightness first
    /// (`Context::fit_looseness` on the frozen glyphs).
    pub fit_frozen: bool,
    /// Corrections toward what well-spaced fonts do, after the solve.
    pub harness: Option<Arc<Harness>>,
    /// Keep the solve as it was before the harness (`Outcome::bare`).
    pub keep_bare: bool,
}

/// The designer harness: where the model consistently spaces differently from
/// designers (learned from well-spaced fonts), as corrections applied after
/// the solve, in font units. `sides[i]` shifts glyph i's [lsb, rsb]; each of
/// `pairs` (left glyph, right glyph, value) adds to that pair's kerning,
/// written as a glyph–glyph entry over whatever class kerning it has. Frozen
/// glyphs keep their sidebearings, and a pair of two frozen glyphs its
/// kerning. The caller passes a side that follows another glyph (metrics
/// key, composite) that glyph's shift.
#[derive(Clone, Debug, Default)]
pub struct Harness {
    pub sides: Vec<[f64; 2]>,
    pub pairs: Vec<(u32, u32, f64)>,
}

#[derive(Clone, Copy, Debug)]
pub struct Entry {
    pub kind: u8,
    /// Glyph or class indices, depending on `kind`.
    pub left: u32,
    pub right: u32,
    pub value: f64,
    pub importance: f64,
    /// Exceptions: index of the class pair they refine (internal), else NONE.
    pub parent: u32,
}

#[derive(Clone, Debug, Default)]
pub struct RunStats {
    pub pass2_ms: f64,
    pub prune_ms: f64,
    pub threads: u32,
    pub kern_glyphs: u64,
    pub probe_glyphs: u64,
    pub pairs_in_scope: u64,
    pub class_pairs: u64,
    pub member_pairs: u64,
    pub inherited: u64,
    pub verified: u64,
    pub verified_within: u64,
    pub solved: u64,
    pub force_evaluations: u64,
    pub merged_rays: u64,
    pub active_rays: u64,
    pub clearance_hits: u64,
    pub crevice_hits: u64,
    pub saturated: u64,
    pub window_hits: u64,
    pub bound_hits: u64,
    pub fallbacks: u64,
    pub entries_before_budget: u64,
    pub class_entries: u64,
    pub exception_entries: u64,
    pub dropped_by_budget: u64,
}

pub struct Outcome {
    pub pass1: Pass1,
    pub mode: Mode,
    pub entries: Vec<Entry>,
    pub kern: Vec<bool>,
    pub stats: RunStats,
    /// Classes built for this solve (frozen glyphs); None = the context's.
    pub classes: Option<Classes>,
    /// Looseness offset found by `fit_frozen` (slider units), else NaN.
    pub fitted: f64,
    /// With `Params::keep_bare` and a harness: the same solve before the
    /// harness (the bare model's sidebearings and kerning).
    pub bare: Option<(Pass1, Vec<Entry>)>,
}

#[inline]
fn script_ok(a: &PreparedGlyph, b: &PreparedGlyph, on: bool) -> bool {
    !on || a.script == 0 || b.script == 0 || a.script == b.script
}

fn tally(stats: &mut RunStats, out: &PairOut) {
    stats.force_evaluations += out.evals as u64;
    stats.merged_rays += out.rays as u64;
    stats.active_rays += out.active as u64;
    if out.flags & PAIR_CLEARANCE != 0 {
        stats.clearance_hits += 1;
    }
    if out.flags & PAIR_CREVICE != 0 {
        stats.crevice_hits += 1;
    }
    if out.flags & PAIR_SATURATED != 0 {
        stats.saturated += 1;
    }
    if out.flags & PAIR_WINDOW != 0 {
        stats.window_hits += 1;
    }
    if out.flags & PAIR_FALLBACK != 0 {
        stats.fallbacks += 1;
    }
    if out.flags & PAIR_BOUNDED != 0 {
        stats.bound_hits += 1;
    }
}

fn merge_stats(a: &mut RunStats, b: &RunStats) {
    a.pairs_in_scope += b.pairs_in_scope;
    a.force_evaluations += b.force_evaluations;
    a.merged_rays += b.merged_rays;
    a.active_rays += b.active_rays;
    a.clearance_hits += b.clearance_hits;
    a.crevice_hits += b.crevice_hits;
    a.saturated += b.saturated;
    a.window_hits += b.window_hits;
    a.bound_hits += b.bound_hits;
    a.fallbacks += b.fallbacks;
    a.inherited += b.inherited;
    a.verified += b.verified;
    a.verified_within += b.verified_within;
    a.solved += b.solved;
    a.member_pairs += b.member_pairs;
}

/// Statistics of one rayon work split, added to a shared total when the
/// split ends: per-item work keeps no statistics of its own.
struct LocalStats<'a> {
    stats: RunStats,
    total: &'a Mutex<RunStats>,
}

impl<'a> LocalStats<'a> {
    fn new(total: &'a Mutex<RunStats>) -> Self {
        LocalStats { stats: RunStats::default(), total }
    }
}

impl Drop for LocalStats<'_> {
    fn drop(&mut self) {
        merge_stats(&mut self.total.lock().unwrap_or_else(|e| e.into_inner()), &self.stats);
    }
}

/// Runs a solve. `mask[i] != 0` selects glyph `i` for kerning (None: all).
/// Progress phases: 2/3 evaluating pairs, 3/3 grouping and pruning. Fails
/// (rather than runs) when a class-mode solve exceeds MAX_CLASS_PAIRS.
pub fn run(ctx: &Context, p: &Params, mask: Option<&[u8]>, progress: &Progress) -> Result<Outcome, JobError> {
    let n = ctx.glyphs.len();
    let opts: Option<&[GlyphOpt]> = p.glyph_opts.as_deref().map(|v| v.as_slice());
    let frozen: Vec<bool> = (0..n).map(|i| opts.and_then(|o| o.get(i)).is_some_and(|x| x.frozen)).collect();
    let any_frozen = frozen.iter().any(|&f| f);
    // sides that keep their sidebearings: frozen glyphs and kept joins
    let fixed_l: Vec<bool> = (0..n).map(|i| frozen[i] || ctx.glyphs[i].kept_left).collect();
    let fixed_r: Vec<bool> = (0..n).map(|i| frozen[i] || ctx.glyphs[i].kept_right).collect();
    let any_kept = ctx.glyphs.iter().any(|g| g.kept_left || g.kept_right);
    // the frozen glyphs' (and kept joins') own tightness, then the solve at
    // it: the kept joins of the lowercase base letters (GLYPH_ZONE, where the
    // caller marks them), as a script's swash capitals and alternates reach
    // far past their bodies (Great Vibes' capitals alone fit below −6, its
    // lowercase at −0.5)
    let mut fitted = f64::NAN;
    let mut options = p.options.clone();
    if p.fit_frozen && (any_frozen || any_kept) {
        let zone_marks = ctx.glyphs.iter().any(|g| g.flags & GLYPH_ZONE != 0);
        let lowercase_base = |g: &PreparedGlyph| {
            g.group_id == crate::engine::GROUP_LOWERCASE && (g.flags & GLYPH_ZONE != 0 || !zone_marks)
        };
        let base_kept = ctx.glyphs.iter().any(|g| (g.kept_left || g.kept_right) && lowercase_base(g));
        let counts = |i: usize| !base_kept || lowercase_base(&ctx.glyphs[i]);
        let fit_l: Vec<bool> = (0..n).map(|i| frozen[i] || (ctx.glyphs[i].kept_left && counts(i))).collect();
        let fit_r: Vec<bool> = (0..n).map(|i| frozen[i] || (ctx.glyphs[i].kept_right && counts(i))).collect();
        if let Some(dt) = ctx.fit_looseness_sides(&options, &fit_l, &fit_r) {
            fitted = dt;
            options = options.shifted(dt);
        }
    }
    let p_local;
    let p = if fitted.is_finite() {
        p_local = Params { options, ..p.clone() };
        &p_local
    } else {
        p
    };
    let mut pass1 = ctx.pass1_with(&p.options, opts);
    // partitions: frozen glyphs, and the kerning force of each painted group
    let mut forces: Vec<f64> = Vec::new();
    let part: Vec<u32> = (0..n)
        .map(|i| {
            let o = opts.and_then(|o| o.get(i)).copied().unwrap_or_default();
            if o.frozen {
                return PART_FROZEN;
            }
            let f = if o.intensity.is_finite() { o.intensity.max(0.0) } else { 1.0 };
            match forces.iter().position(|&x| (x - f).abs() < 1e-9) {
                Some(k) => k as u32,
                None => {
                    forces.push(f);
                    (forces.len() - 1) as u32
                }
            }
        })
        .collect();
    let classes = if (any_frozen || forces.len() > 1) && p.mode == Mode::Classes {
        Some(Classes::build_with(&ctx.glyphs, ctx.upm, Some(&part)))
    } else {
        None
    };
    let force: Option<Vec<f64>> = opts.filter(|o| o.iter().any(|x| (x.intensity - 1.0).abs() > 1e-12)).map(|o| {
        (0..n).map(|i| o.get(i).map_or(1.0, |x| if x.intensity.is_finite() { x.intensity.max(0.0) } else { 1.0 })).collect()
    });
    let kern: Vec<bool> = (0..n)
        .map(|i| {
            let g = &ctx.glyphs[i];
            g.kernable() && g.flags & GLYPH_RTL == 0 && mask.map_or(true, |m| m.get(i).copied().unwrap_or(0) != 0)
        })
        .collect();
    let mut stats = RunStats { threads: pool(p.threads).current_num_threads() as u32, ..RunStats::default() };
    stats.kern_glyphs = kern.iter().filter(|&&k| k).count() as u64;
    if p.options.coupling <= 0.0 || stats.kern_glyphs == 0 {
        progress.begin(3, 3, 1);
        progress.add(1);
        let mut entries = Vec::new();
        let mut bare = None;
        if let Some(h) = &p.harness {
            if p.keep_bare {
                bare = Some((pass1.clone(), entries.clone()));
            }
            let built = classes.as_ref().unwrap_or(&ctx.classes);
            apply_harness(h, ctx, &mut pass1, &mut entries, built, p.mode, (&fixed_l, &fixed_r), &kern, p.threshold, false);
        }
        return Ok(Outcome { pass1, mode: p.mode, entries, kern, stats, classes, fitted, bare });
    }
    let t2 = Instant::now();
    let pool = pool(p.threads);
    let fields = Fields::new(ctx, &p.options);
    let knobs = Knobs::new(&p.options, ctx.upm, p.threshold);
    let frozen_ref: Option<&[bool]> = if any_frozen { Some(&frozen) } else { None };
    let entries = pool.install(|| match p.mode {
        Mode::Pairs => run_pairs(ctx, p, &pass1, &kern, frozen_ref, force.as_deref(), &fields, knobs, progress, &mut stats)
            .map_err(JobError::from),
        Mode::Classes => run_classes(
            ctx,
            p,
            &pass1,
            &kern,
            classes.as_ref().unwrap_or(&ctx.classes),
            frozen_ref,
            force.as_deref(),
            &fields,
            knobs,
            progress,
            &mut stats,
        ),
    })?;
    stats.pass2_ms = t2.elapsed().as_secs_f64() * 1000.0;
    let t3 = Instant::now();
    let mut entries = prune(entries, p.budget, &mut stats);
    let built = classes.as_ref().unwrap_or(&ctx.classes);
    // kept joins at the font's own values, whatever the threshold and budget did
    keep_joins(ctx, &mut entries, built, p.mode, &kern, &frozen, p.scope_scripts);
    let mut bare = None;
    if let Some(h) = &p.harness {
        if p.keep_bare {
            bare = Some((pass1.clone(), entries.clone()));
        }
        apply_harness(h, ctx, &mut pass1, &mut entries, built, p.mode, (&fixed_l, &fixed_r), &kern, p.threshold, true);
    }
    stats.prune_ms = t3.elapsed().as_secs_f64() * 1000.0;
    progress.add(1);
    Ok(Outcome { pass1, mode: p.mode, entries, kern, stats, classes, fitted, bare })
}

/// Keep joins: every pair of two kept sides gets the font's kerning, exactly
/// (glyph–glyph entries where the class kerning, the threshold or the budget
/// left another value). Most such pairs already have it: Pass 2 gives kept
/// pairs their own values, so classes of them carry the font's class kerning.
fn keep_joins(
    ctx: &Context,
    entries: &mut Vec<Entry>,
    classes: &Classes,
    mode: Mode,
    kern: &[bool],
    frozen: &[bool],
    scope_scripts: bool,
) {
    let Some(fj) = ctx.joins.as_ref().filter(|j| j.keep) else {
        return;
    };
    let n = ctx.glyphs.len();
    let left: Vec<usize> = (0..n).filter(|&i| kern[i] && ctx.glyphs[i].kept_right).collect();
    let right: Vec<usize> = (0..n).filter(|&i| kern[i] && ctx.glyphs[i].kept_left).collect();
    if left.is_empty() || right.is_empty() {
        return;
    }
    let mut gg: HashMap<(u32, u32), usize> = HashMap::new();
    let mut gc: HashMap<(u32, u32), f64> = HashMap::new();
    let mut cg: HashMap<(u32, u32), f64> = HashMap::new();
    let mut cc: HashMap<(u32, u32), f64> = HashMap::new();
    for (k, e) in entries.iter().enumerate() {
        match e.kind {
            KIND_GLYPH_GLYPH => {
                gg.insert((e.left, e.right), k);
            }
            KIND_GLYPH_CLASS => {
                gc.insert((e.left, e.right), e.value);
            }
            KIND_CLASS_GLYPH => {
                cg.insert((e.left, e.right), e.value);
            }
            _ => {
                cc.insert((e.left, e.right), e.value);
            }
        }
    }
    let (rc, lc) = (&classes.right.class_of, &classes.left.class_of);
    let fixes: Vec<(u32, u32, f64, Option<usize>)> = left
        .par_iter()
        .flat_map_iter(|&a| right.iter().map(move |&b| (a, b)))
        .filter_map(|(a, b)| {
            // two frozen glyphs keep whatever kerning is between them
            if !script_ok(&ctx.glyphs[a], &ctx.glyphs[b], scope_scripts) || (frozen[a] && frozen[b]) {
                return None;
            }
            let (l, r) = (a as u32, b as u32);
            let want = fj.font_kern(a, b);
            let at = gg.get(&(l, r)).copied();
            let have = match at {
                Some(k) => entries[k].value,
                None if mode == Mode::Classes => {
                    let (ra, lb) = (rc.get(a).copied().unwrap_or(NONE), lc.get(b).copied().unwrap_or(NONE));
                    gc.get(&(l, lb)).or_else(|| cg.get(&(ra, r))).or_else(|| cc.get(&(ra, lb))).copied().unwrap_or(0.0)
                }
                None => 0.0,
            };
            ((have - want).abs() > 1e-9).then_some((l, r, want, at))
        })
        .collect();
    for (l, r, want, at) in fixes {
        match at {
            Some(k) => entries[k].value = want,
            None => entries.push(Entry {
                kind: KIND_GLYPH_GLYPH,
                left: l,
                right: r,
                value: want,
                importance: importance(want, 0.0, ctx.upm),
                parent: NONE,
            }),
        }
    }
}

/// Applies the designer harness to a finished solve (see `Harness`): after
/// the budget, so its entries are always written.
#[allow(clippy::too_many_arguments)]
fn apply_harness(
    h: &Harness,
    ctx: &Context,
    pass1: &mut Pass1,
    entries: &mut Vec<Entry>,
    classes: &Classes,
    mode: Mode,
    (fixed_l, fixed_r): (&[bool], &[bool]),
    kern: &[bool],
    threshold: f64,
    kerning: bool,
) {
    let n = ctx.glyphs.len();
    for (i, s) in h.sides.iter().enumerate().take(n) {
        // a glyph whose advance is kept (tabular figures, width keys, a kept
        // join on a glyph with a width key) keeps its sides too
        if (fixed_l[i] && fixed_r[i]) || !pass1.metrics[i].valid || ctx.glyphs[i].fixed_advance {
            continue;
        }
        let dl = if s[0].is_finite() && !fixed_l[i] { s[0] } else { 0.0 };
        let dr = if s[1].is_finite() && !fixed_r[i] { s[1] } else { 0.0 };
        let m = &mut pass1.metrics[i];
        m.lsb += dl;
        m.rsb += dr;
        m.advance += dl + dr;
        pass1.lsb[i] += dl;
        pass1.rsb[i] += dr;
    }
    if !kerning || h.pairs.is_empty() {
        return;
    }
    // the kerning a pair has now, with the usual precedence
    let mut gg: HashMap<(u32, u32), usize> = HashMap::new();
    let mut gc: HashMap<(u32, u32), f64> = HashMap::new();
    let mut cg: HashMap<(u32, u32), f64> = HashMap::new();
    let mut cc: HashMap<(u32, u32), f64> = HashMap::new();
    for (k, e) in entries.iter().enumerate() {
        match e.kind {
            KIND_GLYPH_GLYPH => {
                gg.insert((e.left, e.right), k);
            }
            KIND_GLYPH_CLASS => {
                gc.insert((e.left, e.right), e.value);
            }
            KIND_CLASS_GLYPH => {
                cg.insert((e.left, e.right), e.value);
            }
            _ => {
                cc.insert((e.left, e.right), e.value);
            }
        }
    }
    let (rc, lc) = (&classes.right.class_of, &classes.left.class_of);
    for &(l, r, d) in &h.pairs {
        let (li, ri) = (l as usize, r as usize);
        if li >= n || ri >= n || !d.is_finite() || d == 0.0 {
            continue;
        }
        if !kern[li] || !kern[ri] || (fixed_r[li] && fixed_l[ri]) {
            continue;
        }
        if let Some(&k) = gg.get(&(l, r)) {
            entries[k].value += d;
            continue;
        }
        let base = if mode == Mode::Classes {
            let (ra, lb) = (rc.get(li).copied().unwrap_or(NONE), lc.get(ri).copied().unwrap_or(NONE));
            gc.get(&(l, lb)).or_else(|| cg.get(&(ra, r))).or_else(|| cc.get(&(ra, lb))).copied().unwrap_or(0.0)
        } else {
            0.0
        };
        let v = base + d;
        if base == 0.0 && v.abs() < threshold {
            continue;
        }
        entries.push(Entry {
            kind: KIND_GLYPH_GLYPH,
            left: l,
            right: r,
            value: v,
            importance: importance(v, 0.0, ctx.upm),
            parent: NONE,
        });
        gg.insert((l, r), entries.len() - 1);
    }
}

/// Probes and window extremes for every glyph in `needed`, against every
/// rhythm group among them.
fn probes_for(
    ctx: &Context,
    fields: &Fields,
    needed: &[bool],
    progress: &Progress,
    unit: u64,
) -> Result<(Probes, Windows), Cancelled> {
    let mut partners = vec![false; fields.ng];
    for (i, g) in ctx.glyphs.iter().enumerate() {
        if needed[i] && g.valid {
            partners[g.group] = true;
        }
    }
    let probes = Probes::compute(ctx, fields, needed, &partners, progress, unit)?;
    Ok((probes, Windows::compute(ctx, fields, needed, &partners)))
}

fn importance(value: f64, white: f64, upm: f64) -> f64 {
    value.abs() / (white.max(0.0) + 0.05 * upm)
}

#[allow(clippy::too_many_arguments)]
fn run_pairs(
    ctx: &Context,
    p: &Params,
    pass1: &Pass1,
    kern: &[bool],
    frozen: Option<&[bool]>,
    force: Option<&[f64]>,
    fields: &Fields,
    knobs: Knobs,
    progress: &Progress,
    stats: &mut RunStats,
) -> Result<Vec<Entry>, Cancelled> {
    let glyphs = &ctx.glyphs;
    let k: Vec<usize> = (0..glyphs.len()).filter(|&i| kern[i]).collect();
    let rays = |i: usize, right: bool| {
        (if right { glyphs[i].right.rays.len() } else { glyphs[i].left.rays.len() }) as u64
    };
    // work estimate: probes, then every pair's rays
    let right_sum: u64 = k.iter().map(|&i| rays(i, true)).sum();
    let left_sum: u64 = k.iter().map(|&i| rays(i, false)).sum();
    let probe_work = 40 * k.len() as u64;
    progress.begin(2, 3, probe_work + k.len() as u64 * (right_sum + left_sum) / 2 + 1);
    let (probes, windows) = probes_for(ctx, fields, kern, progress, 40)?;
    let kernel = Kernel {
        ctx,
        fields,
        probes: &probes,
        windows: &windows,
        lsb: &pass1.lsb,
        rsb: &pass1.rsb,
        knobs,
        solver: p.solver,
        force,
    };
    let upm = ctx.upm;
    let rows: Vec<Option<(Vec<Entry>, RunStats)>> = k
        .par_iter()
        .map_init(Scratch::default, |sc, &ia| {
            let mut out = Vec::new();
            let mut st = RunStats::default();
            let mut work = 0u64;
            for &ib in &k {
                if progress.cancelled() {
                    return None;
                }
                if !script_ok(&glyphs[ia], &glyphs[ib], p.scope_scripts) {
                    continue;
                }
                if frozen.is_some_and(|f| f[ia] && f[ib]) {
                    continue; // both glyphs frozen: their kerning stays as it is
                }
                st.member_pairs += 1;
                let r = kernel.solve(ia, ib, sc);
                tally(&mut st, &r);
                st.solved += 1;
                if r.value.abs() >= knobs.threshold {
                    out.push(Entry {
                        kind: crate::run::KIND_GLYPH_GLYPH,
                        left: ia as u32,
                        right: ib as u32,
                        value: r.value,
                        importance: importance(r.value, r.white, upm),
                        parent: NONE,
                    });
                }
                work += (rays(ia, true) + rays(ib, false)) / 2;
                if work > 4096 {
                    progress.add(work);
                    work = 0;
                }
            }
            progress.add(work);
            Some((out, st))
        })
        .collect();
    progress.begin(3, 3, 2);
    progress.add(1);
    let mut entries = Vec::new();
    for row in rows {
        let (e, st) = row.ok_or(Cancelled)?;
        entries.extend(e);
        merge_stats(stats, &st);
    }
    stats.pairs_in_scope = stats.member_pairs;
    stats.probe_glyphs = k.len() as u64;
    Ok(entries)
}

/// A class pair's representative result.
#[derive(Clone, Copy, Debug, Default)]
struct RepOut {
    value: f64,
    pre: f64,
    white: f64,
    flags: u32,
}

/// One member pair whose value differs from its class pair's.
#[derive(Clone, Copy, Debug)]
struct Exception {
    pair: u32,
    m: u32,
    n: u32,
    value: f64,
    importance: f64,
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_arguments)]
fn run_classes(
    ctx: &Context,
    p: &Params,
    pass1: &Pass1,
    kern: &[bool],
    classes: &Classes,
    frozen: Option<&[bool]>,
    force: Option<&[f64]>,
    fields: &Fields,
    knobs: Knobs,
    progress: &Progress,
    stats: &mut RunStats,
) -> Result<Vec<Entry>, JobError> {
    let glyphs = &ctx.glyphs;
    let (rc, lc) = (&classes.right, &classes.left);
    // classes never mix frozen and unfrozen glyphs: a pair of two frozen
    // classes keeps the kerning it has
    let (r_frozen, l_frozen) = match frozen {
        Some(f) => (Classes::frozen_classes(rc, f), Classes::frozen_classes(lc, f)),
        None => (vec![false; rc.len()], vec![false; lc.len()]),
    };
    let upm = ctx.upm;
    let t = knobs.threshold;

    // members of every class inside the kern set
    let in_k = |members: &Vec<u32>| -> Vec<u32> { members.iter().copied().filter(|&g| kern[g as usize]).collect() };
    let r_members: Vec<Vec<u32>> = rc.members.iter().map(in_k).collect();
    let l_members: Vec<Vec<u32>> = lc.members.iter().map(in_k).collect();
    // scripts of every class (sorted), and whether it holds Common glyphs
    let scripts_of = |members: &Vec<u32>| -> (Vec<u32>, bool) {
        let mut s: Vec<u32> = members.iter().map(|&g| glyphs[g as usize].script).filter(|&s| s != 0).collect();
        s.sort_unstable();
        s.dedup();
        let common = members.iter().any(|&g| glyphs[g as usize].script == 0);
        (s, common)
    };
    let r_scripts: Vec<(Vec<u32>, bool)> = r_members.iter().map(scripts_of).collect();
    let l_scripts: Vec<(Vec<u32>, bool)> = l_members.iter().map(scripts_of).collect();
    let class_scope = |pi: usize, qi: usize| -> bool {
        if r_frozen[pi] && l_frozen[qi] {
            return false;
        }
        if !p.scope_scripts {
            return true;
        }
        let ((ps, pc), (qs, qc)) = (&r_scripts[pi], &l_scripts[qi]);
        *pc || *qc || ps.iter().any(|s| qs.binary_search(s).is_ok())
    };
    let active_r: Vec<usize> = (0..rc.len()).filter(|&k| !r_members[k].is_empty()).collect();
    let active_l: Vec<usize> = (0..lc.len()).filter(|&k| !l_members[k].is_empty()).collect();
    // counted before anything is allocated for them
    let mut count = 0u64;
    for &pi in &active_r {
        if progress.cancelled() {
            return Err(JobError::Cancelled);
        }
        count += active_l.iter().filter(|&&qi| class_scope(pi, qi)).count() as u64;
    }
    if count > MAX_CLASS_PAIRS {
        return Err(JobError::Failed(format!(
            "{} right classes × {} left classes make {} class pairs, more than the {} one solve takes on. \
             Kern the glyphs of the sample text, or a smaller set of glyphs, instead of the whole font.",
            active_r.len(),
            active_l.len(),
            count,
            MAX_CLASS_PAIRS
        )));
    }
    let mut pairs: Vec<(u32, u32)> = Vec::with_capacity(count as usize);
    for &pi in &active_r {
        if progress.cancelled() {
            return Err(JobError::Cancelled);
        }
        pairs.extend(active_l.iter().filter(|&&qi| class_scope(pi, qi)).map(|&qi| (pi as u32, qi as u32)));
    }
    stats.class_pairs = pairs.len() as u64;

    // probes: every kerned glyph and every representative in play
    let mut needed = kern.to_vec();
    for &pi in &active_r {
        needed[rc.rep[pi] as usize] = true;
    }
    for &qi in &active_l {
        needed[lc.rep[qi] as usize] = true;
    }
    stats.probe_glyphs = needed.iter().filter(|&&b| b).count() as u64;

    // work estimate: probes, representative pairs (full solves), member pairs
    let member_count: u64 =
        pairs.iter().map(|&(pi, qi)| (r_members[pi as usize].len() * l_members[qi as usize].len()) as u64).sum();
    const REP_WORK: u64 = 16;
    const PROBE_WORK: u64 = 24;
    let probe_work = PROBE_WORK * stats.probe_glyphs;
    progress.begin(2, 3, probe_work + REP_WORK * pairs.len() as u64 + member_count + 1);
    let (probes, windows) = probes_for(ctx, fields, &needed, progress, PROBE_WORK)?;
    let kernel = Kernel {
        ctx,
        fields,
        probes: &probes,
        windows: &windows,
        lsb: &pass1.lsb,
        rsb: &pass1.rsb,
        knobs,
        solver: p.solver,
        force,
    };

    // Statistics go to per-split accumulators, not to every item: a class
    // pair keeps only its result. A cancelled item leaves a placeholder; the
    // flag is checked once the collect is done (it never clears).
    let total = Mutex::new(RunStats::default());

    // 1. representative pairs
    let rep_out: Vec<RepOut> = pairs
        .par_iter()
        .with_min_len(16)
        .map_init(
            || (Scratch::default(), LocalStats::new(&total)),
            |(sc, local), &(pi, qi)| {
                if progress.cancelled() {
                    return RepOut::default();
                }
                let (rp, rq) = (rc.rep[pi as usize] as usize, lc.rep[qi as usize] as usize);
                let r = kernel.solve(rp, rq, sc);
                tally(&mut local.stats, &r);
                local.stats.solved += 1;
                progress.add(REP_WORK);
                RepOut { value: r.value, pre: r.pre, white: r.white, flags: r.flags }
            },
        )
        .collect();
    if progress.cancelled() {
        return Err(JobError::Cancelled);
    }

    // 2. member pairs: inherit, verify, or solve
    // A member shares its class value unevaluated only where its evaluation
    // would be the representative's: the same rays near the partner and the
    // same probes (a probe a fraction of a unit off moves a value sitting at
    // the threshold across it).
    let eps_probe = 1e-9 * upm;
    let ratio = p.radius_ratio.max(0.0);
    let results: Vec<Vec<Exception>> = (0..pairs.len())
        .into_par_iter()
        .with_min_len(8)
        .map_init(
            || (Scratch::default(), LocalStats::new(&total)),
            |(sc, local), idx| {
                let (pi, qi) = (pairs[idx].0 as usize, pairs[idx].1 as usize);
                let (rp, rq) = (rc.rep[pi] as usize, lc.rep[qi] as usize);
                let rep = rep_out[idx];
                let v_eff = if rep.value.abs() >= t { rep.value } else { 0.0 };
                let mut exc = Vec::new();
                let st = &mut local.stats;
                let mut work = 0u64;
                for &m in &r_members[pi] {
                    let m = m as usize;
                    for &nn in &l_members[qi] {
                        let nn = nn as usize;
                        work += 1;
                        if !script_ok(&glyphs[m], &glyphs[nn], p.scope_scripts) {
                            continue;
                        }
                        st.pairs_in_scope += 1;
                        if m == rp && nn == rq {
                            continue; // the representative pair itself
                        }
                        if progress.cancelled() {
                            return Vec::new();
                        }
                        st.member_pairs += 1;
                        let f = fields.get(glyphs[m].group, glyphs[nn].group);
                        let radius = ratio * f.g_ref;
                        let (gm, gn) = (&glyphs[m], &glyphs[nn]);
                        let same_m = m == rp
                            || (!rc.diff[m].hits(gn.ink_left.0 - radius, gn.ink_left.1 + radius)
                                && (probes.right(m, gn.group) - probes.right(rp, gn.group) - rc.shift[m]).abs()
                                    <= eps_probe);
                        let same_n = nn == rq
                            || (!lc.diff[nn].hits(gm.ink_right.0 - radius, gm.ink_right.1 + radius)
                                && (probes.left(nn, gm.group) - probes.left(rq, gm.group) - lc.shift[nn]).abs()
                                    <= eps_probe);
                        let shareable = rep.flags & PAIR_FALLBACK == 0;
                        let value = if same_m && same_n && shareable {
                            st.inherited += 1;
                            kernel.finish(m, nn, rep.pre).0
                        } else {
                            st.verified += 1;
                            let (v, evals, bounded) = if shareable {
                                kernel.verify(m, nn, rep.pre, sc)
                            } else {
                                (Verify::Differs, 0, false)
                            };
                            st.force_evaluations += evals as u64;
                            if bounded {
                                st.bound_hits += 1;
                            }
                            if v == Verify::Within {
                                st.verified_within += 1;
                                kernel.finish(m, nn, rep.pre).0
                            } else {
                                st.solved += 1;
                                let r = kernel.solve(m, nn, sc);
                                tally(st, &r);
                                r.value
                            }
                        };
                        let value = if value.abs() >= t { value } else { 0.0 };
                        if (value - v_eff).abs() >= t {
                            exc.push(Exception {
                                pair: idx as u32,
                                m: m as u32,
                                n: nn as u32,
                                value,
                                importance: importance(value - v_eff, rep.white, upm),
                            });
                        }
                    }
                }
                progress.add(work);
                exc
            },
        )
        .collect();
    if progress.cancelled() {
        return Err(JobError::Cancelled);
    }
    progress.begin(3, 3, 2);
    merge_stats(stats, &total.into_inner().unwrap_or_else(|e| e.into_inner()));
    let mut exceptions = Vec::with_capacity(results.iter().map(Vec::len).sum());
    for e in results {
        exceptions.extend(e);
    }

    // 3. entries: class pairs, then exceptions compressed to glyph–class /
    //    class–glyph where every partner in the class agrees
    let mut entries: Vec<Entry> = Vec::new();
    let mut class_entry_of = vec![NONE; pairs.len()];
    for (idx, &(pi, qi)) in pairs.iter().enumerate() {
        let rep = rep_out[idx];
        if rep.value.abs() >= t {
            let coverage = (r_members[pi as usize].len() * l_members[qi as usize].len()) as f64;
            class_entry_of[idx] = entries.len() as u32;
            entries.push(Entry {
                kind: crate::run::KIND_CLASS_CLASS,
                left: pi,
                right: qi,
                value: rep.value,
                importance: importance(rep.value, rep.white, upm) * coverage.sqrt(),
                parent: NONE,
            });
        }
    }
    // partners of glyph m in class Q (in scope), and of glyph n in class P
    let partners_in = |g: usize, members: &Vec<u32>| -> usize {
        members.iter().filter(|&&o| script_ok(&glyphs[g], &glyphs[o as usize], p.scope_scripts)).count()
    };
    let rounded = |v: f64| v.round() as i64;
    let mut by_mq: HashMap<(u32, u32), Vec<usize>> = HashMap::new();
    let mut by_pn: HashMap<(u32, u32), Vec<usize>> = HashMap::new();
    for (k, e) in exceptions.iter().enumerate() {
        let (pi, qi) = pairs[e.pair as usize];
        by_mq.entry((e.m, qi)).or_default().push(k);
        by_pn.entry((pi, e.n)).or_default().push(k);
    }
    let mut covered = vec![false; exceptions.len()];
    let uniform = |ks: &Vec<usize>| ks.iter().all(|&k| rounded(exceptions[k].value) == rounded(exceptions[ks[0]].value));
    let mut keys: Vec<_> = by_mq.keys().copied().collect();
    keys.sort_unstable();
    for key in keys {
        let ks = &by_mq[&key];
        let (m, qi) = key;
        if ks.len() >= 2 && ks.len() == partners_in(m as usize, &l_members[qi as usize]) && uniform(ks) {
            let e0 = exceptions[ks[0]];
            let imp: f64 = ks.iter().map(|&k| exceptions[k].importance).fold(0.0, f64::max);
            entries.push(Entry {
                kind: crate::run::KIND_GLYPH_CLASS,
                left: m,
                right: qi,
                value: e0.value,
                importance: imp * (ks.len() as f64).sqrt(),
                parent: class_entry_of[e0.pair as usize],
            });
            for &k in ks {
                covered[k] = true;
            }
        }
    }
    let mut keys: Vec<_> = by_pn.keys().copied().collect();
    keys.sort_unstable();
    for key in keys {
        let ks = &by_pn[&key];
        let (pi, nn) = key;
        if ks.len() >= 2
            && ks.iter().any(|&k| !covered[k])
            && ks.len() == partners_in(nn as usize, &r_members[pi as usize])
            && uniform(ks)
        {
            let e0 = exceptions[ks[0]];
            let imp: f64 = ks.iter().map(|&k| exceptions[k].importance).fold(0.0, f64::max);
            entries.push(Entry {
                kind: crate::run::KIND_CLASS_GLYPH,
                left: pi,
                right: nn,
                value: e0.value,
                importance: imp * (ks.len() as f64).sqrt(),
                parent: class_entry_of[e0.pair as usize],
            });
            for &k in ks {
                covered[k] = true;
            }
        }
    }
    for (k, e) in exceptions.iter().enumerate() {
        if !covered[k] {
            entries.push(Entry {
                kind: crate::run::KIND_GLYPH_GLYPH,
                left: e.m,
                right: e.n,
                value: e.value,
                importance: e.importance,
                parent: class_entry_of[e.pair as usize],
            });
        }
    }
    Ok(entries)
}

/// Applies the budget: keeps the most important entries; an exception goes
/// with the class pair it refines.
fn prune(mut entries: Vec<Entry>, budget: usize, stats: &mut RunStats) -> Vec<Entry> {
    stats.entries_before_budget = entries.len() as u64;
    if budget > 0 && entries.len() > budget {
        let mut order: Vec<usize> = (0..entries.len()).collect();
        order.sort_by(|&a, &b| entries[b].importance.total_cmp(&entries[a].importance).then(a.cmp(&b)));
        let mut keep = vec![false; entries.len()];
        for &i in order.iter().take(budget) {
            keep[i] = true;
        }
        // exceptions of dropped class pairs go too
        for i in 0..entries.len() {
            let parent = entries[i].parent;
            if keep[i] && parent != NONE && !keep[parent as usize] {
                keep[i] = false;
            }
        }
        let mut kept = Vec::with_capacity(budget);
        for (i, e) in entries.drain(..).enumerate() {
            if keep[i] {
                kept.push(Entry { parent: NONE, ..e });
            }
        }
        stats.dropped_by_budget = stats.entries_before_budget - kept.len() as u64;
        entries = kept;
    } else {
        for e in entries.iter_mut() {
            e.parent = NONE;
        }
    }
    stats.class_entries = entries.iter().filter(|e| e.kind == KIND_CLASS_CLASS).count() as u64;
    stats.exception_entries = entries.len() as u64 - stats.class_entries;
    entries
}
