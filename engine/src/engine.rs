//! Geometry pre-compute (adaptive SDF profiles, DMAT) and Pass 1 (macro
//! rhythm), adapted from Kinetic SDF Kerning v1 with the model unchanged.
//!
//! What is new in Kinetikern2:
//!
//! * `Context::prepare` reports progress and can be cancelled;
//! * every glyph carries what the pair pipeline needs: script and kerning
//!   eligibility (pair scope), existing kerning groups and composite base
//!   (class kerning), ink y-ranges and tip protrusions (exact pair culling and
//!   floor bounds);
//! * sidebearings can be constrained per side (metrics keys, auto-aligned
//!   composites): Pass 2 then works with the sidebearings the font will
//!   really have after Apply, not with ones that are never written.
//!
//! Spacing works on *zones*: the core band a group's letters share (baseline
//! to x-height for lowercase, to cap height for capitals), found as the median
//! extents of the group. Margin white is measured inside the zone from the
//! zone's extreme ink, and Pass 1 solves for zone sidebearings, so a j's
//! descender hook or an f's ascender hook may overhang the advance width the
//! way designers draw them. Pass 2 always sees the whole outline.

use crate::clock::Instant;

use rayon::prelude::*;

use crate::dmat::{facing_series, pack_inner, pack_outer, Disk, DmatConfig, InnerWhite, SideWhite};
use crate::geometry::{BBox, Outline, Vec2};
use crate::job::{pool, Cancelled, Progress};
use crate::physics::{rest_gap, Pass1Params, SpacingBody, TripletSystem};
use crate::profile::{RayPlan, SdfProfile, Side};

/// Rhythm group ids are 0..MAX_GROUPS (0 = other/punctuation, 1 = uppercase,
/// 2 = lowercase, 3 = figures, 4 = small caps, 5..7 = free).
pub const MAX_GROUPS: u32 = 8;
pub const GROUP_OTHER: u32 = 0;
pub const GROUP_UPPERCASE: u32 = 1;
pub const GROUP_LOWERCASE: u32 = 2;

/// Glyph flags (input).
pub const GLYPH_FIXED_ADVANCE: u32 = 1;
/// The glyph takes part in kerning (letters, figures, punctuation and the
/// symbols that sit in text; not box drawing, arrows, math operators…).
pub const GLYPH_KERN: u32 = 2;
/// Right-to-left script: never kerned in left-to-right order.
pub const GLYPH_RTL: u32 = 4;
/// A base letter (a–z, A–Z, a Cyrillic or Greek letter without a mark…):
/// its extents set its group's spacing zone. Accented and other derived
/// letters reach above or below and outnumber the base letters in most
/// fonts; without such glyphs every member of the group counts (feature 16).
pub const GLYPH_ZONE: u32 = 32;
/// One of the default figures 0–9: the decoration test reads these (a font
/// whose glyphs carry none: every glyph of `GROUP_FIGURES`), so tabular,
/// old-style and superior variants do not change its share.
pub const GLYPH_FIGURE: u32 = 64;

/// "No index" for optional glyph and group references.
pub const NONE: u32 = u32::MAX;

/// The contour-field coupling the kerning intensity scales: 100 % (β = 1) is
/// 0.8 of the raw field, the value that matched designers best on the Google
/// Fonts benchmark (v1 tools/benchmark.py).
pub const COUPLING_CALIBRATION: f64 = 0.8;

/// The Looseness slider (−1 tight … +1 loose, 0 = the tuned default) moves
/// the spring and the bounding repulsion in opposite directions by
/// e^{∓GAIN·t}; at 0 their ratio is `LOOSENESS_RATIO`, the median best fit to
/// the 30 most popular Google Fonts families. The plugin's sliders and every
/// tool use this one mapping.
pub const LOOSENESS_GAIN: f64 = 0.55;
pub const LOOSENESS_RATIO: f64 = 3.86;

/// (spring, repulsion) for a Looseness slider position.
pub fn physics_for_looseness(t: f64) -> (f64, f64) {
    ((-LOOSENESS_GAIN * t).exp(), LOOSENESS_RATIO * (LOOSENESS_GAIN * t).exp())
}

/// Per-solve options of one glyph: the spacing group it was painted into.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct GlyphOpt {
    /// Keep the glyph's current sidebearings, and never kern a pair of two
    /// frozen glyphs: only the rest of the font is spaced, around it.
    pub frozen: bool,
    /// Looseness offset of the glyph's group, in slider units (0 = the
    /// solve's own): figures, fractions or a script spaced looser or tighter
    /// than the rest of the font.
    pub looseness: f64,
    /// Kerning force of the glyph's group as a multiple of the solve's
    /// intensity (1 = the same). A pair kerns with the mean of its two glyphs'.
    pub intensity: f64,
}

impl Default for GlyphOpt {
    fn default() -> Self {
        GlyphOpt { frozen: false, looseness: 0.0, intensity: 1.0 }
    }
}

/// How one sidebearing is decided.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum SideRule {
    /// Pass 1 decides.
    Free,
    /// Kept as it is (a metrics key the engine cannot evaluate).
    Fixed(f64),
    /// Follows another glyph's new sidebearing: `=H` (same side), `=|H`
    /// (opposite side), auto-aligned composites (same side as the base), plus
    /// an offset.
    Follow { glyph: u32, opposite: bool, offset: f64 },
}

