//! # Kinetikern2 — engine and C ABI
//!
//! `libkinetikern2.dylib` spaces a font (Pass 1, macro rhythm) and kerns it
//! (Pass 2, SDF contour field) as **asynchronous jobs** on threads it owns:
//!
//! ```text
//! job = kk2_prepare_start(glyphs, n, upm, threads)     // Phase 1/3: analyzing SDFs
//! poll kk2_job_poll(job, &progress) until state != RUNNING
//! ctx = kk2_job_take_context(job); kk2_job_free(job)
//! job = kk2_solve_start(ctx, &params, mask, n)         // Phases 2/3 and 3/3
//! … poll …
//! res = kk2_job_take_result(job); kk2_job_free(job)
//! read res->metrics / res->entries in place; kk2_result_value(res, a, b)
//! kk2_result_free(res); kk2_context_free(ctx)
//! ```
//!
//! Nothing ever calls back into the caller: progress is a handful of atomics
//! read by `kk2_job_poll`, so a UI can poll from a timer without blocking.
//! `kk2_job_cancel` stops a job within one pair's time. `kk2_job_free` never
//! blocks either: a running job is cancelled and cleans up after itself. A
//! context is shared (reference counted) by the jobs that use it, so it may
//! be freed while a solve is still running.
//!
//! Results are compact arrays the caller reads in place (no per-pair
//! objects): metrics per glyph and kerning entries — class pairs, glyph–class
//! and class–glyph exceptions, glyph pairs. `kk2_result_value` resolves one
//! glyph pair with the usual precedence (glyph–glyph, glyph–class,
//! class–glyph, class–class).
//!
//! Every export catches panics; failures return NULL / 0 and leave a message
//! for the calling thread in `kk2_last_error` (jobs keep theirs in
//! `kk2_job_error`).

// Negated comparisons such as `!(x > 0.0)` are deliberate: they reject NaN too.
#![allow(clippy::neg_cmp_op_on_partial_ord)]
#![allow(clippy::missing_safety_doc)]

pub mod api;
mod classes;
pub mod clock;
mod dmat;
mod engine;
mod geometry;
mod job;
mod measure;
mod pass2;
mod physics;
mod profile;
mod run;

use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::{c_char, CString};
use std::mem::size_of;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::ptr::null_mut;
use std::sync::atomic::Ordering::Relaxed;
use std::sync::{Arc, Mutex};

use engine::{Context, GlyphInput, GlyphOpt, SideRule, SolveOptions, MAX_GROUPS, NONE};
use geometry::{Vec2, NODE_LINE, NODE_QCURVE};
use job::{Job, JobError, STATE_FAILED};
use pass2::Solver;
use profile::Side;
use run::{Mode, Outcome, Params, KIND_CLASS_CLASS, KIND_CLASS_GLYPH, KIND_GLYPH_CLASS, KIND_GLYPH_GLYPH};

/// Bumped whenever a `#[repr(C)]` layout below changes.
pub const ABI_VERSION: u32 = 1;

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct KK2Point {
    pub x: f64,
    pub y: f64,
    /// 0 line, 1 curve, 2 off-curve, 3 qcurve.
    pub kind: u32,
    pub reserved: u32,
}

/// Sidebearing rules (`KK2Glyph.lsb_rule` / `rsb_rule`).
pub const RULE_FREE: u32 = 0;
/// Keep `*_value` (or the current sidebearing if it is NaN).
pub const RULE_FIXED: u32 = 1;
/// Follow glyph `*_glyph`'s same side, plus `*_value`.
pub const RULE_FOLLOW_SAME: u32 = 2;
/// Follow glyph `*_glyph`'s opposite side, plus `*_value`.
pub const RULE_FOLLOW_OPPOSITE: u32 = 3;

/// One glyph. Flags: 1 fixed advance (tabular), 2 kern (takes part in
/// kerning), 4 right-to-left, 8 key glyph of its left group, 16 key glyph of
/// its right group. `script`: ISO 15924 as four ASCII bytes (0 = Common).
/// Groups are caller ids (NONE = 0xFFFFFFFF); `base` is the glyph index of a
/// composite's first component.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct KK2Glyph {
    pub points: *const KK2Point,
    pub contour_lengths: *const u32,
    pub point_count: u32,
    pub contour_count: u32,
    pub advance: f64,
    pub rhythm_group: u32,
    pub flags: u32,
    pub script: u32,
    pub base: u32,
    pub left_group: u32,
    pub right_group: u32,
    pub lsb_rule: u32,
    pub rsb_rule: u32,
    pub lsb_glyph: u32,
    pub rsb_glyph: u32,
    pub lsb_value: f64,
    pub rsb_value: f64,
    pub cur_lsb: f64,
    pub cur_rsb: f64,
}

/// `KK2Params.flags`.
pub const PARAM_SKIP_PASS2: u32 = 1;
/// Class kerning (else every glyph pair on its own).
pub const PARAM_CLASSES: u32 = 2;
/// Window-first solver (else v1's search from the Pass-1 gap).
pub const PARAM_WINDOW: u32 = 4;
/// Pairs only within one script plus Common/Inherited.
pub const PARAM_SCOPE_SCRIPTS: u32 = 8;
/// With frozen glyphs (`kk2_solve_start2`): first move the Looseness to the
/// frozen glyphs' own tightness, so new glyphs match the spacing that is
/// already there; the solve's Looseness is then an offset from it.
pub const PARAM_FIT_FROZEN: u32 = 16;

/// `KK2GlyphOpt.flags`: keep the glyph's sidebearings, and never kern a pair
/// of two frozen glyphs.
pub const GLYPHOPT_FROZEN: u32 = 1;

