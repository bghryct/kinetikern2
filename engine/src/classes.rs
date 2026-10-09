//! Kerning classes.
//!
//! A class is one side group: glyphs whose facing profile is (nearly) the
//! same, so one value serves all of them. Classes come from the designer's
//! kerning groups; a glyph without a group joins the group of its composite
//! base when its profile matches the base's wherever the base has ink (Á, À,
//! Ä join A; Ą, Ľ, Ơ get their own), otherwise it forms its own class. Then
//! classes without a designer group merge by shape: two whose representatives
//! match within a few units over the spacing zone (n and m on the right, H
//! and the Cyrillic Н, the straight stems of the Greek and Latin capitals)
//! become one, and the ink one of them has outside the zone (the ascender of
//! h next to n) is one more difference, like an accent.
//!
//! For every member the class keeps where its rays differ from the class
//! representative's (an accent above the cap height, a tail below the
//! baseline, a stem a unit wider). A member pair then shares the class value
//! only if no such difference lies within the interaction radius of the
//! partner's ink: then the pair evaluation sees exactly the representative's
//! samples. Every other member pair is verified (a few signs of the force) and
//! solved when it really differs.

use crate::engine::{PreparedGlyph, NONE};
use crate::profile::{PairBand, SdfProfile};

/// `origin` of a class that came from the designer's groups (low bits: the
/// caller's group id); otherwise `origin` is the glyph naming the new group.
pub const EXISTING: u32 = 1 << 31;
/// `origin` of a frozen glyph without a kerning group (low bits: the glyph):
/// a class of its own that is written with the glyph's name, so that Apply
/// never gives a frozen glyph a group.
pub const GLYPH_KEYED: u32 = 1 << 30;

/// Partition keys (`Classes::build_with`): classes never mix glyphs with
/// different keys — the spacing groups a user painted (glyphs kerned with
/// different force must not share a class value) and frozen glyphs, whose
/// key has this bit.
pub const PART_FROZEN: u32 = 1 << 31;
/// A side with a join (a connected script): it never shares a class with a
/// side without one, since a pair of joining sides is kerned otherwise.
pub const PART_JOIN: u32 = 1 << 30;

/// Glyph flags (input): the glyph is the key glyph of its left / right group
/// (its name is the group's name). Preferred as the class representative.
pub const GLYPH_LEFT_KEY: u32 = 8;
pub const GLYPH_RIGHT_KEY: u32 = 16;

/// Where a member differs from its class representative.
#[derive(Clone, Debug, PartialEq)]
pub enum Diff {
    Same,
    /// Height intervals with differences.
    Bands(Vec<(f64, f64)>),
    /// Different shape everywhere that matters (or a different rhythm group).
    All,
}