pub struct GlyphInput {
    pub contours: Vec<Vec<(Vec2, u32)>>,
    pub advance: f64,
    pub group: u32,
    pub flags: u32,
    /// ISO 15924 code packed as four ASCII bytes (big-endian); 0 = Common /
    /// Inherited / unknown (kerns with every script).
    pub script: u32,
    /// Existing kerning groups: `left_group` is the glyph's leftKerningGroup
    /// (used when the glyph stands on the right of a pair, @MMK_R_),
    /// `right_group` its rightKerningGroup (@MMK_L_). Ids are the caller's.
    pub left_group: u32,
    pub right_group: u32,
    /// Index of the glyph's first component's glyph (composites), else NONE.
    pub base: u32,
    pub lsb_rule: SideRule,
    pub rsb_rule: SideRule,
    /// The sidebearings the glyph has now (fallbacks for broken rules).
    pub cur_lsb: f64,
    pub cur_rsb: f64,
    /// A connected script's joins: the heights (font units) where this side's
    /// stroke reaches into the neighbour. Pass 1 spaces the side's body
    /// without it (the stroke overhangs, like the hook of a j), and a pair
    /// whose facing sides both join gets no kerning floors: the joins overlap
    /// as drawn. None: no join (every other glyph).
    pub join_left: Option<(f64, f64)>,
    pub join_right: Option<(f64, f64)>,
}

impl GlyphInput {
    pub fn simple(contours: Vec<Vec<(Vec2, u32)>>, advance: f64, group: u32) -> Self {
        GlyphInput {
            contours,
            advance,
            group,
            flags: GLYPH_KERN,
            script: 0,
            left_group: NONE,
            right_group: NONE,
            base: NONE,
            lsb_rule: SideRule::Free,
            rsb_rule: SideRule::Free,
            cur_lsb: f64::NAN,
            cur_rsb: f64::NAN,
            join_left: None,
            join_right: None,
        }
    }
}

pub struct PreparedGlyph {
    pub valid: bool,
    pub fixed_advance: bool,
    pub advance: f64,
    pub group_id: u32,
    /// Dense group index into `Context::rhythm_scale`.
    pub group: usize,
    pub bbox: BBox,
    pub left: SdfProfile,
    pub right: SdfProfile,
    pub left_pieces: Vec<(Vec2, Vec2)>,
    pub right_pieces: Vec<(Vec2, Vec2)>,
    /// Spacing zone (heights) and the extreme ink x inside it.
    pub zone: (f64, f64),
    pub zone_left: f64,
    pub zone_right: f64,
    /// Margin white inside the zone (Pass 1).
    pub white_left: SideWhite,
    pub white_right: SideWhite,
    /// Crevice and tip micro-disk series of the whole facing profiles (Pass 2).
    pub left_crevices: Vec<Disk>,
    pub left_tips: Vec<Disk>,
    pub right_crevices: Vec<Disk>,
    pub right_tips: Vec<Disk>,
    pub crevice_count: u32,
    pub inner: InnerWhite,
    /// Inner white volume / height: the glyph's mean counter width.
    pub counter_width: f64,
    // ---- Kinetikern2 additions
    pub flags: u32,
    pub script: u32,
    pub left_group_in: u32,
    pub right_group_in: u32,
    pub base: u32,
    pub lsb_rule: SideRule,
    pub rsb_rule: SideRule,
    pub cur_lsb: f64,
    pub cur_rsb: f64,
    /// Lowest and highest ink height of each side's profile.
    pub ink_left: (f64, f64),
    pub ink_right: (f64, f64),
    /// How far each side's tip micro-disks reach past the bbox edge (≥ 0).
    pub tip_left: f64,
    pub tip_right: f64,
    /// Rays a fixed 10-unit comb would need for each side (statistics).
    pub comb_left: u32,
    pub comb_right: u32,
    /// Join bands (GlyphInput::join_left/right) and each joining side's body:
    /// the profile with the band left out.
    pub join_left: Option<(f64, f64)>,
    pub join_right: Option<(f64, f64)>,
    pub left_body: Option<SdfProfile>,
    pub right_body: Option<SdfProfile>,
    /// Keep joins: the side keeps its sidebearing, and a pair of two kept
    /// sides the font's kerning (crate::checker).
    pub kept_left: bool,
    pub kept_right: bool,
}

impl PreparedGlyph {
    fn zone_height(&self) -> f64 {
        (self.zone.1 - self.zone.0).max(1e-6)
    }
    /// The profile Pass 1 spaces on each side: the body of a joining side.
    pub fn spaced_left(&self) -> &SdfProfile {
        self.left_body.as_ref().unwrap_or(&self.left)
    }
    pub fn spaced_right(&self) -> &SdfProfile {
        self.right_body.as_ref().unwrap_or(&self.right)
    }
    /// The pair (self, b) joins: self's right side and b's left side both have joins.
    pub fn joins(&self, b: &PreparedGlyph) -> bool {
        self.join_right.is_some() && b.join_left.is_some()
    }
    /// Keep joins: the pair (self, b) keeps the font's kerning.
    pub fn keeps(&self, b: &PreparedGlyph) -> bool {
        self.kept_right && b.kept_left
    }
    /// The side joins (a band, or kept): it never shares a kerning class
    /// with a side that does not.
    pub fn join_side(&self, right: bool) -> bool {
        if right {
            self.join_right.is_some() || self.kept_right
        } else {
            self.join_left.is_some() || self.kept_left
        }
    }
    /// How far the bbox reaches past the zone's extreme ink on each side.
    fn overhang(&self) -> (f64, f64) {
        (self.zone_left - self.bbox.x0, self.bbox.x1 - self.zone_right)
    }
    pub fn kernable(&self) -> bool {
        self.valid && self.flags & GLYPH_KERN != 0
    }
}