/// Per-solve options of one glyph (`kk2_solve_start2`): its spacing group.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct KK2GlyphOpt {
    pub flags: u32,
    pub reserved: u32,
    /// Looseness offset of the glyph's group, slider units (0 = none).
    pub looseness: f64,
    /// Kerning force as a multiple of the solve's intensity (1 = the same;
    /// NaN or negative = 1). A pair kerns with the mean of its glyphs'.
    pub intensity: f64,
}

/// The designer harness of a solve (`kk2_solve_start3`): corrections toward
/// what well-spaced fonts do, applied after the solve, in font units. Set
/// `struct_size = sizeof(KK2Harness)`.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct KK2Harness {
    pub struct_size: u32,
    /// Entries of `sides` (one per glyph; 0 with `sides` NULL).
    pub glyph_count: u32,
    /// [lsb shift, rsb shift] of each glyph; NULL = none.
    pub sides: *const f64,
    pub pairs: *const KK2HarnessPair,
    pub pair_count: u32,
    pub reserved: u32,
}

/// A pair correction of the harness: added to the kerning of glyph pair
/// (left, right).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct KK2HarnessPair {
    pub left: u32,
    pub right: u32,
    pub value: f64,
}

/// One entry of the font's current kerning (`kk2_measure`): `kind` as the
/// result entries; class sides are the caller's group ids (`KK2Glyph`
/// `right_group` on the left of a pair, `left_group` on the right).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct KK2KernIn {
    pub kind: u32,
    pub left: u32,
    pub right: u32,
    pub value: f32,
}

/// One measured pair (`kk2_measure`): the visible gap now and the model's,
/// and their difference after removing the font's overall offset.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct KK2PairOut {
    pub left: u32,
    pub right: u32,
    pub current: f32,
    pub model: f32,
    pub residual: f32,
    pub reserved: u32,
}

/// Summary of `kk2_measure`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct KK2MeasureStats {
    pub pairs: u64,
    /// Mean of current − model gaps (the overall tightness difference).
    pub offset: f64,
    /// Mean absolute residual after removing `offset`.
    pub mae: f64,
    pub rms: f64,
    pub loosest_count: u32,
    pub tightest_count: u32,
}