impl Diff {
    /// True if a difference lies within `[lo, hi]`.
    pub fn hits(&self, lo: f64, hi: f64) -> bool {
        match self {
            Diff::Same => false,
            Diff::All => true,
            Diff::Bands(b) => b.iter().any(|&(y0, y1)| y0 <= hi && y1 >= lo),
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct SideClasses {
    /// Class of every glyph (NONE for glyphs without outline).
    pub class_of: Vec<u32>,
    pub origin: Vec<u32>,
    pub rep: Vec<u32>,
    pub members: Vec<Vec<u32>>,
    /// Per glyph: difference from its representative.
    pub diff: Vec<Diff>,
    /// Per glyph: how much farther its bbox edge sits from the shared shape
    /// than the representative's (probe comparison).
    pub shift: Vec<f64>,
}

#[derive(Clone, Debug, Default)]
pub struct Classes {
    /// Right sides (glyph on the left of a pair, rightKerningGroup, @MMK_L_).
    pub right: SideClasses,
    /// Left sides (glyph on the right of a pair, leftKerningGroup, @MMK_R_).
    pub left: SideClasses,
}

/// Shape classes: representatives must match within this many units per
/// 1000 em over the spacing zone.
pub const SHAPE_EPS: f64 = 2.0;
/// Heights sampled across the zone for the shape pre-filter.
const SIGNATURE: usize = 24;

/// Runs of `differs` over the sorted heights `ys`, each reaching to the
/// neighbouring samples (the profiles interpolate between them).
fn runs(ys: &[f64], differs: impl Fn(usize) -> bool) -> Vec<(f64, f64)> {
    let n = ys.len();
    let mut bands: Vec<(f64, f64)> = Vec::new();
    let mut i = 0;
    while i < n {
        if !differs(i) {
            i += 1;
            continue;
        }
        let mut j = i;
        while j + 1 < n && differs(j + 1) {
            j += 1;
        }
        let y0 = ys[i.saturating_sub(1)];
        let y1 = ys[(j + 1).min(n - 1)];
        match bands.last_mut() {
            Some(last) if last.1 >= y0 => last.1 = last.1.max(y1),
            _ => bands.push((y0, y1)),
        }
        i = j + 1;
    }
    bands
}

/// Two profiles of the same side merged and aligned by translation: the
/// merged samples, the offset, and which samples differ by more than `eps`
/// (or where only one has ink); None without common ink.
fn aligned(p: &SdfProfile, r: &SdfProfile, eps: f64) -> Option<(PairBand, f64, Vec<bool>)> {
    let band = PairBand::merge(p, r);
    let mut d: Vec<f64> = band
        .xa
        .iter()
        .zip(&band.xb)
        .filter(|(a, b)| a.is_finite() && b.is_finite())
        .map(|(a, b)| a - b)
        .collect();
    if d.is_empty() {
        return None;
    }
    d.sort_by(f64::total_cmp);
    let t = d[d.len() / 2];
    let differs = (0..band.len())
        .map(|i| {
            let (a, b) = (band.xa[i], band.xb[i]);
            match (a.is_finite(), b.is_finite()) {
                (true, true) => (a - b - t).abs() > eps,
                (false, false) => false,
                _ => true,
            }
        })
        .collect();
    Some((band, t, differs))
}

/// Compares two profiles of the same side, aligned by translation. Returns the
/// height intervals where they differ by more than `eps` (or where only one
/// has ink) and the alignment offset, or None without common ink.
pub fn compare(p: &SdfProfile, r: &SdfProfile, eps: f64) -> Option<(Vec<(f64, f64)>, f64)> {
    let (band, t, differs) = aligned(p, r, eps)?;
    Some((runs(&band.ys, |i| differs[i]), t))
}

/// True if the profiles, aligned, are within `eps` of each other at every
/// sample strictly inside `zone` (heights).
fn same_within(p: &SdfProfile, r: &SdfProfile, eps: f64, zone: (f64, f64)) -> bool {
    aligned(p, r, eps)
        .is_some_and(|(band, _, differs)| !(0..band.len()).any(|i| differs[i] && band.ys[i] > zone.0 && band.ys[i] < zone.1))
}

/// Heights where the rays of `p` and of `r` shifted by `t` are not the same
/// rays: one profile has a ray the other lacks, or ink or x differ. Outside
/// these intervals a pair evaluation sees the same samples, weights and
/// pieces with either profile.
pub fn ray_diff(p: &SdfProfile, r: &SdfProfile, t: f64) -> Vec<(f64, f64)> {
    let (a, b) = (&p.rays, &r.rays);
    let mut ys: Vec<f64> = Vec::with_capacity(a.len() + b.len());
    let mut differs: Vec<bool> = Vec::with_capacity(a.len() + b.len());
    let (mut i, mut j) = (0, 0);
    while i < a.len() || j < b.len() {
        if j >= b.len() || (i < a.len() && a[i].y < b[j].y) {
            ys.push(a[i].y);
            differs.push(true);
            i += 1;
        } else if i >= a.len() || b[j].y < a[i].y {
            ys.push(b[j].y);
            differs.push(true);
            j += 1;
        } else {
            let same = match (a[i].has_ink(), b[j].has_ink()) {
                (true, true) => (a[i].x - b[j].x - t).abs() <= 1e-9 * (1.0 + a[i].x.abs() + t.abs()),
                (false, false) => true,
                _ => false,
            };
            ys.push(a[i].y);
            differs.push(!same);
            i += 1;
            j += 1;
        }
    }
    runs(&ys, |k| differs[k])
}

/// The profile at evenly spaced heights across `zone` (NaN where no ink).
fn signature(p: &SdfProfile, zone: (f64, f64)) -> [f64; SIGNATURE] {
    let mut s = [f64::NAN; SIGNATURE];
    for (k, v) in s.iter_mut().enumerate() {
        let y = zone.0 + (zone.1 - zone.0) * k as f64 / (SIGNATURE - 1) as f64;
        *v = p.eval(y).unwrap_or(f64::NAN);
    }
    s
}

/// True if two signatures can belong to profiles within `eps` of each other
/// after a translation (a necessary condition of `compare` passing).
fn signatures_close(a: &[f64; SIGNATURE], b: &[f64; SIGNATURE], eps: f64) -> bool {
    let (mut lo, mut hi) = (f64::INFINITY, f64::NEG_INFINITY);
    for k in 0..SIGNATURE {
        match (a[k].is_finite(), b[k].is_finite()) {
            (true, true) => {
                let d = a[k] - b[k];
                lo = lo.min(d);
                hi = hi.max(d);
            }
            (false, false) => {}
            _ => return false,
        }
    }
    hi - lo <= 2.0 * eps
}

impl SideClasses {
    fn build(glyphs: &[PreparedGlyph], right: bool, eps: f64, shape_eps: f64, part: Option<&[u32]>) -> SideClasses {
        let n = glyphs.len();
        let joins = |i: usize| if right { glyphs[i].join_right.is_some() } else { glyphs[i].join_left.is_some() };
        let any_join = (0..n).any(|i| glyphs[i].valid && joins(i));
        let key = |i: usize| part.map_or(0, |p| p.get(i).copied().unwrap_or(0)) | if joins(i) { PART_JOIN } else { 0 };
        let is_frozen = |i: usize| key(i) & PART_FROZEN != 0;
        let profile = |i: usize| if right { &glyphs[i].right } else { &glyphs[i].left };
        let existing = |i: usize| if right { glyphs[i].right_group_in } else { glyphs[i].left_group_in };
        let key_flag = if right { GLYPH_RIGHT_KEY } else { GLYPH_LEFT_KEY };
        let ink = |i: usize| if right { glyphs[i].ink_right } else { glyphs[i].ink_left };

        let mut c = SideClasses {
            class_of: vec![NONE; n],
            diff: vec![Diff::Same; n],
            shift: vec![0.0; n],
            ..Default::default()
        };
        // 1. the designer's groups. A group whose members fall into different
        //    partitions stays the group of its frozen members, else of its
        //    largest partition; the other members are grouped anew below, as
        //    if they had none.
        let mut keeper: std::collections::HashMap<u32, u32> = std::collections::HashMap::new();
        if part.is_some() || any_join {
            let mut counts: std::collections::HashMap<u32, std::collections::BTreeMap<u32, usize>> =
                std::collections::HashMap::new();
            for i in (0..n).filter(|&i| glyphs[i].valid && existing(i) != NONE) {
                *counts.entry(existing(i)).or_default().entry(key(i)).or_insert(0) += 1;
            }
            for (id, by_key) in counts {
                let k = match by_key.keys().find(|&&k| k & PART_FROZEN != 0) {
                    Some(&k) => k,
                    // largest partition, the smallest key on ties
                    None => by_key.iter().max_by(|a, b| a.1.cmp(b.1).then(b.0.cmp(a.0))).map(|(&k, _)| k).unwrap_or(0),
                };
                keeper.insert(id, k);
            }
        }
        let mut by_id: std::collections::HashMap<u32, u32> = std::collections::HashMap::new();
        for i in (0..n).filter(|&i| glyphs[i].valid) {
            let id = existing(i);
            if id != NONE && keeper.get(&id).map_or(true, |&k| k == key(i)) {
                let k = *by_id.entry(id).or_insert_with(|| {
                    c.origin.push(EXISTING | (id & !EXISTING));
                    c.members.push(Vec::new());
                    (c.origin.len() - 1) as u32
                });
                c.class_of[i] = k;
                c.members[k as usize].push(i as u32);
            }
        }
        // 2. glyphs without a group: their composite base's class, if the
        //    profile matches the base wherever the base has ink
        #[allow(clippy::too_many_arguments)]
        fn assign<'g>(
            i: usize,
            glyphs: &'g [PreparedGlyph],
            c: &mut SideClasses,
            depth: u32,
            profile: &dyn Fn(usize) -> &'g SdfProfile,
            ink: &dyn Fn(usize) -> (f64, f64),
            eps: f64,
            is_frozen: &dyn Fn(usize) -> bool,
            key: &dyn Fn(usize) -> u32,
        ) -> u32 {
            if c.class_of[i] != NONE {
                return c.class_of[i];
            }
            if is_frozen(i) {
                // a frozen glyph without a group keeps none: a class of its own
                c.origin.push(GLYPH_KEYED | i as u32);
                c.members.push(vec![i as u32]);
                c.class_of[i] = (c.origin.len() - 1) as u32;
                return c.class_of[i];
            }
            let b = glyphs[i].base;
            let mut target = NONE;
            if b != NONE
                && (b as usize) < glyphs.len()
                && b as usize != i
                && glyphs[b as usize].valid
                && key(b as usize) == key(i)
                && depth < 8
            {
                let kb = assign(b as usize, glyphs, c, depth + 1, profile, ink, eps, is_frozen, key);
                let (y0, y1) = ink(b as usize);
                let matches = glyphs[i].group_id == glyphs[b as usize].group_id
                    && compare(profile(i), profile(b as usize), eps)
                        .is_some_and(|(bands, _)| !bands.iter().any(|&(lo, hi)| lo <= y1 && hi >= y0));
                if matches {
                    target = kb;
                }
            }
            if target == NONE {
                c.origin.push(i as u32);
                c.members.push(Vec::new());
                target = (c.origin.len() - 1) as u32;
            }
            c.class_of[i] = target;
            c.members[target as usize].push(i as u32);
            target
        }
        for i in (0..n).filter(|&i| glyphs[i].valid) {
            assign(i, glyphs, &mut c, 0, &profile, &ink, eps, &is_frozen, &key);
        }
        // 2b. classes without a designer group merge when their representatives
        //     have the same shape over the spacing zone (same rhythm group)
        c.merge_shapes(glyphs, &profile, shape_eps, &key);
        // 3. representatives: the key glyph, else the first non-composite, else the first
        c.rep = c
            .members
            .iter()
            .zip(&c.origin)
            .map(|(m, &origin)| {
                if origin & EXISTING == 0 {
                    return origin & !GLYPH_KEYED;
                }
                m.iter()
                    .copied()
                    .find(|&g| glyphs[g as usize].flags & key_flag != 0)
                    .or_else(|| m.iter().copied().find(|&g| glyphs[g as usize].base == NONE))
                    .unwrap_or(m[0])
            })
            .collect();
        for members in c.members.iter_mut() {
            members.sort_unstable();
        }
        // 4. where every member differs from its representative
        for (k, members) in c.members.iter().enumerate() {
            let r = c.rep[k] as usize;
            for &g in members {
                let g = g as usize;
                if g == r {
                    continue;
                }
                if glyphs[g].group_id != glyphs[r].group_id {
                    c.diff[g] = Diff::All;
                    continue;
                }
                match compare(profile(g), profile(r), eps) {
                    None => c.diff[g] = Diff::All,
                    Some((_, t)) => {
                        c.shift[g] = (profile(g).extreme - profile(r).extreme) - t;
                        let bands = ray_diff(profile(g), profile(r), t);
                        c.diff[g] = if bands.is_empty() { Diff::Same } else { Diff::Bands(bands) };
                    }
                }
            }
        }
        c
    }

    /// Merges classes without a designer group whose origin glyphs match within
    /// `eps` over the spacing zone into the first such class. Origins are
    /// visited in glyph order, so a class keeps the earliest glyph's name.
    fn merge_shapes<'g>(
        &mut self,
        glyphs: &'g [PreparedGlyph],
        profile: &dyn Fn(usize) -> &'g SdfProfile,
        eps: f64,
        key: &dyn Fn(usize) -> u32,
    ) {
        if eps <= 0.0 {
            return;
        }
        let k_all = self.origin.len();
        let mut into: Vec<u32> = (0..k_all as u32).collect();
        // heads: (class, origin glyph, signature)
        let mut heads: Vec<(usize, usize, [f64; SIGNATURE])> = Vec::new();
        for k in 0..k_all {
            if self.origin[k] & (EXISTING | GLYPH_KEYED) != 0 {
                continue;
            }
            let g = self.origin[k] as usize;
            let zone = glyphs[g].zone;
            let sig = signature(profile(g), zone);
            if sig.iter().filter(|x| x.is_finite()).count() < SIGNATURE / 2 {
                continue;
            }
            let head = heads.iter().find(|&&(_, h, ref hs)| {
                let gh = &glyphs[h];
                gh.group_id == glyphs[g].group_id
                    && key(h) == key(g)
                    && (gh.zone.0 - zone.0).abs() <= eps
                    && (gh.zone.1 - zone.1).abs() <= eps
                    && signatures_close(&sig, hs, eps)
                    && same_within(profile(g), profile(h), eps, zone)
            });
            match head {
                Some(&(kh, _, _)) => into[k] = kh as u32,
                None => heads.push((k, g, sig)),
            }
        }
        if heads.len() == k_all {
            return;
        }
        // renumber: surviving classes keep their order, merged ones join their head
        let mut new_index = vec![NONE; k_all];
        let (mut origin, mut members) = (Vec::new(), Vec::<Vec<u32>>::new());
        for k in 0..k_all {
            if into[k] as usize == k {
                new_index[k] = origin.len() as u32;
                origin.push(self.origin[k]);
                members.push(Vec::new());
            }
        }
        for k in 0..k_all {
            let target = new_index[into[k] as usize];
            for &g in &self.members[k] {
                self.class_of[g as usize] = target;
                members[target as usize].push(g);
            }
        }
        self.origin = origin;
        self.members = members;
    }

    pub fn len(&self) -> usize {
        self.rep.len()
    }

    pub fn is_empty(&self) -> bool {
        self.rep.is_empty()
    }
}

impl Classes {
    pub fn build(glyphs: &[PreparedGlyph], upm: f64) -> Classes {
        Self::build_with(glyphs, upm, None)
    }

    /// Classes that never mix partitions (`PART_FROZEN`, `GLYPH_KEYED` and
    /// step 1 of `SideClasses::build`).
    pub fn build_with(glyphs: &[PreparedGlyph], upm: f64, part: Option<&[u32]>) -> Classes {
        let eps = 0.5 * upm / 1000.0;
        let shape_eps = SHAPE_EPS * upm / 1000.0;
        Classes {
            right: SideClasses::build(glyphs, true, eps, shape_eps, part),
            left: SideClasses::build(glyphs, false, eps, shape_eps, part),
        }
    }

    /// True for every class of `side` whose members are all frozen.
    pub fn frozen_classes(side: &SideClasses, frozen: &[bool]) -> Vec<bool> {
        side.members
            .iter()
            .map(|m| !m.is_empty() && m.iter().all(|&g| frozen.get(g as usize).copied().unwrap_or(false)))
            .collect()
    }
}