pub struct Context {
    pub upm: f64,
    pub glyphs: Vec<PreparedGlyph>,
    /// Group id of every dense group index.
    pub group_ids: Vec<u32>,
    /// Rhythm scale λ per dense group.
    pub rhythm_scale: Vec<f64>,
    /// Depth the margin white was packed to.
    pub depth_pack: f64,
    pub prep_ms: f64,
    /// Kerning classes (physics-independent, built once).
    pub classes: crate::classes::Classes,
    /// A connected script's letters for the join checker, and whether its
    /// joins are kept (`prepare_with_joins`); None otherwise.
    pub joins: Option<crate::checker::FontJoins>,
}

#[derive(Clone, Debug)]
pub struct SolveOptions {
    /// k_s — Pass-1 spring tension.
    pub spring: f64,
    /// k_b — Pass-1 bounding repulsion.
    pub repulsion: f64,
    /// β — Pass-2 contour-field coupling (kerning intensity); 0 disables Pass 2.
    pub coupling: f64,
    pub white_credit: f64,
    pub depth_ratio: f64,
    pub rhythm: f64,
    pub width_coupling: f64,
    pub min_clearance: f64,
    pub crevice_pressure: f64,
    pub max_negative_kern: f64,
    pub max_positive_kern: f64,
    /// Dead zone of Pass 2: interactions are shrunk toward zero by this much.
    pub min_kern: f64,
    /// Contour field shape, relative to the pair's rest gap.
    pub field_cap: f64,
    pub field_decay: f64,
    pub field_core: f64,
}

impl SolveOptions {
    /// The same options with the Looseness moved by `dt` slider units.
    pub fn shifted(&self, dt: f64) -> SolveOptions {
        let mut o = self.clone();
        if dt.is_finite() && dt != 0.0 {
            o.spring *= (-LOOSENESS_GAIN * dt).exp();
            o.repulsion *= (LOOSENESS_GAIN * dt).exp();
        }
        o
    }

    /// Tuned on the 30 most popular Google Fonts families, selected on the next
    /// 13 and tested on 30 macOS system families (v1 tools/benchmark.py).
    pub fn defaults(upm: f64) -> Self {
        SolveOptions {
            spring: 1.0,
            repulsion: 3.86,
            coupling: 1.0,
            white_credit: 0.8,
            depth_ratio: 0.8,
            rhythm: 0.2,
            width_coupling: 0.0,
            min_clearance: 0.01 * upm,
            crevice_pressure: 0.2,
            max_negative_kern: 0.10 * upm,
            max_positive_kern: 0.06 * upm,
            min_kern: 0.015 * upm,
            field_cap: 4.0,
            field_decay: 0.2,
            field_core: 0.3,
        }
    }
}

/// `GlyphMetrics.flags`: the side follows a rule instead of Pass 1.
pub const METRIC_LSB_RULED: u32 = 1;
pub const METRIC_RSB_RULED: u32 = 2;

#[derive(Clone, Copy, Debug, Default)]
pub struct GlyphMetrics {
    pub lsb: f64,
    pub rsb: f64,
    pub advance: f64,
    pub bbox: [f64; 4],
    pub optical_left: f64,
    pub optical_right: f64,
    pub valid: bool,
    pub flags: u32,
}

/// Pass 1 over every valid glyph.
#[derive(Clone)]
pub struct Pass1 {
    pub lsb: Vec<f64>,
    pub rsb: Vec<f64>,
    /// The sidebearings Pass 1 wanted before rules, frozen glyphs and kept
    /// joins (the drawing advice for a kept side: what the model would give it).
    pub wanted_lsb: Vec<f64>,
    pub wanted_rsb: Vec<f64>,
    pub metrics: Vec<GlyphMetrics>,
    pub iterations: u32,
    pub residual: f64,
    pub ms: f64,
    pub rest_gap: [f64; MAX_GROUPS as usize],
    pub rhythm_scale: [f64; MAX_GROUPS as usize],
}

/// Pass 1's own result before section offsets and rules.
struct FreeSpacing {
    zone_sb: Vec<(f64, f64)>,
    omegas: Vec<(f64, f64)>,
    white_credit: f64,
    lsb: Vec<f64>,
    rsb: Vec<f64>,
    iterations: u32,
    residual: f64,
}

fn median(mut v: Vec<f64>) -> Option<f64> {
    if v.is_empty() {
        return None;
    }
    v.sort_by(f64::total_cmp);
    let m = v.len() / 2;
    Some(if v.len() % 2 == 1 { v[m] } else { 0.5 * (v[m - 1] + v[m]) })
}

/// Extreme ink x of a profile between the heights `z`.
fn extreme_within(p: &SdfProfile, z: (f64, f64)) -> f64 {
    let mut best: Option<f64> = None;
    let mut take = |x: f64| best = Some(best.map_or(x, |b| p.side.pick(b.min(x), b.max(x))));
    for r in p.rays.iter().filter(|r| r.has_ink() && r.y >= z.0 && r.y <= z.1) {
        take(r.x);
    }
    for y in [z.0, z.1] {
        if let Some(x) = p.eval(y) {
            take(x);
        }
    }
    best.unwrap_or(p.extreme)
}

fn ink_range(p: &SdfProfile) -> (f64, f64) {
    let mut lo = f64::INFINITY;
    let mut hi = f64::NEG_INFINITY;
    for r in p.rays.iter().filter(|r| r.has_ink()) {
        lo = lo.min(r.y);
        hi = hi.max(r.y);
    }
    (lo, hi)
}