/// Solve parameters. Set `struct_size = sizeof(KK2Params)`; negative or NaN
/// tuning fields keep their defaults.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct KK2Params {
    pub struct_size: u32,
    pub flags: u32,
    pub spring: f64,
    pub repulsion: f64,
    pub coupling: f64,
    pub white_credit: f64,
    pub depth_ratio: f64,
    pub rhythm_weight: f64,
    pub width_coupling: f64,
    pub min_clearance: f64,
    pub crevice_pressure: f64,
    pub max_negative_kern: f64,
    pub max_positive_kern: f64,
    pub min_kern: f64,
    pub field_cap: f64,
    pub field_decay: f64,
    pub field_core: f64,
    /// Drop results with |value| below this (font units; ≤ 0.5 keeps v1's rounding).
    pub threshold: f64,
    /// Classes: interaction radius as a multiple of the pair's rest gap (default 1).
    pub radius_ratio: f64,
    /// Maximum entries (0 = unlimited).
    pub budget: u32,
    /// Worker threads (0 = all cores but one).
    pub threads: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct KK2Progress {
    /// 0 running, 1 done, 2 failed, 3 cancelled.
    pub state: u32,
    /// 1 analyzing SDFs, 2 evaluating pairs, 3 grouping & pruning.
    pub phase: u32,
    pub phases: u32,
    pub reserved: u32,
    pub done: u64,
    pub total: u64,
    pub elapsed: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct KK2Metrics {
    pub lsb: f64,
    pub rsb: f64,
    pub advance: f64,
    pub x_min: f64,
    pub x_max: f64,
    pub y_min: f64,
    pub y_max: f64,
    pub valid: u32,
    /// 1 LSB follows a rule, 2 RSB follows a rule.
    pub flags: u32,
}

/// Entry kinds.
pub const ENTRY_GLYPH_GLYPH: u32 = KIND_GLYPH_GLYPH as u32;
pub const ENTRY_CLASS_CLASS: u32 = KIND_CLASS_CLASS as u32;
pub const ENTRY_GLYPH_CLASS: u32 = KIND_GLYPH_CLASS as u32;
pub const ENTRY_CLASS_GLYPH: u32 = KIND_CLASS_GLYPH as u32;

/// One kerning entry: `left` is a glyph index (glyph–*) or a right-side class
/// index (class–*), `right` a glyph index or a left-side class index.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct KK2Entry {
    pub left: u32,
    pub right: u32,
    pub value: f32,
    pub importance: f32,
    pub kind: u32,
    pub reserved: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct KK2Stats {
    pub prep_ms: f64,
    pub pass1_ms: f64,
    pub pass2_ms: f64,
    pub prune_ms: f64,
    pub pass1_residual: f64,
    pub pass1_iterations: u32,
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
    pub rest_gap: [f64; 8],
    pub rhythm_scale: [f64; 8],
}

/// A solve's output; every array has the stated length and lives until
/// `kk2_result_free`. Class origins: bit 31 set = the caller's group id in the
/// low bits; otherwise the glyph whose name names a new group.
#[repr(C)]
#[derive(Debug)]
pub struct KK2Result {
    pub abi_version: u32,
    pub glyph_count: u32,
    pub entry_count: u32,
    /// 0 glyph pairs, 1 classes.
    pub mode: u32,
    pub right_class_count: u32,
    pub left_class_count: u32,
    pub reserved0: u32,
    pub reserved1: u32,
    pub metrics: *const KK2Metrics,
    pub entries: *const KK2Entry,
    pub glyph_right_class: *const u32,
    pub glyph_left_class: *const u32,
    pub right_class_origin: *const u32,
    pub left_class_origin: *const u32,
    pub right_class_rep: *const u32,
    pub left_class_rep: *const u32,
    /// 1 where the glyph was kerned in this solve.
    pub kern_mask: *const u8,
    pub reserved_ptr: *const u8,
    pub stats: KK2Stats,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct KK2Ray {
    pub y: f64,
    pub x: f64,
    pub tier: u32,
    pub reserved: u32,
}

// the plugin's 64-bit ABI (the WebAssembly build of Spacing QA, with 32-bit
// pointers, does not use the C functions)
#[cfg(target_pointer_width = "64")]
const _: () = {
    assert!(size_of::<KK2Point>() == 24);
    assert!(size_of::<KK2Glyph>() == 104);
    assert!(size_of::<KK2Params>() == 152);
    assert!(size_of::<KK2Progress>() == 40);
    assert!(size_of::<KK2Metrics>() == 64);
    assert!(size_of::<KK2Entry>() == 24);
    assert!(size_of::<KK2Stats>() == 352);
    assert!(size_of::<KK2Result>() == 464);
    assert!(size_of::<KK2Ray>() == 24);
    assert!(size_of::<KK2GlyphOpt>() == 24);
    assert!(size_of::<KK2Harness>() == 32);
    assert!(size_of::<KK2HarnessPair>() == 16);
    assert!(size_of::<KK2KernIn>() == 16);
    assert!(size_of::<KK2PairOut>() == 24);
    assert!(size_of::<KK2MeasureStats>() == 40);
};

/// Owns a result's arrays; `c` points into them. `c` is the first field so
/// the pointer handed out is also the box's address.
#[repr(C)]
struct ResultBox {
    c: KK2Result,
    metrics: Vec<KK2Metrics>,
    entries: Vec<KK2Entry>,
    glyph_right_class: Vec<u32>,
    glyph_left_class: Vec<u32>,
    right_origin: Vec<u32>,
    left_origin: Vec<u32>,
    right_rep: Vec<u32>,
    left_rep: Vec<u32>,
    kern: Vec<u8>,
    lookup: Lookup,
    fitted: f64,
}

// The raw pointers in `c` point into the box's own vectors, whose heap
// buffers do not move with the box: handing the box to another thread is safe.
unsafe impl Send for ResultBox {}

#[derive(Default)]
struct Lookup {
    classes: bool,
    gg: HashMap<(u32, u32), f32>,
    gc: HashMap<(u32, u32), f32>,
    cg: HashMap<(u32, u32), f32>,
    cc: HashMap<(u32, u32), f32>,
}

/// Opaque prepared geometry, shared by the jobs that use it.
pub struct KK2Context(Arc<Context>);

enum JobKind {
    Prepare(Job<Arc<Context>>),
    Solve(Job<Box<ResultBox>>),
}

/// Opaque job handle.
pub struct KK2Job {
    kind: JobKind,
    error: Mutex<CString>,
}

thread_local! {
    static LAST_ERROR: RefCell<CString> = RefCell::new(CString::default());
}

fn set_error(msg: String) {
    let c = CString::new(msg.replace('\0', " ")).unwrap_or_default();
    LAST_ERROR.with(|e| *e.borrow_mut() = c);
}

/// Runs `f`, converting both errors and panics into `fallback` + last error.
fn guard<T>(fallback: T, f: impl FnOnce() -> Result<T, String>) -> T {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(Ok(v)) => v,
        Ok(Err(msg)) => {
            set_error(msg);
            fallback
        }
        Err(panic) => {
            let msg = panic
                .downcast_ref::<&str>()
                .map(|s| s.to_string())
                .or_else(|| panic.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "unknown panic".into());
            set_error(format!("internal error in Kinetikern2 engine: {msg}"));
            fallback
        }
    }
}

unsafe fn slice<'a, T>(p: *const T, n: u32) -> &'a [T] {
    if n == 0 || p.is_null() {
        &[]
    } else {
        std::slice::from_raw_parts(p, n as usize)
    }
}

fn rule(kind: u32, glyph: u32, value: f64) -> SideRule {
    match kind {
        RULE_FIXED => SideRule::Fixed(value),
        RULE_FOLLOW_SAME | RULE_FOLLOW_OPPOSITE if glyph != NONE => SideRule::Follow {
            glyph,
            opposite: kind == RULE_FOLLOW_OPPOSITE,
            offset: if value.is_finite() { value } else { 0.0 },
        },
        _ => SideRule::Free,
    }
}

unsafe fn read_inputs(glyphs: *const KK2Glyph, count: u32) -> Result<Vec<GlyphInput>, String> {
    if count > 0 && glyphs.is_null() {
        return Err("glyphs is NULL".into());
    }
    let mut out = Vec::with_capacity(count as usize);
    for (gi, g) in slice(glyphs, count).iter().enumerate() {
        let mut contours = Vec::new();
        if g.contour_count > 0 && g.point_count > 0 {
            if g.points.is_null() || g.contour_lengths.is_null() {
                return Err(format!("glyph {gi}: NULL outline buffers"));
            }
            let pts = slice(g.points, g.point_count);
            let lens = slice(g.contour_lengths, g.contour_count);
            let total: u64 = lens.iter().map(|&l| l as u64).sum();
            if total > g.point_count as u64 {
                return Err(format!("glyph {gi}: contour lengths add up to {total} > {} points", g.point_count));
            }
            let mut k = 0usize;
            let mut finite = true;
            for &len in lens {
                let c: Vec<(Vec2, u32)> = pts[k..k + len as usize]
                    .iter()
                    .map(|p| (Vec2::new(p.x, p.y), if p.kind <= NODE_QCURVE { p.kind } else { NODE_LINE }))
                    .collect();
                finite &= c.iter().all(|(v, _)| v.is_finite());
                contours.push(c);
                k += len as usize;
            }
            if !finite {
                contours.clear(); // treated as an empty glyph
            }
        }
        let base = if g.base < count { g.base } else { NONE };
        out.push(GlyphInput {
            contours,
            advance: g.advance,
            group: g.rhythm_group,
            flags: g.flags,
            script: g.script,
            left_group: g.left_group,
            right_group: g.right_group,
            base,
            lsb_rule: rule(g.lsb_rule, if g.lsb_glyph < count { g.lsb_glyph } else { NONE }, g.lsb_value),
            rsb_rule: rule(g.rsb_rule, if g.rsb_glyph < count { g.rsb_glyph } else { NONE }, g.rsb_value),
            cur_lsb: g.cur_lsb,
            cur_rsb: g.cur_rsb,
        });
    }
    Ok(out)
}

unsafe fn read_params(upm: f64, p: *const KK2Params) -> Result<Params, String> {
    if p.is_null() {
        return Err("params is NULL".into());
    }
    let c = &*p;
    if (c.struct_size as usize) < size_of::<KK2Params>() {
        return Err(format!("params.struct_size {} < {}", c.struct_size, size_of::<KK2Params>()));
    }
    let mut o = SolveOptions::defaults(upm);
    let pick = |v: f64, d: f64| if v.is_finite() && v >= 0.0 { v } else { d };
    if c.spring.is_finite() && c.spring > 0.0 {
        o.spring = c.spring;
    }
    o.repulsion = pick(c.repulsion, o.repulsion);
    o.coupling = pick(c.coupling, o.coupling);
    o.white_credit = pick(c.white_credit, o.white_credit);
    o.depth_ratio = pick(c.depth_ratio, o.depth_ratio);
    o.rhythm = pick(c.rhythm_weight, o.rhythm);
    o.width_coupling = pick(c.width_coupling, o.width_coupling);
    o.min_clearance = pick(c.min_clearance, o.min_clearance);
    o.crevice_pressure = pick(c.crevice_pressure, o.crevice_pressure).min(1.0);
    o.max_negative_kern = pick(c.max_negative_kern, o.max_negative_kern);
    o.max_positive_kern = pick(c.max_positive_kern, o.max_positive_kern);
    o.min_kern = pick(c.min_kern, o.min_kern);
    o.field_cap = pick(c.field_cap, o.field_cap);
    o.field_decay = pick(c.field_decay, o.field_decay);
    o.field_core = pick(c.field_core, o.field_core);
    if c.flags & PARAM_SKIP_PASS2 != 0 {
        o.coupling = 0.0;
    }
    Ok(Params {
        options: o,
        mode: if c.flags & PARAM_CLASSES != 0 { Mode::Classes } else { Mode::Pairs },
        solver: if c.flags & PARAM_WINDOW != 0 { Solver::Window } else { Solver::Reference },
        scope_scripts: c.flags & PARAM_SCOPE_SCRIPTS != 0,
        threshold: if c.threshold.is_finite() { c.threshold } else { 0.5 },
        budget: c.budget as usize,
        radius_ratio: pick(c.radius_ratio, 1.0),
        threads: c.threads as usize,
        glyph_opts: None,
        fit_frozen: c.flags & PARAM_FIT_FROZEN != 0,
        harness: None,
    })
}

fn build_result(ctx: &Context, out: Outcome) -> Box<ResultBox> {
    let p1 = &out.pass1;
    let metrics: Vec<KK2Metrics> = p1
        .metrics
        .iter()
        .map(|m| KK2Metrics {
            lsb: m.lsb,
            rsb: m.rsb,
            advance: m.advance,
            x_min: m.bbox[0],
            x_max: m.bbox[1],
            y_min: m.bbox[2],
            y_max: m.bbox[3],
            valid: m.valid as u32,
            flags: m.flags,
        })
        .collect();
    let entries: Vec<KK2Entry> = out
        .entries
        .iter()
        .map(|e| KK2Entry {
            left: e.left,
            right: e.right,
            value: e.value as f32,
            importance: e.importance as f32,
            kind: e.kind as u32,
            reserved: 0,
        })
        .collect();
    let classes = out.mode == Mode::Classes;
    let built = out.classes.as_ref().unwrap_or(&ctx.classes);
    let (rc, lc) = (&built.right, &built.left);
    let mut lookup = Lookup { classes, ..Lookup::default() };
    for e in &entries {
        let map = match e.kind {
            ENTRY_CLASS_CLASS => &mut lookup.cc,
            ENTRY_GLYPH_CLASS => &mut lookup.gc,
            ENTRY_CLASS_GLYPH => &mut lookup.cg,
            _ => &mut lookup.gg,
        };
        map.insert((e.left, e.right), e.value);
    }
    let s = &out.stats;
    let stats = KK2Stats {
        prep_ms: ctx.prep_ms,
        pass1_ms: p1.ms,
        pass2_ms: s.pass2_ms,
        prune_ms: s.prune_ms,
        pass1_residual: p1.residual,
        pass1_iterations: p1.iterations,
        threads: s.threads,
        kern_glyphs: s.kern_glyphs,
        probe_glyphs: s.probe_glyphs,
        pairs_in_scope: s.pairs_in_scope,
        class_pairs: s.class_pairs,
        member_pairs: s.member_pairs,
        inherited: s.inherited,
        verified: s.verified,
        verified_within: s.verified_within,
        solved: s.solved,
        force_evaluations: s.force_evaluations,
        merged_rays: s.merged_rays,
        active_rays: s.active_rays,
        clearance_hits: s.clearance_hits,
        crevice_hits: s.crevice_hits,
        saturated: s.saturated,
        window_hits: s.window_hits,
        bound_hits: s.bound_hits,
        fallbacks: s.fallbacks,
        entries_before_budget: s.entries_before_budget,
        class_entries: s.class_entries,
        exception_entries: s.exception_entries,
        dropped_by_budget: s.dropped_by_budget,
        rest_gap: p1.rest_gap,
        rhythm_scale: p1.rhythm_scale,
    };
    let mut b = Box::new(ResultBox {
        c: KK2Result {
            abi_version: ABI_VERSION,
            glyph_count: metrics.len() as u32,
            entry_count: entries.len() as u32,
            mode: classes as u32,
            right_class_count: rc.len() as u32,
            left_class_count: lc.len() as u32,
            reserved0: 0,
            reserved1: 0,
            metrics: std::ptr::null(),
            entries: std::ptr::null(),
            glyph_right_class: std::ptr::null(),
            glyph_left_class: std::ptr::null(),
            right_class_origin: std::ptr::null(),
            left_class_origin: std::ptr::null(),
            right_class_rep: std::ptr::null(),
            left_class_rep: std::ptr::null(),
            kern_mask: std::ptr::null(),
            reserved_ptr: std::ptr::null(),
            stats,
        },
        metrics,
        entries,
        glyph_right_class: rc.class_of.clone(),
        glyph_left_class: lc.class_of.clone(),
        right_origin: rc.origin.clone(),
        left_origin: lc.origin.clone(),
        right_rep: rc.rep.clone(),
        left_rep: lc.rep.clone(),
        kern: out.kern.iter().map(|&k| k as u8).collect(),
        lookup,
        fitted: out.fitted,
    });
    let ptr = |v: &[u32]| if v.is_empty() { std::ptr::null() } else { v.as_ptr() };
    b.c.metrics = if b.metrics.is_empty() { std::ptr::null() } else { b.metrics.as_ptr() };
    b.c.entries = if b.entries.is_empty() { std::ptr::null() } else { b.entries.as_ptr() };
    b.c.glyph_right_class = ptr(&b.glyph_right_class);
    b.c.glyph_left_class = ptr(&b.glyph_left_class);
    b.c.right_class_origin = ptr(&b.right_origin);
    b.c.left_class_origin = ptr(&b.left_origin);
    b.c.right_class_rep = ptr(&b.right_rep);
    b.c.left_class_rep = ptr(&b.left_rep);
    b.c.kern_mask = if b.kern.is_empty() { std::ptr::null() } else { b.kern.as_ptr() };
    b
}

/// Kerning of glyph pair (a, b) from a result's entries, with the usual
/// precedence; NaN if either glyph was not kerned.
fn lookup_value(l: &Lookup, kern: &[u8], rc: &[u32], lc: &[u32], a: u32, b: u32) -> f32 {
    let n = kern.len() as u32;
    if a >= n || b >= n || kern[a as usize] == 0 || kern[b as usize] == 0 {
        return f32::NAN;
    }
    if let Some(&v) = l.gg.get(&(a, b)) {
        return v;
    }
    if !l.classes {
        return 0.0;
    }
    let (ra, lb) = (rc[a as usize], lc[b as usize]);
    if let Some(&v) = l.gc.get(&(a, lb)) {
        return v;
    }
    if let Some(&v) = l.cg.get(&(ra, b)) {
        return v;
    }
    l.cc.get(&(ra, lb)).copied().unwrap_or(0.0)
}

impl ResultBox {
    fn value(&self, a: u32, b: u32) -> f32 {
        lookup_value(&self.lookup, &self.kern, &self.glyph_right_class, &self.glyph_left_class, a, b)
    }
}

// ------------------------------------------------------------------ exports

#[no_mangle]
pub extern "C" fn kk2_abi_version() -> u32 {
    ABI_VERSION
}

/// Static, NUL-terminated version string.
#[no_mangle]
pub extern "C" fn kk2_engine_version() -> *const c_char {
    concat!("kinetikern2 ", env!("CARGO_PKG_VERSION"), "\0").as_ptr() as *const c_char
}

/// Message of the last failure on the calling thread (empty if none).
#[no_mangle]
pub extern "C" fn kk2_last_error() -> *const c_char {
    LAST_ERROR.with(|e| e.borrow().as_ptr())
}

#[no_mangle]
pub extern "C" fn kk2_cpu_count() -> u32 {
    job::cpu_count() as u32
}

/// Worker threads used for `threads = 0`.
#[no_mangle]
pub extern "C" fn kk2_default_threads() -> u32 {
    job::default_threads() as u32
}

/// Starts the geometry pre-compute (Phase 1/3). The input is copied before
/// the call returns. Returns a job, or NULL (see `kk2_last_error`).
#[no_mangle]
pub unsafe extern "C" fn kk2_prepare_start(
    glyphs: *const KK2Glyph,
    glyph_count: u32,
    units_per_em: f64,
    threads: u32,
) -> *mut KK2Job {
    guard(null_mut(), || {
        let inputs = read_inputs(glyphs, glyph_count)?;
        let job = Job::spawn("kinetikern2-prepare", move |progress| {
            Context::prepare(inputs, units_per_em, threads as usize, progress)
                .map(Arc::new)
                .map_err(JobError::from)
        });
        Ok(Box::into_raw(Box::new(KK2Job { kind: JobKind::Prepare(job), error: Mutex::new(CString::default()) })))
    })
}

/// Starts a solve (Phases 2/3 and 3/3) on a prepared context. `kern_mask`
/// (NULL = every glyph) selects the glyphs to kern; `params` is copied.
#[no_mangle]
pub unsafe extern "C" fn kk2_solve_start(
    ctx: *const KK2Context,
    params: *const KK2Params,
    kern_mask: *const u8,
    mask_len: u32,
) -> *mut KK2Job {
    guard(null_mut(), || {
        if ctx.is_null() {
            return Err("context is NULL".into());
        }
        let ctx = (*ctx).0.clone();
        let p = read_params(ctx.upm, params)?;
        let mask: Option<Vec<u8>> = if kern_mask.is_null() { None } else { Some(slice(kern_mask, mask_len).to_vec()) };
        let job = Job::spawn("kinetikern2-solve", move |progress| {
            let out = run::run(&ctx, &p, mask.as_deref(), progress)?;
            Ok(build_result(&ctx, out))
        });
        Ok(Box::into_raw(Box::new(KK2Job { kind: JobKind::Solve(job), error: Mutex::new(CString::default()) })))
    })
}

/// `kk2_solve_start` with per-glyph options (`opts`, `opt_count` entries,
/// one per glyph; NULL = none): frozen glyphs and section Looseness offsets.
/// With `PARAM_FIT_FROZEN` the Looseness is first fitted to the frozen
/// glyphs (`kk2_result_fitted_looseness`).
#[no_mangle]
pub unsafe extern "C" fn kk2_solve_start2(
    ctx: *const KK2Context,
    params: *const KK2Params,
    kern_mask: *const u8,
    mask_len: u32,
    opts: *const KK2GlyphOpt,
    opt_count: u32,
) -> *mut KK2Job {
    guard(null_mut(), || {
        if ctx.is_null() {
            return Err("context is NULL".into());
        }
        let ctx = (*ctx).0.clone();
        let mut p = read_params(ctx.upm, params)?;
        if !opts.is_null() && opt_count > 0 {
            let v: Vec<GlyphOpt> = slice(opts, opt_count)
                .iter()
                .map(|o| GlyphOpt {
                    frozen: o.flags & GLYPHOPT_FROZEN != 0,
                    looseness: if o.looseness.is_finite() { o.looseness } else { 0.0 },
                    intensity: if o.intensity.is_finite() && o.intensity >= 0.0 { o.intensity } else { 1.0 },
                })
                .collect();
            p.glyph_opts = Some(Arc::new(v));
        }
        let mask: Option<Vec<u8>> = if kern_mask.is_null() { None } else { Some(slice(kern_mask, mask_len).to_vec()) };
        let job = Job::spawn("kinetikern2-solve", move |progress| {
            let out = run::run(&ctx, &p, mask.as_deref(), progress)?;
            Ok(build_result(&ctx, out))
        });
        Ok(Box::into_raw(Box::new(KK2Job { kind: JobKind::Solve(job), error: Mutex::new(CString::default()) })))
    })
}

/// `kk2_solve_start2` with the designer harness (`harness`, NULL = none):
/// glyph sides shifted and pair kerning corrected after the solve, frozen
/// glyphs left as they are.
#[no_mangle]
pub unsafe extern "C" fn kk2_solve_start3(
    ctx: *const KK2Context,
    params: *const KK2Params,
    kern_mask: *const u8,
    mask_len: u32,
    opts: *const KK2GlyphOpt,
    opt_count: u32,
    harness: *const KK2Harness,
) -> *mut KK2Job {
    guard(null_mut(), || {
        if ctx.is_null() {
            return Err("context is NULL".into());
        }
        let ctx = (*ctx).0.clone();
        let mut p = read_params(ctx.upm, params)?;
        if !opts.is_null() && opt_count > 0 {
            let v: Vec<GlyphOpt> = slice(opts, opt_count)
                .iter()
                .map(|o| GlyphOpt {
                    frozen: o.flags & GLYPHOPT_FROZEN != 0,
                    looseness: if o.looseness.is_finite() { o.looseness } else { 0.0 },
                    intensity: if o.intensity.is_finite() && o.intensity >= 0.0 { o.intensity } else { 1.0 },
                })
                .collect();
            p.glyph_opts = Some(Arc::new(v));
        }
        if !harness.is_null() {
            let h = &*harness;
            if (h.struct_size as usize) < size_of::<KK2Harness>() {
                return Err(format!("KK2Harness.struct_size {} < {}", h.struct_size, size_of::<KK2Harness>()));
            }
            let sides: Vec<[f64; 2]> = if h.sides.is_null() || h.glyph_count == 0 {
                Vec::new()
            } else {
                slice(h.sides, h.glyph_count * 2).chunks(2).map(|c| [c[0], c[1]]).collect()
            };
            let pairs: Vec<(u32, u32, f64)> = if h.pairs.is_null() || h.pair_count == 0 {
                Vec::new()
            } else {
                slice(h.pairs, h.pair_count).iter().map(|q| (q.left, q.right, q.value)).collect()
            };
            p.harness = Some(Arc::new(run::Harness { sides, pairs }));
        }
        let mask: Option<Vec<u8>> = if kern_mask.is_null() { None } else { Some(slice(kern_mask, mask_len).to_vec()) };
        let job = Job::spawn("kinetikern2-solve", move |progress| {
            let out = run::run(&ctx, &p, mask.as_deref(), progress)?;
            Ok(build_result(&ctx, out))
        });
        Ok(Box::into_raw(Box::new(KK2Job { kind: JobKind::Solve(job), error: Mutex::new(CString::default()) })))
    })
}

/// Looseness offset (slider units) the solve moved to with
/// `PARAM_FIT_FROZEN`; NaN if it did not fit.
#[no_mangle]
pub unsafe extern "C" fn kk2_result_fitted_looseness(res: *const KK2Result) -> f64 {
    if res.is_null() {
        return f64::NAN;
    }
    (*(res as *const ResultBox)).fitted
}

/// The Looseness offset (slider units, relative to `params`) at which Pass 1
/// gives the glyphs flagged in `which` (`which_len` bytes), on average, the
/// sidebearings they have now. NaN with fewer than three such glyphs.
/// Synchronous: Pass 1 only, a few milliseconds per step.
#[no_mangle]
pub unsafe extern "C" fn kk2_fit_looseness(
    ctx: *const KK2Context,
    params: *const KK2Params,
    which: *const u8,
    which_len: u32,
) -> f64 {
    guard(f64::NAN, || {
        if ctx.is_null() {
            return Err("context is NULL".into());
        }
        let c = &(*ctx).0;
        let p = read_params(c.upm, params)?;
        let w: Vec<bool> = if which.is_null() {
            vec![true; c.glyphs.len()]
        } else {
            let m = slice(which, which_len);
            (0..c.glyphs.len()).map(|i| m.get(i).copied().unwrap_or(0) != 0).collect()
        };
        Ok(c.fit_looseness(&p.options, &w).unwrap_or(f64::NAN))
    })
}

/// Measures the font's spacing as it is against a solve's: for every pair of
/// glyphs flagged in `mask` (NULL = the glyphs the solve kerned) within the
/// pair scope, the visible gap now (current sidebearings + `current`
/// kerning) and the model's. Fills up to `cap` of the loosest and of the
/// tightest pairs relative to the font's own overall tightness (sorted,
/// most extreme first) and `stats`. Returns the number of pairs measured.
#[no_mangle]
pub unsafe extern "C" fn kk2_measure(
    ctx: *const KK2Context,
    res: *const KK2Result,
    current: *const KK2KernIn,
    current_count: u32,
    mask: *const u8,
    mask_len: u32,
    scope_scripts: u32,
    loosest: *mut KK2PairOut,
    tightest: *mut KK2PairOut,
    cap: u32,
    stats: *mut KK2MeasureStats,
) -> u64 {
    guard(0, || {
        if ctx.is_null() || res.is_null() {
            return Err("context or result is NULL".into());
        }
        let c = &(*ctx).0;
        let r = &*(res as *const ResultBox);
        let cur: Vec<measure::KernIn> = slice(current, current_count)
            .iter()
            .map(|k| measure::KernIn { kind: k.kind as u8, left: k.left, right: k.right, value: k.value as f64 })
            .collect();
        let n = c.glyphs.len();
        let m: Vec<bool> = if mask.is_null() {
            r.kern.iter().map(|&k| k != 0).collect()
        } else {
            let mm = slice(mask, mask_len);
            (0..n).map(|i| mm.get(i).copied().unwrap_or(0) != 0).collect()
        };
        let lsb: Vec<f64> = r.metrics.iter().map(|x| x.lsb).collect();
        let rsb: Vec<f64> = r.metrics.iter().map(|x| x.rsb).collect();
        let (look, kern, rcs, lcs) = (&r.lookup, &r.kern[..], &r.glyph_right_class[..], &r.glyph_left_class[..]);
        let model = move |a: u32, b: u32| -> f64 {
            let v = lookup_value(look, kern, rcs, lcs, a, b);
            if v.is_finite() {
                v as f64
            } else {
                0.0
            }
        };
        let out = measure::measure(c, &lsb, &rsb, &model, &cur, &m, scope_scripts != 0, cap as usize);
        let write = |dst: *mut KK2PairOut, src: &[measure::PairOut]| {
            if !dst.is_null() {
                for (k, p) in src.iter().enumerate() {
                    *dst.add(k) = KK2PairOut {
                        left: p.left,
                        right: p.right,
                        current: p.current as f32,
                        model: p.model as f32,
                        residual: p.residual as f32,
                        reserved: 0,
                    };
                }
            }
        };
        write(loosest, &out.loosest);
        write(tightest, &out.tightest);
        if !stats.is_null() {
            *stats = KK2MeasureStats {
                pairs: out.pairs,
                offset: out.offset,
                mae: out.mae,
                rms: out.rms,
                loosest_count: out.loosest.len() as u32,
                tightest_count: out.tightest.len() as u32,
            };
        }
        Ok(out.pairs)
    })
}

/// Bit set of optional features: 1 per-glyph options and the frozen-glyph
/// Looseness fit (`kk2_solve_start2`, `kk2_fit_looseness`), 2 `kk2_measure`,
/// 4 the designer harness (`kk2_solve_start3`).
#[no_mangle]
pub extern "C" fn kk2_features() -> u32 {
    7
}

fn progress_of(job: &KK2Job) -> &job::Progress {
    match &job.kind {
        JobKind::Prepare(j) => &j.shared.progress,
        JobKind::Solve(j) => &j.shared.progress,
    }
}

/// Fills `out` (may be NULL) and returns the job state: 0 running, 1 done,
/// 2 failed, 3 cancelled. Never blocks.
#[no_mangle]
pub unsafe extern "C" fn kk2_job_poll(job: *const KK2Job, out: *mut KK2Progress) -> u32 {
    guard(STATE_FAILED, || {
        if job.is_null() {
            return Err("job is NULL".into());
        }
        let p = progress_of(&*job);
        let state = p.state.load(Relaxed);
        if !out.is_null() {
            *out = KK2Progress {
                state,
                phase: p.phase.load(Relaxed),
                phases: p.phases.load(Relaxed),
                reserved: 0,
                done: p.done.load(Relaxed),
                total: p.total.load(Relaxed),
                elapsed: p.elapsed(),
            };
        }
        Ok(state)
    })
}

/// Asks a running job to stop. Never blocks.
#[no_mangle]
pub unsafe extern "C" fn kk2_job_cancel(job: *const KK2Job) {
    if !job.is_null() {
        progress_of(&*job).cancel();
    }
}

/// Blocks until the job leaves the running state or `timeout_s` passes
/// (≤ 0: no limit). For tools and tests; a UI should poll instead.
#[no_mangle]
pub unsafe extern "C" fn kk2_job_wait(job: *const KK2Job, timeout_s: f64) -> u32 {
    guard(STATE_FAILED, || {
        if job.is_null() {
            return Err("job is NULL".into());
        }
        Ok(match &(*job).kind {
            JobKind::Prepare(j) => j.wait(timeout_s),
            JobKind::Solve(j) => j.wait(timeout_s),
        })
    })
}

/// Error message of a failed job (empty otherwise); valid until the job is freed.
#[no_mangle]
pub unsafe extern "C" fn kk2_job_error(job: *const KK2Job) -> *const c_char {
    if job.is_null() {
        return kk2_last_error();
    }
    let j = &*job;
    let msg = match &j.kind {
        JobKind::Prepare(x) => x.error(),
        JobKind::Solve(x) => x.error(),
    }
    .unwrap_or_default();
    let mut slot = j.error.lock().unwrap_or_else(|e| e.into_inner());
    *slot = CString::new(msg.replace('\0', " ")).unwrap_or_default();
    slot.as_ptr()
}

/// The context of a finished prepare job (once); free with `kk2_context_free`.
#[no_mangle]
pub unsafe extern "C" fn kk2_job_take_context(job: *const KK2Job) -> *mut KK2Context {
    guard(null_mut(), || {
        if job.is_null() {
            return Err("job is NULL".into());
        }
        match &(*job).kind {
            JobKind::Prepare(j) => match j.take() {
                Some(ctx) => Ok(Box::into_raw(Box::new(KK2Context(ctx)))),
                None => Err("the job has no context (still running, failed, cancelled or already taken)".into()),
            },
            JobKind::Solve(_) => Err("not a prepare job".into()),
        }
    })
}

/// The result of a finished solve job (once); free with `kk2_result_free`.
#[no_mangle]
pub unsafe extern "C" fn kk2_job_take_result(job: *const KK2Job) -> *mut KK2Result {
    guard(null_mut(), || {
        if job.is_null() {
            return Err("job is NULL".into());
        }
        match &(*job).kind {
            JobKind::Solve(j) => match j.take() {
                Some(b) => Ok(Box::into_raw(b) as *mut KK2Result),
                None => Err("the job has no result (still running, failed, cancelled or already taken)".into()),
            },
            JobKind::Prepare(_) => Err("not a solve job".into()),
        }
    })
}

/// Releases a job handle. A running job is cancelled and finishes on its own.
#[no_mangle]
pub unsafe extern "C" fn kk2_job_free(job: *mut KK2Job) {
    if !job.is_null() {
        let _ = catch_unwind(AssertUnwindSafe(|| {
            let j = Box::from_raw(job);
            progress_of(&j).cancel();
            drop(j);
        }));
    }
}

#[no_mangle]
pub unsafe extern "C" fn kk2_context_free(ctx: *mut KK2Context) {
    if !ctx.is_null() {
        let _ = catch_unwind(AssertUnwindSafe(|| drop(Box::from_raw(ctx))));
    }
}

#[no_mangle]
pub unsafe extern "C" fn kk2_context_glyph_count(ctx: *const KK2Context) -> u32 {
    if ctx.is_null() {
        0
    } else {
        (&*ctx).0.glyphs.len() as u32
    }
}

#[no_mangle]
pub unsafe extern "C" fn kk2_context_prep_ms(ctx: *const KK2Context) -> f64 {
    if ctx.is_null() {
        0.0
    } else {
        (&*ctx).0.prep_ms
    }
}

/// Copies glyph `glyph`'s adaptive rays of one side (0 left, 1 right) into
/// `out` (capacity `cap`); returns how many there are.
#[no_mangle]
pub unsafe extern "C" fn kk2_context_rays(
    ctx: *const KK2Context,
    glyph: u32,
    side: u32,
    out: *mut KK2Ray,
    cap: u32,
) -> u32 {
    guard(0, || {
        if ctx.is_null() {
            return Err("context is NULL".into());
        }
        let c = &(*ctx).0;
        let g = c.glyphs.get(glyph as usize).ok_or("glyph index out of range")?;
        let p = if side == 0 { &g.left } else { &g.right };
        debug_assert!(matches!(p.side, Side::Left | Side::Right));
        if !out.is_null() {
            for (k, r) in p.rays.iter().take(cap as usize).enumerate() {
                *out.add(k) = KK2Ray { y: r.y, x: r.x, tier: r.tier as u32, reserved: 0 };
            }
        }
        Ok(p.rays.len() as u32)
    })
}

#[no_mangle]
pub unsafe extern "C" fn kk2_result_free(res: *mut KK2Result) {
    if !res.is_null() {
        let _ = catch_unwind(AssertUnwindSafe(|| drop(Box::from_raw(res as *mut ResultBox))));
    }
}

/// Kerning of glyph pair (left, right) with entry precedence; NaN if either
/// glyph was not kerned in this solve.
#[no_mangle]
pub unsafe extern "C" fn kk2_result_value(res: *const KK2Result, left: u32, right: u32) -> f32 {
    if res.is_null() {
        return f32::NAN;
    }
    (*(res as *const ResultBox)).value(left, right)
}

/// `kk2_result_value` for `count` pairs.
#[no_mangle]
pub unsafe extern "C" fn kk2_result_values(
    res: *const KK2Result,
    lefts: *const u32,
    rights: *const u32,
    count: u32,
    out: *mut f32,
) -> u32 {
    if res.is_null() || lefts.is_null() || rights.is_null() || out.is_null() {
        return 0;
    }
    let r = &*(res as *const ResultBox);
    for k in 0..count as usize {
        *out.add(k) = r.value(*lefts.add(k), *rights.add(k));
    }
    count
}

/// Max rhythm groups (for callers sizing per-group arrays).
#[no_mangle]
pub extern "C" fn kk2_max_groups() -> u32 {
    MAX_GROUPS
}