/// How far tip disks reach past the profile's bbox edge.
fn tip_protrusion(tips: &[Disk], side: Side, edge: f64) -> f64 {
    tips.iter()
        .map(|t| match side {
            Side::Left => edge - (t.c.x - t.r),
            Side::Right => (t.c.x + t.r) - edge,
        })
        .fold(0.0, f64::max)
}

impl Context {
    pub fn prepare(inputs: Vec<GlyphInput>, upm: f64, threads: usize, progress: &Progress) -> Result<Context, Cancelled> {
        Self::prepare_with_joins(inputs, upm, threads, progress, None)
    }

    /// `prepare` for a connected script (`setup`): every glyph with an outline
    /// is measured for the join checker first, and with Keep joins every side with a join
    /// in the font gets a band (crate::checker) and is kept.
    pub fn prepare_with_joins(
        mut inputs: Vec<GlyphInput>,
        upm: f64,
        threads: usize,
        progress: &Progress,
        setup: Option<&crate::checker::JoinSetup>,
    ) -> Result<Context, Cancelled> {
        let t0 = Instant::now();
        let upm = if upm.is_finite() && upm >= 16.0 { upm } else { 1000.0 };
        let s = upm / 1000.0;
        let plan = RayPlan::for_upm(upm);
        let dcfg = DmatConfig::for_upm(upm);
        let n = inputs.len();
        let pool = pool(threads);
        progress.begin(1, 3, (2 + setup.is_some() as u64) * n as u64 + 1);
        let joins = match setup {
            Some(setup) => {
                let fj = pool.install(|| crate::checker::FontJoins::build(&inputs, upm, setup, progress))?;
                if fj.keep {
                    fj.add_bands(&mut inputs, upm);
                }
                Some(fj)
            }
            None => None,
        };
        let keep = joins.as_ref().is_some_and(|j| j.keep);

        // Phase A: outlines, adaptive profiles, counters, facing micro-disk series.
        let mut glyphs: Vec<PreparedGlyph> = pool.install(|| {
            inputs
                .into_par_iter()
                .map(|inp| {
                    if progress.cancelled() {
                        return None;
                    }
                    let g = prepare_glyph(inp, s, &plan, &dcfg, keep);
                    progress.add(1);
                    Some(g)
                })
                .collect::<Option<Vec<_>>>()
        })
        .ok_or(Cancelled)?;

        let mut group_ids: Vec<u32> = glyphs.iter().filter(|g| g.valid).map(|g| g.group_id).collect();
        group_ids.sort_unstable();
        group_ids.dedup();
        if group_ids.is_empty() {
            group_ids.push(GROUP_OTHER);
        }
        for g in glyphs.iter_mut() {
            g.group = group_ids.iter().position(|&id| id == g.group_id).unwrap_or(0);
        }

        // Rhythm scale of every group from its median counter width. The scale
        // grows with the square root of the counters (geometric blend with the
        // font-wide median), so capitals space looser than lowercase without
        // doubling. Punctuation and symbols follow the lowercase rhythm.
        let min_counter = 0.03 * upm;
        let counters = |id: Option<u32>| -> Vec<f64> {
            glyphs
                .iter()
                .filter(|g| g.valid && g.counter_width > min_counter && id.map_or(true, |id| g.group_id == id))
                .map(|g| g.counter_width)
                .collect()
        };
        let all = median(counters(None)).unwrap_or(0.25 * upm);
        let of_group = |id: u32| -> Option<f64> {
            let v = counters(Some(id));
            if v.len() >= 2 {
                median(v)
            } else {
                None
            }
        };
        let lower = of_group(GROUP_LOWERCASE);
        let rhythm_scale: Vec<f64> = group_ids
            .iter()
            .map(|&id| {
                let c = if id == GROUP_OTHER { lower.unwrap_or(all) } else { of_group(id).or(lower).unwrap_or(all) };
                (all * c).sqrt()
            })
            .collect();
        let max_scale = rhythm_scale.iter().cloned().fold(0.0, f64::max);
        let depth_pack = (1.25 * max_scale).clamp(0.15 * upm, 0.6 * upm);

        // Spacing zones from the median extents of each group: of its base
        // letters where the caller marks them (GLYPH_ZONE), else of all its
        // members. Accented letters would lift a lowercase zone to accent
        // height, where an f's hook decides its right side.
        let zone_of = |id: u32| -> Option<(f64, f64)> {
            let all: Vec<&PreparedGlyph> = glyphs.iter().filter(|g| g.valid && g.group_id == id).collect();
            let base: Vec<&PreparedGlyph> = all.iter().copied().filter(|g| g.flags & GLYPH_ZONE != 0).collect();
            let members = if base.len() >= 3 { base } else { all };
            if members.len() < 3 {
                return None;
            }
            Some((
                median(members.iter().map(|g| g.bbox.y0).collect())?,
                median(members.iter().map(|g| g.bbox.y1).collect())?,
            ))
        };
        let (lower_zone, upper_zone) = (zone_of(GROUP_LOWERCASE), zone_of(GROUP_UPPERCASE));
        // Punctuation and symbols sit between capitals as much as between
        // lowercase, and tall ones (parentheses, slash, ?, !) are judged by
        // their whole shape: they are spaced over the cap-height band.
        let group_zone: Vec<Option<(f64, f64)>> = group_ids
            .iter()
            .map(|&id| if id == GROUP_OTHER { upper_zone.or(lower_zone) } else { zone_of(id).or(lower_zone).or(upper_zone) })
            .collect();
        for g in glyphs.iter_mut().filter(|g| g.valid) {
            let (y0, y1) = (g.bbox.y0, g.bbox.y1);
            if let Some((z0, z1)) = group_zone[g.group] {
                let (a, b) = (z0.max(y0), z1.min(y1));
                // glyphs that live mostly outside the zone (quotes, superiors) keep their own extent
                if b - a >= 0.5 * (y1 - y0).min(z1 - z0) {
                    g.zone = (a, b);
                }
            }
            g.zone_left = extreme_within(g.spaced_left(), g.zone);
            g.zone_right = extreme_within(g.spaced_right(), g.zone);
        }

        // Phase B: the margin white inside each zone.
        let ok = pool.install(|| {
            glyphs.par_iter_mut().all(|g| {
                if progress.cancelled() {
                    return false;
                }
                if g.valid {
                    g.white_left = pack_outer(g.spaced_left(), g.zone, g.zone_left, depth_pack, &dcfg);
                    g.white_right = pack_outer(g.spaced_right(), g.zone, g.zone_right, depth_pack, &dcfg);
                }
                progress.add(1);
                true
            })
        });
        if !ok {
            return Err(Cancelled);
        }

        let classes = crate::classes::Classes::build(&glyphs, upm);
        progress.add(1);
        Ok(Context {
            upm,
            glyphs,
            group_ids,
            rhythm_scale,
            depth_pack,
            prep_ms: t0.elapsed().as_secs_f64() * 1000.0,
            classes,
            joins,
        })
    }

    fn depth_for(&self, group: usize, o: &SolveOptions) -> f64 {
        (o.depth_ratio.max(0.0) * self.rhythm_scale[group]).min(self.depth_pack)
    }

    /// Margin white of a glyph as equivalent widths (volume / zone height).
    fn omegas(&self, g: &PreparedGlyph, o: &SolveOptions) -> (f64, f64) {
        let d = self.depth_for(g.group, o);
        let h = g.zone_height();
        (g.white_left.volume_within(d) / h, g.white_right.volume_within(d) / h)
    }

    /// Pass-1 rest gap of every dense group pair (the contour fields' g_ref).
    pub fn rest_gap_of(&self, group: usize, o: &SolveOptions) -> f64 {
        rest_gap(self.rhythm_scale[group], o.spring.max(1e-9), o.repulsion.max(0.0))
    }

    /// Pass 1 over every valid glyph, then the per-side rules.
    pub fn pass1(&self, o: &SolveOptions) -> Pass1 {
        self.pass1_with(o, None)
    }

    /// Pass 1 with per-glyph options: section Looseness offsets move a
    /// section's sidebearings by half the change of its rest gap per side (a
    /// gap inside the section changes by the whole difference, a gap to the
    /// rest of the font by half of it); frozen glyphs keep the sidebearings
    /// they have.
    pub fn pass1_with(&self, o: &SolveOptions, opts: Option<&[GlyphOpt]>) -> Pass1 {
        let t1 = Instant::now();
        let spring = o.spring.max(1e-9);
        let repulsion = o.repulsion.max(0.0);
        let mut rest = [f64::NAN; MAX_GROUPS as usize];
        let mut scale = [f64::NAN; MAX_GROUPS as usize];
        for (k, &id) in self.group_ids.iter().enumerate() {
            scale[id as usize] = self.rhythm_scale[k];
            rest[id as usize] = rest_gap(self.rhythm_scale[k], spring, repulsion);
        }
        let free = self.free_sidebearings(o);
        let (zone_sb, omegas, white_credit) = (&free.zone_sb, &free.omegas, free.white_credit);
        let mut lsb = free.lsb.clone();
        let mut rsb = free.rsb.clone();
        if let Some(opts) = opts {
            for (i, g) in self.glyphs.iter().enumerate().filter(|(_, g)| g.valid && !g.fixed_advance) {
                let dt = opts.get(i).map_or(0.0, |x| x.looseness);
                if dt.is_finite() && dt != 0.0 {
                    let shifted = o.shifted(dt);
                    let lam = self.rhythm_scale[g.group];
                    let d = 0.5
                        * (rest_gap(lam, shifted.spring.max(1e-9), shifted.repulsion.max(0.0))
                            - rest_gap(lam, spring, repulsion));
                    lsb[i] += d;
                    rsb[i] += d;
                }
            }
        }
        let (wanted_lsb, wanted_rsb) = (lsb.clone(), rsb.clone());
        let ruled = self.apply_rules(&mut lsb, &mut rsb, opts);

        let metrics: Vec<GlyphMetrics> = self
            .glyphs
            .iter()
            .enumerate()
            .map(|(i, g)| {
                if !g.valid {
                    return GlyphMetrics { advance: g.advance, lsb: f64::NAN, rsb: f64::NAN, ..GlyphMetrics::default() };
                }
                let (lz, rz) = zone_sb[i];
                GlyphMetrics {
                    lsb: lsb[i],
                    rsb: rsb[i],
                    advance: if g.fixed_advance { g.advance } else { lsb[i] + g.bbox.width() + rsb[i] },
                    bbox: [g.bbox.x0, g.bbox.x1, g.bbox.y0, g.bbox.y1],
                    optical_left: lz + white_credit * omegas[i].0,
                    optical_right: rz + white_credit * omegas[i].1,
                    valid: true,
                    flags: ruled[i],
                }
            })
            .collect();
        Pass1 {
            lsb,
            rsb,
            wanted_lsb,
            wanted_rsb,
            metrics,
            iterations: free.iterations,
            residual: free.residual,
            ms: t1.elapsed().as_secs_f64() * 1000.0,
            rest_gap: rest,
            rhythm_scale: scale,
        }
    }

    /// The Looseness offset (slider units, relative to `o`) at which Pass 1
    /// gives the glyphs flagged in `which`, on average, the sidebearings they
    /// have now: the tightness the designer chose, read off the glyphs whose
    /// spacing is final. None with fewer than three such glyphs. Pass 1 only,
    /// a few milliseconds per evaluation.
    pub fn fit_looseness(&self, o: &SolveOptions, which: &[bool]) -> Option<f64> {
        self.fit_looseness_sides(o, which, which)
    }

    /// `fit_looseness` over sides: the left sides flagged in `left` and the
    /// right sides flagged in `right` (kept joins, frozen glyphs). None with
    /// fewer than six such sides.
    pub fn fit_looseness_sides(&self, o: &SolveOptions, left: &[bool], right: &[bool]) -> Option<f64> {
        let ok = |i: usize, flags: &[bool], cur: f64| {
            let g = &self.glyphs[i];
            flags.get(i).copied().unwrap_or(false) && g.valid && !g.fixed_advance && cur.is_finite()
        };
        let sides: Vec<(usize, bool)> = (0..self.glyphs.len())
            .flat_map(|i| [(i, false), (i, true)])
            .filter(|&(i, r)| {
                let g = &self.glyphs[i];
                if r {
                    ok(i, right, g.cur_rsb)
                } else {
                    ok(i, left, g.cur_lsb)
                }
            })
            .collect();
        if sides.len() < 6 {
            return None;
        }
        let cur = |i: usize, r: bool| if r { self.glyphs[i].cur_rsb } else { self.glyphs[i].cur_lsb };
        let target = sides.iter().map(|&(i, r)| cur(i, r)).sum::<f64>() / sides.len() as f64;
        // model minus current, mean over the sides: grows with the looseness
        let f = |dt: f64| -> f64 {
            let free = self.free_sidebearings(&o.shifted(dt));
            sides.iter().map(|&(i, r)| if r { free.rsb[i] } else { free.lsb[i] }).sum::<f64>() / sides.len() as f64
                - target
        };
        // half of 0.05 units: the fit on glyphs stopped at 0.05 on a glyph's two sides
        let tol = 0.025 * self.upm / 1000.0;
        let (mut a, mut fa): (f64, f64) = (0.0, f(0.0));
        if !fa.is_finite() {
            return None;
        }
        if fa.abs() <= tol {
            return Some(0.0);
        }
        // bracket: step away from zero in the direction that shrinks the gap
        let dir = if fa > 0.0 { -1.0 } else { 1.0 };
        let mut b: f64 = a;
        let mut fb: f64 = fa;
        let mut step: f64 = 0.5;
        while fb.signum() == fa.signum() {
            b += dir * step;
            if b.abs() > crate::slant::FIT_LIMIT {
                return Some(b.clamp(-crate::slant::FIT_LIMIT, crate::slant::FIT_LIMIT));
            }
            fb = f(b);
            if !fb.is_finite() {
                return None;
            }
            if fb.signum() == fa.signum() {
                a = b;
                fa = fb;
            }
            step *= 1.5;
        }
        // Illinois on [a, b]
        let mut side = 0i32;
        for _ in 0..40 {
            let c = (a * fb - b * fa) / (fb - fa);
            let fc = f(c);
            if !fc.is_finite() {
                break;
            }
            if fc.abs() <= tol || (b - a).abs() < 1e-4 {
                return Some(c);
            }
            if fc.signum() == fb.signum() {
                b = c;
                fb = fc;
                if side == -1 {
                    fa *= 0.5;
                }
                side = -1;
            } else {
                a = c;
                fa = fc;
                if side == 1 {
                    fb *= 0.5;
                }
                side = 1;
            }
        }
        Some(0.5 * (a + b))
    }

    /// Pass 1's own sidebearings (before section offsets and rules), the zone
    /// sidebearings and margin white they came from.
    fn free_sidebearings(&self, o: &SolveOptions) -> FreeSpacing {
        let upm = self.upm;
        let s = upm / 1000.0;
        let n = self.glyphs.len();
        let spring = o.spring.max(1e-9);
        let repulsion = o.repulsion.max(0.0);

        let free: Vec<usize> = (0..n).filter(|&i| self.glyphs[i].valid && !self.glyphs[i].fixed_advance).collect();
        let omegas: Vec<(f64, f64)> =
            self.glyphs.iter().map(|g| if g.valid { self.omegas(g, o) } else { (0.0, 0.0) }).collect();
        let bodies: Vec<SpacingBody> = free
            .iter()
            .map(|&i| {
                let g = &self.glyphs[i];
                SpacingBody { group: g.group, width: g.zone_right - g.zone_left, omega_l: omegas[i].0, omega_r: omegas[i].1 }
            })
            .collect();
        let p1 = Pass1Params {
            spring,
            repulsion,
            white_credit: o.white_credit,
            rhythm: o.rhythm.max(0.0),
            width_coupling: o.width_coupling,
            wall: 4.0,
            wall_length: 0.01 * upm,
            tolerance: 1e-4 * s,
            max_iterations: 2000,
        };
        let sys = TripletSystem::new(&bodies, &self.rhythm_scale, &p1);
        let (init_l, init_r): (Vec<f64>, Vec<f64>) = bodies
            .iter()
            .map(|b| {
                let g0 = 0.5 * rest_gap(self.rhythm_scale[b.group], spring, repulsion);
                (g0 - p1.white_credit * b.omega_l, g0 - p1.white_credit * b.omega_r)
            })
            .unzip();
        let res = sys.solve(&init_l, &init_r);

        // zone sidebearings of every valid glyph
        let mut zone_sb = vec![(f64::NAN, f64::NAN); n];
        for (k, &i) in free.iter().enumerate() {
            zone_sb[i] = (res.lsb[k], res.rsb[k]);
        }
        for (i, g) in self.glyphs.iter().enumerate().filter(|(_, g)| g.valid && g.fixed_advance) {
            // fixed advance: split the given space so the optical margins balance
            let (ol, or) = g.overhang();
            let space = g.advance - g.bbox.width() + ol + or;
            let rz = 0.5 * (space + o.white_credit * (omegas[i].0 - omegas[i].1));
            zone_sb[i] = (space - rz, rz);
        }
        let mut lsb = vec![f64::NAN; n];
        let mut rsb = vec![f64::NAN; n];
        for (i, g) in self.glyphs.iter().enumerate().filter(|(_, g)| g.valid) {
            let (lz, rz) = zone_sb[i];
            let (ol, or) = g.overhang();
            lsb[i] = lz - ol;
            rsb[i] = rz - or;
        }
        FreeSpacing {
            zone_sb,
            omegas,
            white_credit: p1.white_credit,
            lsb,
            rsb,
            iterations: res.iterations,
            residual: res.residual,
        }
    }

    /// Applies Fixed / Follow rules (chains resolved in up to 8 rounds; a rule
    /// that cannot resolve keeps the glyph's current sidebearing). A frozen
    /// glyph keeps both of its current sidebearings, whatever its rules say.
    fn apply_rules(&self, lsb: &mut [f64], rsb: &mut [f64], opts: Option<&[GlyphOpt]>) -> Vec<u32> {
        let n = self.glyphs.len();
        let mut flags = vec![0u32; n];
        let free_l: Vec<f64> = lsb.to_vec();
        let free_r: Vec<f64> = rsb.to_vec();
        let frozen = |i: usize| opts.and_then(|o| o.get(i)).is_some_and(|x| x.frozen);
        let mut pending: Vec<(usize, bool)> = Vec::new();
        for (i, g) in self.glyphs.iter().enumerate().filter(|(_, g)| g.valid) {
            if frozen(i) {
                if g.cur_lsb.is_finite() && g.cur_rsb.is_finite() {
                    lsb[i] = g.cur_lsb;
                    rsb[i] = g.cur_rsb;
                    flags[i] |= METRIC_LSB_RULED | METRIC_RSB_RULED;
                }
                continue;
            }
            if g.fixed_advance {
                // its advance is kept: a kept join keeps its side as drawn, and
                // so the other side too (the two add up to the advance) —
                // tabular figures in a design whose glyphs touch, a letter
                // whose width follows another's in a script
                if (g.kept_left || g.kept_right) && g.cur_lsb.is_finite() && g.cur_rsb.is_finite() {
                    lsb[i] = g.cur_lsb;
                    rsb[i] = g.cur_rsb;
                    flags[i] |= METRIC_LSB_RULED | METRIC_RSB_RULED;
                }
                continue;
            }
            for (left, rule, cur, kept) in
                [(true, g.lsb_rule, g.cur_lsb, g.kept_left), (false, g.rsb_rule, g.cur_rsb, g.kept_right)]
            {
                // a kept join stays as drawn, whatever rule the side has
                let rule = if kept && cur.is_finite() { SideRule::Fixed(cur) } else { rule };
                match rule {
                    SideRule::Free => {}
                    SideRule::Fixed(v) => {
                        let v = if v.is_finite() { v } else { cur };
                        if v.is_finite() {
                            if left {
                                lsb[i] = v;
                            } else {
                                rsb[i] = v;
                            }
                            flags[i] |= if left { METRIC_LSB_RULED } else { METRIC_RSB_RULED };
                        }
                    }
                    SideRule::Follow { .. } => pending.push((i, left)),
                }
            }
        }
        // Follow rules read the *final* value of their target, so resolve the
        // ones whose target is settled first.
        let mut settled = vec![[true, true]; n];
        for &(i, left) in &pending {
            settled[i][if left { 0 } else { 1 }] = false;
        }
        for _round in 0..8 {
            let mut progressed = false;
            for &(i, left) in &pending {
                let k = if left { 0 } else { 1 };
                if settled[i][k] {
                    continue;
                }
                let rule = if left { self.glyphs[i].lsb_rule } else { self.glyphs[i].rsb_rule };
                if let SideRule::Follow { glyph, opposite, offset } = rule {
                    let t = glyph as usize;
                    if t >= n || !self.glyphs[t].valid {
                        continue;
                    }
                    let tk = if left != opposite { 0 } else { 1 };
                    if !settled[t][tk] {
                        continue;
                    }
                    let v = if tk == 0 { lsb[t] } else { rsb[t] } + offset;
                    if v.is_finite() {
                        if left {
                            lsb[i] = v;
                        } else {
                            rsb[i] = v;
                        }
                        flags[i] |= if left { METRIC_LSB_RULED } else { METRIC_RSB_RULED };
                    }
                    settled[i][k] = true;
                    progressed = true;
                }
            }
            if !progressed {
                break;
            }
        }
        // unresolved (cycles, missing targets): keep what the font has now
        for &(i, left) in &pending {
            let k = if left { 0 } else { 1 };
            if !settled[i][k] {
                let g = &self.glyphs[i];
                let cur = if left { g.cur_lsb } else { g.cur_rsb };
                let v = if cur.is_finite() { cur } else if left { free_l[i] } else { free_r[i] };
                if left {
                    lsb[i] = v;
                } else {
                    rsb[i] = v;
                }
                flags[i] |= if left { METRIC_LSB_RULED } else { METRIC_RSB_RULED };
            }
        }
        flags
    }
}

fn prepare_glyph(inp: GlyphInput, s: f64, plan: &RayPlan, dcfg: &DmatConfig, keep: bool) -> PreparedGlyph {
    let outline = Outline::from_contours(&inp.contours, 0.05 * s);
    let bbox = outline.bbox;
    let valid = !outline.is_empty() && bbox.width() > 0.5 * s && bbox.height() > 0.5 * s;
    let (left, right, inner) = if valid {
        let left = SdfProfile::build(&outline, Side::Left, plan);
        let right = SdfProfile::build(&outline, Side::Right, plan);
        let inner = pack_inner(&outline, &left, &right, dcfg);
        (left, right, inner)
    } else {
        (SdfProfile::empty(Side::Left), SdfProfile::empty(Side::Right), InnerWhite::default())
    };
    let (left_crevices, left_tips, nl) = facing_series(&left, dcfg, 150.0 * s);
    let (right_crevices, right_tips, nr) = facing_series(&right, dcfg, 150.0 * s);
    let bbox = if valid { bbox } else { BBox { x0: 0.0, y0: 0.0, x1: 0.0, y1: 0.0 } };
    let comb = |p: &SdfProfile| p.fixed_comb_equivalent(10.0 * s) as u32;
    // a joining side's body: the profile without its join band
    let band = |b: Option<(f64, f64)>| b.filter(|(y0, y1)| valid && y0.is_finite() && y1.is_finite() && y1 > y0);
    let (join_left, join_right) = (band(inp.join_left), band(inp.join_right));
    // kept whether or not a body remains without the band
    let (kept_left, kept_right) = (keep && join_left.is_some(), keep && join_right.is_some());
    let left_body = join_left.map(|b| left.masked(b)).filter(|p| !p.is_empty());
    let right_body = join_right.map(|b| right.masked(b)).filter(|p| !p.is_empty());
    let (join_left, join_right) = (join_left.filter(|_| left_body.is_some()), join_right.filter(|_| right_body.is_some()));
    PreparedGlyph {
        valid,
        fixed_advance: inp.flags & GLYPH_FIXED_ADVANCE != 0,
        advance: if inp.advance.is_finite() { inp.advance } else { 0.0 },
        group_id: inp.group.min(MAX_GROUPS - 1),
        group: 0,
        bbox,
        left_pieces: left.ink_segments().collect(),
        right_pieces: right.ink_segments().collect(),
        zone: (bbox.y0, bbox.y1),
        zone_left: bbox.x0,
        zone_right: bbox.x1,
        white_left: SideWhite::empty(Side::Left, 0.0, 0.0),
        white_right: SideWhite::empty(Side::Right, 0.0, 0.0),
        tip_left: tip_protrusion(&left_tips, Side::Left, bbox.x0),
        tip_right: tip_protrusion(&right_tips, Side::Right, bbox.x1),
        ink_left: ink_range(&left),
        ink_right: ink_range(&right),
        comb_left: comb(&left),
        comb_right: comb(&right),
        left,
        right,
        left_crevices,
        left_tips,
        right_crevices,
        right_tips,
        crevice_count: nl + nr + inner.crevices,
        counter_width: if valid { inner.volume / bbox.height() } else { 0.0 },
        inner,
        flags: inp.flags,
        script: inp.script,
        left_group_in: inp.left_group,
        right_group_in: inp.right_group,
        base: inp.base,
        lsb_rule: inp.lsb_rule,
        rsb_rule: inp.rsb_rule,
        cur_lsb: inp.cur_lsb,
        cur_rsb: inp.cur_rsb,
        join_left,
        join_right,
        left_body,
        right_body,
        kept_left,
        kept_right,
    }
}

#[cfg(test)]
mod zone_tests {
    use super::*;
    use crate::geometry::NODE_LINE;

    fn rect(x0: f64, y0: f64, x1: f64, y1: f64) -> Vec<(Vec2, u32)> {
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)].iter().map(|&(x, y)| (Vec2::new(x, y), NODE_LINE)).collect()
    }

    /// Three base letters (x-height 500) and five accented ones (to 720):
    /// marked, the base letters set the lowercase zone; unmarked, the
    /// accented majority lifts it to their height. (A glyph's zone is the
    /// group's clipped to its own extents: an accented letter's shows it.)
    fn zone_top(mark: bool) -> f64 {
        let mut inputs = Vec::new();
        for k in 0..3 {
            let mut g = GlyphInput::simple(vec![rect(50.0, 0.0, 450.0 + 10.0 * k as f64, 500.0)], 520.0, GROUP_LOWERCASE);
            if mark {
                g.flags |= GLYPH_ZONE;
            }
            inputs.push(g);
        }
        for k in 0..5 {
            let body = rect(50.0, 0.0, 450.0, 500.0);
            let accent = rect(180.0 + 5.0 * k as f64, 600.0, 320.0, 720.0);
            inputs.push(GlyphInput::simple(vec![body, accent], 520.0, GROUP_LOWERCASE));
        }
        let ctx = Context::prepare(inputs, 1000.0, 1, &Progress::new()).unwrap();
        ctx.glyphs[3].zone.1
    }

    #[test]
    fn base_letters_set_the_zone() {
        assert!((zone_top(true) - 500.0).abs() < 1e-9, "marked: {}", zone_top(true));
        assert!((zone_top(false) - 720.0).abs() < 1e-9, "unmarked: {}", zone_top(false));
    }
}
