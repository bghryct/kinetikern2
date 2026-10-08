# encoding: utf-8
"""
kk2_apply — writes a solve into the font, and takes it back.

A Planner turns a Result into the exact writes an Apply makes (kerning keys
and values, new kerning-group memberships, sidebearings, removed entries)
without touching the font, so the window can say what will happen before it
happens. An Applier carries a plan out. Kerning goes in one entry at a time
through the Objective-C setter with undo registration off: 3.5 µs per entry
instead of 33, and unlike swapping in a whole new kerning dictionary (instant)
it leaves Glyphs' undo history usable. Undo therefore does not cover an
Apply; the RevertPoint taken first puts back what the Apply changed.

All three (and the Restorer that reverts) work in slices: step(budget_s)
does at most about `budget_s` of work and returns, so the window's timer can
drive them between events and show progress; a whole-font Apply is a second
or more of writes. Interface updates and undo registration are off within a
slice and back on between slices. plan(), apply() and RevertPoint.restore()
run a stepper to the end in one call (tools and tests).

Main thread only (GSFont is not thread-safe). Nothing here looks at glyph
pairs: a plan walks the result's entries once and the font's existing
kerning once, and an apply makes one call per write.

Kerning keys are Glyphs' own: a glyph is its glyph.id; a class on the left of
a pair is "@MMK_L_<right group>", on the right "@MMK_R_<left group>".
Precedence in Glyphs and in the engine is the same: glyph–glyph, glyph–class,
class–glyph, class–class.

Composites stay rigid. A component draws its base glyph's outline where the
base has it, so moving a base (a new LSB) would move that part inside every
composite built from it: the accent of Aacute would slide off its A, and the
composite's own new sidebearings (computed by the engine for its shape as it
was) would land wrong. Whenever Apply moves an outline it therefore moves each
component drawing it (unless Glyphs aligns that component automatically) back
by the same amount. Glyphs caches a layer's LSB / RSB and does not refresh a
composite's when a base moves, so composites are refreshed (updateMetrics)
before they are read.
"""

from __future__ import division, print_function, unicode_literals

import math
import time
import traceback

import kk2_bridge as kb

try:
    from GlyphsApp import LTR
except ImportError:  # outside Glyphs (tools, tests)
    LTR = 0

# KK2Metrics.flags: the side follows a metrics key or an aligned component
# instead of Pass 1. Glyphs computes those sides; Apply leaves them alone.
METRIC_LSB_RULED = 1
METRIC_RSB_RULED = 2

LEFT_PREFIX = "@MMK_L_"  # class on the left of a pair: its glyphs' right group
RIGHT_PREFIX = "@MMK_R_"  # class on the right of a pair: its glyphs' left group
NEW_GROUP_SUFFIX = ".kk2"
NO_KERNING = 1e9  # Glyphs reports a missing pair as NSNotFound
READBACK_KERNING = 500
READBACK_REMOVALS = 100
EXAMPLES = 20
SAME = 1e-6  # a kerning value read back equals the one written
SAME_METRIC = 1e-3  # a sidebearing or width is still the one Apply left

_FOLLOW = (kb.RULE_FOLLOW_SAME, kb.RULE_FOLLOW_OPPOSITE)
_DECIDE = object()  # a Restorer's work yields this when it needs the caller's decision


def round_units(value):
    """Rounds half away from zero, as the engine does (Rust f64::round)."""
    return int(value + 0.5) if value >= 0 else -int(0.5 - value)


def _moved(target, current):
    """Where a side goes to reach `target`: moved by whole units, so an outline
    on the grid stays on it (a fractional current side stays fractional).
    None if it stays where it is."""
    if target is None:
        return None
    if current is None or not math.isfinite(current):
        return float(target)
    d = round_units(target - current)
    return current + d if d else None


def _text(value):
    return str(value) if value else None


def _value(v):
    """A kerning value read from Glyphs, or None for "no entry"."""
    if v is None:
        return None
    v = float(v)
    return v if math.isfinite(v) and abs(v) < NO_KERNING else None


def _same_value(a, b):
    """Two kerning values (None: no entry) are the same entry."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= SAME


def _glyph(font, name):
    try:
        return font.glyphs[name]
    except (KeyError, IndexError, TypeError):
        return None


def _layer(glyph, master_id):
    if glyph is None:
        return None
    try:
        return glyph.layers[master_id]
    except (KeyError, IndexError, TypeError):
        return None


class _UndoOff(object):
    """Undo registration off for the font and for every glyph written (a
    glyph keeps its own undo manager for its layers; with it on, an LSB write
    costs 0.63 ms instead of 0.21). Apply and Revert are their own undo."""

    def __init__(self, font):
        self.managers = []
        self.add(font)

    def add(self, obj):
        try:
            um = obj.undoManager()
        except Exception:
            return
        if um is None:
            return
        enabled = getattr(um, "isUndoRegistrationEnabled", None)
        if enabled is None or enabled():
            um.disableUndoRegistration()
            self.managers.append(um)

    def glyphs(self, font, names):
        for name in names:
            glyph = _glyph(font, name)
            if glyph is not None:
                self.add(glyph)

    def forget(self, obj):
        """Drops the undo actions `obj` recorded before a write made with
        registration off: replayed on top of it, they would put single values
        from before back (absolute values: a kerning pair, a sidebearing) and
        leave the font neither applied nor reverted. removeAllActions also
        turns registration back on, so a manager that was off is turned off
        again (close() stays balanced)."""
        try:
            um = obj.undoManager()
        except Exception:
            return
        remove = getattr(um, "removeAllActions", None) if um is not None else None
        if remove is None:
            return
        enabled = getattr(um, "isUndoRegistrationEnabled", None)
        was_off = enabled is not None and not enabled()
        remove()
        if was_off and (enabled is None or enabled()):
            um.disableUndoRegistration()

    def close(self):
        while self.managers:
            self.managers.pop().enableUndoRegistration()


def _has_metrics_key(glyph, layer):
    for obj in (layer, glyph):
        for attr in ("leftMetricsKey", "rightMetricsKey", "widthMetricsKey"):
            if getattr(obj, attr, None):
                return True
    return False


def _xy(point):
    try:
        return float(point.x), float(point.y)
    except AttributeError:
        return float(point[0]), float(point[1])


def _refresh(glyph, layer):
    """Makes a composite's cached LSB / RSB current (Glyphs does not refresh
    them when a base glyph moves). Layers with metrics keys are left to
    syncMetrics: refreshing must not apply a key."""
    if layer is None or not getattr(layer, "components", None) or _has_metrics_key(glyph, layer):
        return
    update = getattr(layer, "updateMetrics", None)
    if update is not None:
        update()


def _component_users(font, master_id, bases, candidates=None, users=None):
    """Fills `users` (a new dict if None) with {base glyph: [(composite,
    component index, component, dx, dy)]} for every component on this master
    that draws one of `bases` and that Glyphs does not align by itself; (dx,
    dy) is where a base outline moved by one unit along x lands in the
    composite (the component's first matrix column). `candidates`: names of
    the glyphs that may hold such components (the snapshot knows them); None
    looks at every glyph of the font. A generator: it yields after every few
    glyphs (a composite costs ~60 µs to look at), so the Applier can slice it."""
    if users is None:
        users = {}
    if not bases:
        return users
    if candidates is None:
        try:
            glyphs = list(font.glyphs)
        except TypeError:
            return users
    else:
        glyphs = (_glyph(font, name) for name in sorted(candidates))
    for n, glyph in enumerate(glyphs):
        if not n & 15:
            yield
        if glyph is None:
            continue
        layer = _layer(glyph, master_id)
        comps = getattr(layer, "components", None) if layer is not None else None
        if not comps or getattr(layer, "isAligned", False):
            continue
        for k, comp in enumerate(comps):
            base = _text(getattr(comp, "componentName", None))
            if base not in bases or getattr(comp, "automaticAlignment", False) or getattr(comp, "position", None) is None:
                continue
            t = getattr(comp, "transform", None)
            dx, dy = (float(t[0]), float(t[1])) if t is not None and len(t) >= 2 else (1.0, 0.0)
            users.setdefault(base, []).append((str(glyph.name), k, comp, dx, dy))
    return users


def _master_rows(font, master_id):
    """The master's LTR kerning as Glyphs keeps it ({left: {right: value}}), or None."""
    try:
        whole = font.kerningLTR
    except AttributeError:
        whole = getattr(font, "kerning", None)  # Glyphs before 3.2: LTR only
    if not whole:
        return None
    try:
        return whole.get(master_id)
    except Exception:
        return None


def master_kerning(font, master_id):
    """A plain-Python copy of the master's LTR kerning: {left key: {right key:
    value}} (rows Glyphs keeps empty after removals are left out). For tools
    and tests: O(entries) on the main thread."""
    rows = _master_rows(font, master_id)
    out = {}
    if rows:
        for lk, row in rows.items():
            if row:
                out[str(lk)] = {str(rk): float(v) for rk, v in row.items()}
    return out


# ------------------------------------------------------------- steppers
class _Stepper(object):
    """Runs the generator `_work()` in slices. The work sets `phase`,
    `text` and `fraction` as it goes and yields wherever it may be paused
    (often: a yield costs well under a microsecond); step() resumes it until
    the slice's budget is spent. Busy time is kept per phase in `ms`."""

    def __init__(self):
        self.done = False
        self.waiting = False  # paused for the caller (a Restorer's decision)
        self.phase = "start"
        self.text = ""
        self.fraction = 0.0
        self.ms = {}
        self.busy_ms = 0.0
        self.slice_ms = (0.0, None)  # longest slice and the phases it ran (diagnostics)
        self.end_slice_ms = 0.0  # longest _end_slice (turning interface updates back on)
        self.started = time.perf_counter()
        self._gen = None
        self._value = None

    def _begin_slice(self):
        pass

    def _end_slice(self):
        pass

    def _finished(self, value):
        pass

    def _failed(self, error):
        """An exception in the work; the default lets it through."""
        return False

    def step(self, budget_s=0.008):
        """Works for about `budget_s` seconds (at least to the next pause).
        True once done."""
        if self.done:
            return True
        if self.waiting:
            return False
        if self._gen is None:
            self._gen = self._work()
        t0 = time.perf_counter()
        deadline = t0 + max(0.0, budget_s)
        finished = False
        phases = [self.phase]
        self._begin_slice()
        try:
            t = t0
            while True:
                phase = self.phase
                if phase != phases[-1]:
                    phases.append(phase)
                try:
                    out = next(self._gen)
                except StopIteration as stop:
                    self._value = stop.value
                    finished = True
                except Exception:
                    if not self._failed(traceback.format_exc()):
                        raise
                    finished = True
                now = time.perf_counter()
                self.ms[phase] = self.ms.get(phase, 0.0) + 1000.0 * (now - t)
                t = now
                if finished:
                    break
                if out is _DECIDE:
                    self.waiting = True
                    break
                if now >= deadline:
                    break
        finally:
            t_end = time.perf_counter()
            self._end_slice()
            t1 = time.perf_counter()
            self.end_slice_ms = max(self.end_slice_ms, 1000.0 * (t1 - t_end))
            slice_ms = 1000.0 * (t1 - t0)
            if slice_ms > self.slice_ms[0]:
                self.slice_ms = (slice_ms, "/".join(phases))
            self.busy_ms += slice_ms
        if finished:
            self.done = True
            self.fraction = 1.0
            self._finished(self._value)
        return self.done

    def run(self):
        """Steps to the end in one call (tools and tests, never the window)."""
        while not self.step(1.0):
            if self.waiting:
                raise RuntimeError("%s waits for a decision" % type(self).__name__)
        return self


# ----------------------------------------------------------------- plan
class ApplyPlan(object):
    """Everything one Apply writes, decided before anything is written.

    kerning        [(left key, right key, int value)], one per result entry
    groups_to_set  {glyph: (left group or None, right group or None)}: only
                   sides without a group, of glyphs joining a class the
                   kerning uses
    metrics        {glyph: (LSB or None, RSB or None)}: targets in Glyphs'
                   own measure (layer.LSB / RSB) for the free sides; None
                   for sides a metrics key or an aligned component drives
    keep_width     glyphs whose advance stays (tabular figures): the LSB
                   moves, the width is put back
    sync           glyphs following others (keys, aligned components),
                   targets first; follow {glyph: [targets]}
    removals       [(left key, right key)] existing entries Replace removes
    groups_to_move {glyph: (left group or None, right group or None)}: sides
                   that leave a group they shared with frozen glyphs for a
                   class of their own (spacing groups)
    frozen         glyphs Apply leaves alone (frozen spacing groups)
    counts         for the confirmation dialog (contract keys, plus
                   "joining_existing" and "other_masters")
    details        a finer breakdown
    """

    def __init__(self, master_id, master_name, replace):
        self.master_id = master_id
        self.master_name = master_name
        self.replace = bool(replace)
        self.kerning = []
        self.groups_to_set = {}
        self.metrics = {}
        self.keep_width = set()
        self.sync = []
        self.follow = {}
        self.removals = []
        self.groups_to_move = {}
        self.frozen = set()
        self.right_class_names = []
        self.left_class_names = []
        self.counts = {}
        self.details = {}
        self.ms = 0.0


def _class_names(origins, group_names, names, taken):
    """Group name of every class on one side. A class from the designer's
    groups keeps its name; a new class is named after the glyph it grew from,
    plus ".kk2" when that name is taken on this side (by a group with other
    glyphs, or by kerning keys of a group no glyph carries any more, in any
    master): the new class must not merge into either."""
    out = []
    for origin in origins:
        if origin & kb.EXISTING:
            gid = origin & 0x7FFFFFFF
            if gid >= len(group_names):
                raise ValueError("class origin names group %d of %d" % (gid, len(group_names)))
            out.append(group_names[gid])
            continue
        if origin & kb.GLYPH_KEYED:
            out.append(None)  # a frozen glyph without a group: written with its own key
            continue
        name = names[origin]
        if name in taken:
            candidate = name + NEW_GROUP_SUFFIX
            k = 1
            while candidate in taken:
                k += 1
                candidate = "%s%s.%d" % (name, NEW_GROUP_SUFFIX, k)
            name = candidate
        taken.add(name)
        out.append(name)
    return out


def _covers_font(specs, kern, rows):
    """True if the solve kerned every glyph it could (a whole-font run)."""
    for i, spec in enumerate(specs):
        if not kern[i] and spec.flags & kb.GLYPH_KERN and not spec.flags & kb.GLYPH_RTL and rows[i].valid:
            return False
    return True


def _fully_kerned(specs, kern, attr):
    """Group ids every one of whose spacing glyphs was kerned."""
    total, done = {}, {}
    for i, spec in enumerate(specs):
        gid = getattr(spec, attr)
        if gid != kb.NONE:
            total[gid] = total.get(gid, 0) + 1
            if kern[i]:
                done[gid] = done.get(gid, 0) + 1
    return set(g for g, t in total.items() if done.get(g, 0) == t)


def _followers(specs, names, start):
    """Glyphs whose sides follow other glyphs and that a change of `start`
    (indices) reaches, targets before followers; and their targets. A glyph
    that follows itself ("=|": one side mirrors the other) is its own
    target: moving its free side must re-evaluate the key."""
    n = len(specs)
    targets = {}
    for i, spec in enumerate(specs):
        ts = []
        for rule, t in ((spec.lsb_rule, spec.lsb_glyph), (spec.rsb_rule, spec.rsb_glyph)):
            if rule in _FOLLOW and t < n and t not in ts:
                ts.append(t)
        if ts:
            targets[i] = ts
    if not targets:
        return [], {}
    # chain depth over other glyphs (a cycle stops growing at 16; Glyphs
    # leaves cycles as they are)
    depth = dict.fromkeys(targets, 0)
    for _round in range(16):
        moved = False
        for i, ts in targets.items():
            d = 1 + max([depth.get(t, -1) for t in ts if t != i] or [-1])
            if depth[i] < d <= 16:
                depth[i] = d
                moved = True
        if not moved:
            break
    reached = set(start)
    order = []
    for i in sorted(targets, key=lambda k: (depth[k], k)):
        if any(t in reached for t in targets[i]):
            reached.add(i)
            order.append(i)
    return [names[i] for i in order], dict((names[i], [names[t] for t in targets[i]]) for i in order)


class Planner(_Stepper):
    """Builds the ApplyPlan for applying `result` to the snapshot's master,
    in slices (`plan` once done).

    `replace`: remove existing entries between the kerned glyphs (and their
    classes) that the plan does not overwrite. `metrics_names`: glyphs whose
    sidebearings are written; None = every glyph when the solve covered the
    whole font, else the glyphs it kerned (either way plus the glyphs their
    metrics keys and aligned components follow). `scope_scripts`: the solve
    paired only glyphs of one script (plus Common), as the window's solves
    do; Replace then keeps existing pairs across scripts, which the engine
    never saw. The result must stay open until the planner is done.
    """

    def __init__(self, snapshot, result, replace, metrics_names=None, scope_scripts=True, frozen=None):
        _Stepper.__init__(self)
        self.plan = None
        self._args = (snapshot, result, replace, metrics_names, scope_scripts, frozen)
        self.text = "Planning"

    def _finished(self, value):
        self.plan = value
        value.ms = self.busy_ms

    def _work(self):
        snapshot, result, replace, metrics_names, scope_scripts, frozen_names = self._args
        self._args = None
        names = list(snapshot.names)
        n = len(names)
        specs = snapshot.specs
        if result.glyph_count != n or len(specs) != n:
            raise ValueError("the result (%d glyphs) does not belong to this snapshot (%d glyphs, %d specs)"
                             % (result.glyph_count, n, len(specs)))
        font = snapshot.font
        mid = snapshot.master_id
        p = ApplyPlan(mid, getattr(snapshot, "master_name", ""), replace)
        infos = snapshot.infos
        gkey = []
        for name in names:
            info = infos.get(name)
            gkey.append(_text(getattr(info, "glyph_id", None)) or name)
        kern = bytes(result.kern_mask) if n else b""
        none = [kb.NONE] * n
        rclass = result.glyph_right_class[:] if len(result.glyph_right_class) == n else none
        lclass = result.glyph_left_class[:] if len(result.glyph_left_class) == n else none
        right_groups, left_groups = snapshot.right_group_names, snapshot.left_group_names
        frozen_names = set(frozen_names or ())
        frozen = bytearray(n)
        for i, name in enumerate(names):
            if name in frozen_names:
                frozen[i] = 1
        p.frozen = set(name for name in names if name in frozen_names)
        yield

        # 1. the master's kerning as it is (keys only), and the group names
        #    kerning keys use in any master (groups belong to the glyph, so a
        #    new class must not take a name another master's kerning uses)
        self.phase = "existing"
        existing = []
        kern_right, kern_left = set(), set()
        try:
            masters = [str(m.id) for m in font.masters]
        except Exception:
            masters = [mid]
        if mid not in masters:
            masters.append(mid)
        k = 0
        for m_id in masters:
            rows = _master_rows(font, m_id)
            if not rows:
                continue
            this = m_id == mid
            for lk, row in rows.items():
                lk = str(lk)
                if lk.startswith(LEFT_PREFIX):
                    kern_right.add(lk[7:])
                for rk in row.keys():
                    rk = str(rk)
                    if rk.startswith(RIGHT_PREFIX):
                        kern_left.add(rk[7:])
                    if this:
                        existing.append((lk, rk))
                    k += 1
                    if not k & 511:
                        yield
            yield
        self.fraction = 0.25

        # 2. class names and keys
        self.phase = "kerning"
        right_names = _class_names(result.right_class_origin[:], right_groups, names, set(right_groups) | kern_right)
        left_names = _class_names(result.left_class_origin[:], left_groups, names, set(left_groups) | kern_left)
        r_origins, l_origins = result.right_class_origin[:], result.left_class_origin[:]
        rkeys = [LEFT_PREFIX + s if s is not None else gkey[r_origins[c] & ~kb.GLYPH_KEYED]
                 for c, s in enumerate(right_names)]
        lkeys = [RIGHT_PREFIX + s if s is not None else gkey[l_origins[q] & ~kb.GLYPH_KEYED]
                 for q, s in enumerate(left_names)]
        yield

        # 3. kerning: every entry; class pairs that round to zero say nothing,
        #    zero exceptions override their class pair and are written
        classes = bool(result.classes)
        kerning = []
        add = kerning.append
        class_pairs = set()
        used_r = bytearray(len(right_names))
        used_l = bytearray(len(left_names))
        cc = gc = cg = gg = zero_exceptions = zero_classes = 0
        total = max(1, result.entry_count)
        for k, (kind, a, b, v, _imp) in enumerate(result.iter_entries()):
            if not k & 1023:
                self.fraction = 0.25 + 0.3 * k / total
                yield
            if not math.isfinite(v):
                continue
            iv = round_units(v)
            if kind == kb.ENTRY_CLASS_CLASS:
                if iv == 0:
                    zero_classes += 1
                    continue
                add((rkeys[a], lkeys[b], iv))
                class_pairs.add((rkeys[a], lkeys[b]))
                used_r[a] = used_l[b] = 1
                cc += 1
                continue
            if kind == kb.ENTRY_GLYPH_CLASS:
                add((gkey[a], lkeys[b], iv))
                used_l[b] = 1
                gc += 1
            elif kind == kb.ENTRY_CLASS_GLYPH:
                add((rkeys[a], gkey[b], iv))
                used_r[a] = 1
                cg += 1
            else:
                if iv == 0 and not classes:
                    continue  # a plain glyph pair of zero overrides nothing
                add((gkey[a], gkey[b], iv))
                gg += 1
            if iv == 0:
                zero_exceptions += 1
        self.fraction = 0.55

        # 4. groups: kerned glyphs without a group join the class the kerning uses
        self.phase = "groups"
        origins_r, origins_l = result.right_class_origin, result.left_class_origin
        groups = {}
        moves = {}
        group_sides = joining = 0
        for i in range(n):
            if not i & 1023:
                yield
            if not kern[i] or frozen[i]:
                continue  # a frozen glyph keeps its groups
            spec = specs[i]
            left = right = None
            move_l = move_r = None
            joins = False
            q = lclass[i]
            if q != kb.NONE and used_l[q] and left_names[q] is not None:
                if spec.left_group == kb.NONE:
                    left = left_names[q]
                    joins = bool(origins_l[q] & kb.EXISTING)
                elif frozen_names and not origins_l[q] & kb.EXISTING and \
                        left_groups[spec.left_group] != left_names[q]:
                    move_l = left_names[q]  # it left a group it shared with frozen glyphs
            c = rclass[i]
            if c != kb.NONE and used_r[c] and right_names[c] is not None:
                if spec.right_group == kb.NONE:
                    right = right_names[c]
                    joins = joins or bool(origins_r[c] & kb.EXISTING)
                elif frozen_names and not origins_r[c] & kb.EXISTING and \
                        right_groups[spec.right_group] != right_names[c]:
                    move_r = right_names[c]
            if left is not None or right is not None:
                groups[names[i]] = (left, right)
                group_sides += (left is not None) + (right is not None)
                joining += joins
            if move_l is not None or move_r is not None:
                moves[names[i]] = (move_l, move_r)
        new_groups = (sum(1 for c in range(len(right_names)) if used_r[c] and not origins_r[c] & kb.EXISTING) +
                      sum(1 for q in range(len(left_names)) if used_l[q] and not origins_l[q] & kb.EXISTING))

        # 5. which existing keys the solve covered: the kerned glyphs and their
        #    classes (a designer group in a glyph-pair solve only when all of its
        #    glyphs were kerned), each with the scripts it holds
        left_touch, right_touch = {}, {}

        def note(table, key, script):
            e = table.get(key)
            if e is None:
                e = table[key] = [set(), False]
            if script:
                e[0].add(script)
            else:
                e[1] = True

        full_r = full_l = set()
        if not classes:
            full_r = _fully_kerned(specs, kern, "right_group")
            full_l = _fully_kerned(specs, kern, "left_group")
        for i in range(n):
            if not i & 511:
                yield
            if not kern[i]:
                continue
            spec = specs[i]
            s = spec.script
            note(left_touch, gkey[i], s)
            note(right_touch, gkey[i], s)
            if classes:
                if rclass[i] != kb.NONE:
                    note(left_touch, rkeys[rclass[i]], s)
                if lclass[i] != kb.NONE:
                    note(right_touch, lkeys[lclass[i]], s)
            else:
                if spec.right_group in full_r:
                    note(left_touch, LEFT_PREFIX + right_groups[spec.right_group], s)
                if spec.left_group in full_l:
                    note(right_touch, RIGHT_PREFIX + left_groups[spec.left_group], s)
        self.fraction = 0.65

        def covered(lk, rk):
            a = left_touch.get(lk)
            if a is None:
                return False
            b = right_touch.get(rk)
            if b is None:
                return False
            return not scope_scripts or a[1] or b[1] or not a[0].isdisjoint(b[0])

        self.phase = "removals"
        # keys that belong to frozen glyphs: their glyph keys, and the groups
        # any frozen glyph is in (what lies between two of them stays)
        frozen_keys = set()
        if frozen_names:
            for i in range(n):
                if frozen[i]:
                    frozen_keys.add(gkey[i])
                    spec = specs[i]
                    if spec.right_group != kb.NONE:
                        frozen_keys.add(LEFT_PREFIX + right_groups[spec.right_group])
                    if spec.left_group != kb.NONE:
                        frozen_keys.add(RIGHT_PREFIX + left_groups[spec.left_group])
        removals = []
        overriding = 0
        existing_set = set(existing)
        written = set()  # the existing entries the plan overwrites
        if existing:
            for k, (lk, rk, _v) in enumerate(kerning):
                if (lk, rk) in existing_set:
                    written.add((lk, rk))
                if not k & 2047:
                    yield
            if replace:
                for k, pair in enumerate(existing):
                    if pair not in written and covered(*pair) and not (
                            pair[0] in frozen_keys and pair[1] in frozen_keys):
                        removals.append(pair)
                    if not k & 2047:
                        yield
            elif class_pairs:
                # existing glyph-level entries that will keep overriding a new class pair
                index = dict((gkey[i], i) for i in range(n) if kern[i])
                for k, (lk, rk) in enumerate(existing):
                    if not k & 2047:
                        yield
                    li, ri = index.get(lk), index.get(rk)
                    if (li is None and ri is None) or (lk, rk) in written or not covered(lk, rk):
                        continue
                    lc = rkeys[rclass[li]] if li is not None and rclass[li] != kb.NONE else lk
                    rc = lkeys[lclass[ri]] if ri is not None and lclass[ri] != kb.NONE else rk
                    if (lc, rc) in class_pairs:
                        overriding += 1
        self.fraction = 0.8

        # 6. sidebearings of the free sides, in Glyphs' own measure: the engine
        #    works on the ink of the outline, Glyphs measures along the italic
        #    angle on an italic master; the two differ by a per-glyph, per-side
        #    amount that moving the glyph does not change
        self.phase = "metrics"
        metric_rows = result.metrics
        if metrics_names is not None:
            wanted = set(metrics_names)
            in_scope = [name in wanted for name in names]
        elif _covers_font(specs, kern, metric_rows):
            in_scope = [True] * n
        else:
            in_scope = [bool(k) for k in kern]
        yield
        # a side that follows another glyph (a metrics key, an aligned component)
        # lands where that glyph's new spacing puts it: the glyphs followed are
        # re-spaced too (Ntilde in the sample text moves N)
        for i in range(n):
            if frozen[i]:
                in_scope[i] = False  # frozen: its sidebearings stay
        stack = [i for i in range(n) if in_scope[i]]
        while stack:
            spec = specs[stack.pop()]
            for rule, t in ((spec.lsb_rule, spec.lsb_glyph), (spec.rsb_rule, spec.rsb_glyph)):
                if rule in _FOLLOW and t < n and not in_scope[t] and not frozen[t]:
                    in_scope[t] = True
                    stack.append(t)
        yield
        metrics = {}
        keep_width = set()
        changing = ruled_sides = 0
        for i in range(n):
            if not i & 63:
                self.fraction = 0.8 + 0.18 * i / max(1, n)
                yield
            if not in_scope[i]:
                continue
            m = metric_rows[i]
            if not m.valid:
                continue
            spec = specs[i]
            ruled_l = bool(m.flags & METRIC_LSB_RULED) or spec.lsb_rule != kb.RULE_FREE
            ruled_r = bool(m.flags & METRIC_RSB_RULED) or spec.rsb_rule != kb.RULE_FREE
            fixed = bool(spec.flags & kb.GLYPH_FIXED_ADVANCE)
            ruled_sides += ruled_l + ruled_r
            if fixed and (ruled_l or ruled_r):
                continue  # a kept advance and a key on one side leave nothing free
            name = names[i]
            info = infos.get(name)
            ink_l, ink_r = getattr(info, "lsb", None), getattr(info, "rsb", None)
            font_l, font_r = getattr(info, "font_lsb", None), getattr(info, "font_rsb", None)
            shift_l = font_l - ink_l if font_l is not None and ink_l is not None and math.isfinite(font_l - ink_l) else 0.0
            shift_r = font_r - ink_r if font_r is not None and ink_r is not None and math.isfinite(font_r - ink_r) else 0.0
            lsb = round_units(m.lsb + shift_l) if not ruled_l and math.isfinite(m.lsb) else None
            rsb = round_units(m.rsb + shift_r) if not (ruled_r or fixed) and math.isfinite(m.rsb) else None
            if lsb is None and rsb is None:
                continue
            metrics[name] = (lsb, rsb)
            if fixed:
                keep_width.add(name)
            # the dialog's estimate, against the font as the snapshot read it
            cur_l = font_l if font_l is not None else spec.cur_lsb
            cur_r = font_r if font_r is not None else spec.cur_rsb
            if _moved(lsb, cur_l) is not None or _moved(rsb, cur_r) is not None:
                changing += 1

        # 7. glyphs that follow a re-spaced glyph are synced after the writes
        yield
        self.phase = "followers"
        index_of = dict((name, i) for i, name in enumerate(names))
        sync, follow = _followers(specs, names, [index_of[name] for name in metrics])
        if frozen_names:
            sync = [name for name in sync if name not in frozen_names]
            follow = dict((k, v) for k, v in follow.items() if k not in frozen_names)

        # big temporaries go a slice at a time (freeing them at once would
        # take as long as building them)
        for _ in _release(existing, existing_set, written):
            yield
        p.kerning = kerning
        p.groups_to_set = groups
        p.metrics = metrics
        p.keep_width = keep_width
        p.sync = sync
        p.follow = follow
        p.removals = removals
        p.groups_to_move = moves
        p.right_class_names = right_names
        p.left_class_names = left_names
        p.counts = {
            "class_pairs": cc,
            "exceptions": gc + cg + (gg if classes else 0),
            "glyph_pairs": 0 if classes else gg,
            "removals": len(removals),
            "groups": len(groups),
            "metrics": changing,
            "overriding": overriding,
            "joining_existing": joining,
            "other_masters": len(masters) - 1,
            "frozen": len(p.frozen),
            "group_moves": len(moves),
        }
        p.details = {
            "kerning": len(kerning), "class_pairs": cc, "glyph_class": gc, "class_glyph": cg, "glyph_glyph": gg,
            "zero_exceptions": zero_exceptions, "zero_class_pairs": zero_classes, "existing": len(existing),
            "removals": len(removals), "overriding": overriding, "group_glyphs": len(groups),
            "group_sides": group_sides, "new_groups": new_groups, "joining_existing": joining,
            "metric_glyphs": len(metrics), "metrics_changing": changing, "keep_width": len(keep_width),
            "ruled_sides": ruled_sides, "followers": len(sync), "classes": classes,
        }
        return p


def _release(*containers):
    """Empties lists, sets and dicts a chunk per step (a generator)."""
    for c in containers:
        if isinstance(c, list):
            while c:
                del c[-16384:]
                yield
        else:
            pop = c.popitem if isinstance(c, dict) else c.pop
            while c:
                for _ in range(min(len(c), 8192)):
                    pop()
                yield


def plan(snapshot, result, replace, metrics_names=None, scope_scripts=True, frozen=None):
    """The ApplyPlan of applying `result` (see Planner), in one call."""
    return Planner(snapshot, result, replace, metrics_names, scope_scripts, frozen).run().plan


# --------------------------------------------------------------- revert
class RevertPoint(object):
    """What an Apply changed, as it was before and as the Apply left it: the
    kerning entries it wrote or removed, the kerning groups and metrics of
    the glyphs it wrote (and of the composites it held in place), and the
    positions of those components. O(what the Apply wrote): the master's
    other kerning is never copied.

    A Restorer puts back what is still as the Apply left it; anything
    changed since (by hand, by another plugin) is kept unless the caller
    decides otherwise. `sync_names`: glyphs that follow others (metrics keys,
    aligned components), targets first: a follower is put back directly like
    any other glyph (what was captured is what Glyphs had computed), except
    an auto-aligned composite, which Glyphs re-aligns once its base is back.
    """

    def __init__(self, font, master_id, sync_names=()):
        self.font = font
        self.master_id = master_id
        self.kerning = {}  # (left key, right key) → value before Apply, None: no entry
        self.groups = {}  # name → (left group, right group)
        self.metrics = {}  # name → (LSB, RSB, width)
        self.composed = {}  # composite name → its component glyph names
        self.aligned = set()  # layers Glyphs aligns itself: never set directly
        self.components = []  # (composite, index, x, y)
        self.sync_names = list(sync_names)
        # as the Apply left them; None until it finished (then a revert
        # restores everything captured, unconditionally)
        self.applied_kerning = None  # (left key, right key) → value, None: removed
        self.applied_glyphs = None  # name → (left group, right group, LSB, width)
        self.applied_components = None  # (composite, index) → (x, y)
        self.capture_ms = 0.0

    @property
    def entry_count(self):
        return len(self.kerning)

    def capture_glyph(self, name):
        if name in self.groups:
            return
        glyph = _glyph(self.font, name)
        if glyph is None:
            return
        self.groups[name] = (_text(glyph.leftKerningGroup), _text(glyph.rightKerningGroup))
        layer = _layer(glyph, self.master_id)
        if layer is None:
            return
        comps = getattr(layer, "components", None)
        if comps:
            self.composed[name] = [_text(getattr(c, "componentName", None)) for c in comps]
            if getattr(layer, "isAligned", False):
                self.aligned.add(name)
            _refresh(glyph, layer)
        self.metrics[name] = (float(layer.LSB), float(layer.RSB), float(layer.width))

    def capture_component(self, name, k):
        comp = self._component(name, k)
        if comp is not None:
            self.components.append((name, k) + _xy(comp.position))

    def capture_kerning(self, getk, lk, rk):
        if (lk, rk) not in self.kerning:
            self.kerning[(lk, rk)] = _value(getk(self.master_id, lk, rk, LTR))

    def glyph_state(self, name):
        """(left group, right group, LSB, width) of a captured glyph now, or None."""
        glyph = _glyph(self.font, name)
        if glyph is None:
            return None
        layer = _layer(glyph, self.master_id)
        if layer is None:
            return (_text(glyph.leftKerningGroup), _text(glyph.rightKerningGroup), None, None)
        _refresh(glyph, layer)
        return (_text(glyph.leftKerningGroup), _text(glyph.rightKerningGroup), float(layer.LSB), float(layer.width))

    def _component(self, name, k):
        layer = _layer(_glyph(self.font, name), self.master_id)
        comps = getattr(layer, "components", None) if layer is not None else None
        try:
            return comps[k] if comps is not None else None
        except IndexError:
            return None

    def _order(self, names):
        """Of `names`, the glyphs that are put back directly, in restore order:
        composites before the glyphs they are built from (outermost first:
        until their components move they are exactly as Apply left them, so
        their own LSB moves back exactly), then plain glyphs. Auto-aligned
        composites are left to Glyphs."""
        depth = {}

        def depth_of(name, seen=()):
            if name not in self.composed:
                return 0
            if name not in depth:
                depth[name] = 1 + max([depth_of(c, seen + (name,)) for c in self.composed[name]
                                       if c is not None and c not in seen] or [0])
            return depth[name]

        out = [n for n in self.metrics if n in names and n not in self.aligned]
        return sorted(out, key=lambda n: -depth_of(n))

    def restorer(self, overwrite=False):
        """A Restorer for this point (see there)."""
        return Restorer(self, overwrite)

    def restore(self, overwrite=True):
        """Puts the captured state back in one call (tools and tests); with
        `overwrite`, also what was changed after the Apply. Returns counts."""
        return self.restorer(overwrite).run().counts

    def differences(self, limit=EXAMPLES):
        """(count, examples) of what differs between the font and this point;
        (0, []) right after a restore (of a point nothing changed since)."""
        font, mid = self.font, self.master_id
        getk = font.kerningForFontMasterID_leftKey_rightKey_direction_
        found = []
        count = 0
        for (lk, rk), before in self.kerning.items():
            now = _value(getk(mid, lk, rk, LTR))
            if not _same_value(now, before):
                count += 1
                if len(found) < limit:
                    found.append(("kerning", lk, rk, now, before))
        for name, (left, right) in self.groups.items():
            glyph = _glyph(font, name)
            now = (_text(glyph.leftKerningGroup), _text(glyph.rightKerningGroup)) if glyph is not None else None
            if now != (left, right):
                count += 1
                if len(found) < limit:
                    found.append(("groups", name, now, (left, right)))
        for name, saved in self.metrics.items():
            glyph = _glyph(font, name)
            layer = _layer(glyph, mid)
            if layer is not None:
                _refresh(glyph, layer)
            now = (float(layer.LSB), float(layer.RSB), float(layer.width)) if layer is not None else None
            if now is None or any(abs(x - y) > SAME_METRIC for x, y in zip(now, saved) if math.isfinite(y)):
                count += 1
                if len(found) < limit:
                    found.append(("metrics", name, now, saved))
        for name, k, x, y in self.components:
            comp = self._component(name, k)
            now = _xy(comp.position) if comp is not None else None
            if now is None or abs(now[0] - x) > 1e-6 or abs(now[1] - y) > 1e-6:
                count += 1
                if len(found) < limit:
                    found.append(("component", name, k, now, (x, y)))
        return count, found


def _same_glyph_state(a, b):
    if a is None or b is None:
        return a is None and b is None
    if a[:2] != b[:2]:
        return False
    for x, y in zip(a[2:], b[2:]):
        if x is None or y is None:
            if x is not y:
                return False
        elif math.isfinite(y) and abs(x - y) > SAME_METRIC:
            return False
    return True


class Restorer(_Stepper):
    """Puts a RevertPoint back, in slices.

    First it checks what is still as the Apply left it. If anything was
    changed since (`conflicts`), it pauses (`waiting`) for the caller's
    resolve(overwrite): False keeps those later changes and restores the
    rest, True puts everything back as it was before the Apply. A point of
    an Apply that did not finish has no record of what it left and is put
    back entirely. Undo registration is off while it writes, and the undo
    actions recorded before (on the font and on the glyphs it writes) are
    dropped. `counts` once done."""

    def __init__(self, point, overwrite=False):
        _Stepper.__init__(self)
        self.point = point
        self.overwrite = bool(overwrite)
        self.conflicts = {"kerning": 0, "glyphs": 0, "components": 0}
        self.examples = []
        self.counts = None
        self.text = "Checking what changed since Apply"
        self._undo = None

    @property
    def conflict_count(self):
        return sum(self.conflicts.values())

    def resolve(self, overwrite):
        """The caller's answer to the conflicts: carry on."""
        self.overwrite = bool(overwrite)
        self.waiting = False

    def _begin_slice(self):
        self.point.font.disableUpdateInterface()
        self._undo = _UndoOff(self.point.font)

    def _end_slice(self):
        undo, self._undo = self._undo, None
        try:
            undo.close()
        finally:
            self.point.font.enableUpdateInterface()

    def _conflict(self, kind, *what):
        self.conflicts[kind] += 1
        if len(self.examples) < EXAMPLES:
            self.examples.append((kind,) + what)

    def _work(self):
        point = self.point
        font, mid = point.font, point.master_id
        getk = font.kerningForFontMasterID_leftKey_rightKey_direction_
        setk = font.setKerningForFontMasterID_leftKey_rightKey_value_direction_
        remove = font.removeKerningForFontMasterID_leftKey_rightKey_direction_
        counts = {"kerning_set": 0, "kerning_removed": 0, "groups": 0, "metrics": 0, "components": 0, "synced": 0,
                  "kept_kerning": 0, "kept_glyphs": 0, "kept_components": 0}
        keys = list(point.kerning)
        names = list(point.metrics) + [n for n in point.groups if n not in point.metrics]
        total = max(1, len(keys) + 2 * len(names) + len(point.components))
        done = 0

        # 1. what is still as the Apply left it (all of it without a record)
        self.phase = "check"
        now_kerning = {}
        changed_keys, changed_glyphs, changed_comps = set(), set(), set()
        checked = point.applied_kerning is not None
        for k, key in enumerate(keys):
            now = _value(getk(mid, key[0], key[1], LTR))
            now_kerning[key] = now
            if checked and not _same_value(now, point.applied_kerning.get(key)):
                changed_keys.add(key)
                self._conflict("kerning", key[0], key[1], point.applied_kerning.get(key), now)
            if not k & 63:
                self.fraction = 0.3 * k / max(1, len(keys))
                yield
        if checked and point.applied_glyphs is not None:
            for k, name in enumerate(names):
                if not _same_glyph_state(point.glyph_state(name), point.applied_glyphs.get(name)):
                    changed_glyphs.add(name)
                    self._conflict("glyphs", name)
                if not k & 15:
                    yield
            for name, k, _x, _y in point.components:
                comp = point._component(name, k)
                applied = (point.applied_components or {}).get((name, k))
                now = _xy(comp.position) if comp is not None else None
                if name in changed_glyphs or now is None or applied is None or \
                        abs(now[0] - applied[0]) > 1e-6 or abs(now[1] - applied[1]) > 1e-6:
                    changed_comps.add((name, k))
                    if name not in changed_glyphs:
                        self._conflict("components", name, k)
                yield
        if self.conflict_count and not self.overwrite:
            self.text = "Changed since Apply: %d" % self.conflict_count
            yield _DECIDE
        if self.overwrite:
            changed_keys, changed_glyphs, changed_comps = set(), set(), set()
        counts["kept_kerning"] = len(changed_keys)
        counts["kept_glyphs"] = len(changed_glyphs)
        counts["kept_components"] = len(changed_comps)
        self.text = "Reverting"
        done = len(keys)

        # 2. groups, then metrics (composites before their bases), then the
        #    components every base moved back dragged along, then Glyphs'
        #    own alignment
        self.phase = "groups"
        for name, (left, right) in point.groups.items():
            if name in changed_glyphs:
                continue
            glyph = _glyph(font, name)
            if glyph is None:
                continue
            self._undo.add(glyph)
            if _text(glyph.leftKerningGroup) != left:
                glyph.leftKerningGroup = left
                counts["groups"] += 1
            if _text(glyph.rightKerningGroup) != right:
                glyph.rightKerningGroup = right
                counts["groups"] += 1
            done += 1
            self.fraction = done / total
            yield
        self.phase = "metrics"
        restore = set(n for n in point.metrics if n not in changed_glyphs)
        for name in point._order(restore):
            if self._restore_metrics(name):
                counts["metrics"] += 1
            done += 1
            self.fraction = done / total
            yield
        self.phase = "components"
        for name, k, x, y in point.components:
            if (name, k) in changed_comps:
                continue
            comp = point._component(name, k)
            if comp is not None and _xy(comp.position) != (x, y):
                self._undo.add(_glyph(font, name))
                comp.position = (x, y)
                counts["components"] += 1
            yield
        self.phase = "sync"
        for name in point.sync_names:
            if name in point.aligned and name in restore:
                glyph = _glyph(font, name)
                layer = _layer(glyph, mid)
                if layer is not None and hasattr(layer, "alignComponents"):
                    self._undo.add(glyph)
                    layer.alignComponents()
                    _refresh(glyph, layer)
                    counts["synced"] += 1
                yield

        # 3. kerning
        self.phase = "kerning"
        for k, key in enumerate(keys):
            if key in changed_keys:
                continue
            before = point.kerning[key]
            if not _same_value(now_kerning[key], before):
                if before is None:
                    remove(mid, key[0], key[1], LTR)
                    counts["kerning_removed"] += 1
                else:
                    setk(mid, key[0], key[1], before, LTR)
                    counts["kerning_set"] += 1
            if not k & 63:
                self.fraction = (done + k) / total
                yield

        # 4. undo actions recorded before the revert would replay old values
        self.phase = "undo"
        self._undo.forget(font)
        for k, name in enumerate(restore | set(n for n in point.groups if n not in changed_glyphs)):
            self._undo.forget(_glyph(font, name))
            if not k & 63:
                yield
        return counts

    def _restore_metrics(self, name):
        """Puts a layer's LSB and width back; the RSB follows. Setting the
        saved LSB itself (not a whole-unit move) also takes back the last-bit
        rounding a move across a power of two leaves in Glyphs' setter. A
        composite is read before any glyph it is built from moves back (see
        RevertPoint._order), so its cached LSB is still its own. True if
        anything was set."""
        point = self.point
        glyph = _glyph(point.font, name)
        layer = _layer(glyph, point.master_id)
        if layer is None:
            return False
        self._undo.add(glyph)
        lsb, _rsb, width = point.metrics[name]
        changed = False
        cur = float(layer.LSB)
        if math.isfinite(lsb) and math.isfinite(cur) and abs(cur - lsb) > 1e-9:
            layer.LSB = lsb
            changed = True
        if abs(float(layer.width) - width) > 1e-9:
            layer.width = width
            changed = True
        return changed

    def _finished(self, value):
        self.counts = dict(value or {})
        self.counts["conflicts"] = self.conflict_count
        self.counts["restore_ms"] = self.busy_ms
        self.counts["slowest_slice"] = {"ms": self.slice_ms[0], "phases": self.slice_ms[1],
                                        "end_slice_ms": self.end_slice_ms}


# ---------------------------------------------------------------- apply
class Applier(_Stepper):
    """Carries out a plan on the snapshot's master, in slices.

    Order: revert point, groups, sidebearings (holding composites rigid),
    syncing glyphs that follow the re-spaced ones, removals, kerning; then
    what the Apply left is recorded (for a later revert), up to 500 kerning
    entries (and 100 removals) and every written sidebearing are read back,
    and the undo actions recorded before (on the font and on the glyphs
    written) are dropped: replayed on top of the Apply they would bring back
    single values from before it. A failure halfway is reported in the
    summary ("error") rather than raised; `revert` then undoes the part
    already written. `summary` and `revert` once done.
    """

    def __init__(self, font, snapshot, plan):
        _Stepper.__init__(self)
        if snapshot is not None and snapshot.master_id != plan.master_id:
            raise ValueError("the plan is for master %s, the snapshot for %s" % (plan.master_id, snapshot.master_id))
        self.font = font
        self.snapshot = snapshot
        self.plan = plan
        self.summary = None
        self.revert = None
        self.error = None
        self.text = "Applying"
        self._undo = None
        self._counts = {"kerning": 0, "removed": 0, "groups": 0, "respaced": 0, "synced": 0}
        self._detail = {"group_conflicts": 0, "missing_glyphs": 0, "metric_sides": 0, "composites": 0,
                        "components_held": 0, "realigned": 0, "readback_metrics": 0, "readback_kerning": 0,
                        "readback_removals": 0}
        self._examples = []
        self._bad = [0, 0]  # metric, kerning mismatches

    def _begin_slice(self):
        self.font.disableUpdateInterface()
        self._undo = _UndoOff(self.font)

    def _end_slice(self):
        undo, self._undo = self._undo, None
        try:
            undo.close()
        finally:
            self.font.enableUpdateInterface()

    def _failed(self, error):
        self.error = error
        print("Kinetikern2 apply failed:\n" + error)
        return True

    def _held(self, users, name, shift):
        """The outline of `name` moved by `shift`: its components move back."""
        if not shift:
            return
        for composite, _k, comp, dx, dy in users.get(name, ()):
            self._undo.add(_glyph(self.font, composite))
            x, y = _xy(comp.position)
            comp.position = (x - dx * shift, y - dy * shift)
            self._detail["components_held"] += 1

    def _work(self):
        font, p = self.font, self.plan
        mid = p.master_id
        detail, counts = self._detail, self._counts
        total = max(1, 2 * (len(p.groups_to_set) + len(p.metrics) + len(p.sync)) + len(p.removals) +
                    len(p.kerning) // 4 + 1)
        done = 0

        # 1. the components drawing a glyph this apply may move (a new LSB, a key sync)
        self.phase = "composites"
        self.text = "Applying: preparing"
        names = set(p.metrics)
        names.update(p.groups_to_set)
        names.update(getattr(p, "groups_to_move", {}))
        names.update(p.sync)
        users = {}
        if p.metrics:
            bases = set(p.metrics) | set(p.sync)
            candidates = None
            composites_of = getattr(self.snapshot, "composites_of", None)
            if composites_of is not None:
                candidates = composites_of(bases)
            yield from _component_users(font, mid, bases, candidates, users)
        held = [(c, k) for row in users.values() for (c, k, _comp, _dx, _dy) in row]
        composites = sorted(set(c for c, _k in held))
        names.update(composites)
        detail["composites"] = len(composites)
        yield

        # 2. the revert point: everything this apply writes, as it is now
        self.phase = "revert_point"
        self.text = "Applying: saving the revert point"
        revert = RevertPoint(font, mid, sync_names=p.sync)
        for name in sorted(names):
            revert.capture_glyph(name)
            yield
        for k, (c, j) in enumerate(held):
            revert.capture_component(c, j)
            if not k & 15:
                yield
        getk = font.kerningForFontMasterID_leftKey_rightKey_direction_
        for k, (lk, rk, _v) in enumerate(p.kerning):
            revert.capture_kerning(getk, lk, rk)
            if not k & 63:
                yield
        for k, (lk, rk) in enumerate(p.removals):
            revert.capture_kerning(getk, lk, rk)
            if not k & 63:
                yield
        revert.sync_names = [name for name in p.sync if name in revert.metrics]
        revert.capture_ms = self.ms.get("revert_point", 0.0)
        self.revert = revert  # from here on the font changes

        # 3. groups
        self.phase = "groups"
        self.text = "Applying: kerning groups"
        for name, (left, right) in p.groups_to_set.items():
            glyph = _glyph(font, name)
            if glyph is None:
                detail["missing_glyphs"] += 1
                continue
            self._undo.add(glyph)
            for side, want in (("leftKerningGroup", left), ("rightKerningGroup", right)):
                if want is None:
                    continue
                now = _text(getattr(glyph, side))
                if now is None:
                    setattr(glyph, side, want)
                    counts["groups"] += 1
                elif now != want:
                    detail["group_conflicts"] += 1
                    if len(self._examples) < EXAMPLES:
                        self._examples.append(("group conflict", name, side, now, want))
            done += 1
            self.fraction = done / total
            yield
        # glyphs leaving a group they shared with frozen glyphs (spacing groups)
        for name, (left, right) in getattr(p, "groups_to_move", {}).items():
            glyph = _glyph(font, name)
            if glyph is None:
                detail["missing_glyphs"] += 1
                continue
            self._undo.add(glyph)
            for side, want in (("leftKerningGroup", left), ("rightKerningGroup", right)):
                if want is not None and _text(getattr(glyph, side)) != want:
                    setattr(glyph, side, want)
                    counts["groups"] += 1
            yield

        # 4. sidebearings
        self.phase = "metrics"
        self.text = "Applying: sidebearings"
        written = []
        for name, (lsb, rsb) in p.metrics.items():
            if self._write_metrics(name, lsb, rsb, users, written):
                counts["respaced"] += 1
            done += 2
            self.fraction = done / total
            yield

        # 5. glyphs that follow the ones that moved
        self.phase = "sync"
        self.text = "Applying: metrics keys and aligned composites"
        reached = set(w[0] for w in written)
        for name in p.sync:
            self._sync_one(name, reached, users, revert)
            done += 2
            self.fraction = done / total
            yield

        # 6. removals and kerning
        self.phase = "removals"
        self.text = "Applying: kerning"
        remove = font.removeKerningForFontMasterID_leftKey_rightKey_direction_
        for k, (lk, rk) in enumerate(p.removals):
            remove(mid, lk, rk, LTR)
            counts["removed"] += 1
            if not k & 31:
                self.fraction = (done + k) / total
                yield
        done += len(p.removals)
        self.phase = "kerning"
        setk = font.setKerningForFontMasterID_leftKey_rightKey_value_direction_
        for k, (lk, rk, v) in enumerate(p.kerning):
            setk(mid, lk, rk, v, LTR)
            counts["kerning"] += 1
            if not k & 127:
                self.fraction = (done + k // 4) / total
                yield

        # 7. what the apply left, for a revert that keeps later changes
        self.phase = "record"
        self.text = "Applying: checking"
        applied = {}
        for k, (lk, rk, v) in enumerate(p.kerning):
            applied[(lk, rk)] = float(v)
            if not k & 4095:
                yield
        for lk, rk in p.removals:
            applied[(lk, rk)] = None
        glyphs = {}
        for name in list(revert.groups):
            glyphs[name] = revert.glyph_state(name)
            yield
        comps = {}
        for n, (name, k, _x, _y) in enumerate(revert.components):
            comp = revert._component(name, k)
            if comp is not None:
                comps[(name, k)] = _xy(comp.position)
            if not n & 15:
                yield
        revert.applied_kerning, revert.applied_glyphs, revert.applied_components = applied, glyphs, comps

        # 8. read-back
        self.phase = "read_back"
        for k, row in enumerate(written):
            self._bad[0] += self._read_back_metrics(row)
            if not k & 15:
                yield
        k = len(p.kerning)
        stride = max(1, -(-k // READBACK_KERNING))
        for idx in range(0, k, stride):
            lk, rk, want = p.kerning[idx]
            have = _value(getk(mid, lk, rk, LTR))
            detail["readback_kerning"] += 1
            if have is None or abs(have - want) > SAME:
                self._bad[1] += 1
                if len(self._examples) < EXAMPLES:
                    self._examples.append(("kerning", lk, rk, want, have))
            if not idx & 63:
                yield
        r = len(p.removals)
        stride = max(1, -(-r // READBACK_REMOVALS))
        for idx in range(0, r, stride):
            lk, rk = p.removals[idx]
            have = _value(getk(mid, lk, rk, LTR))
            detail["readback_removals"] += 1
            if have is not None:
                self._bad[1] += 1
                if len(self._examples) < EXAMPLES:
                    self._examples.append(("removal", lk, rk, None, have))

        # 9. undo actions recorded before the apply would replay old values
        self.phase = "undo"
        self._undo.forget(font)
        for k, name in enumerate(sorted(revert.groups)):
            self._undo.forget(_glyph(font, name))
            if not k & 63:
                yield

    def _write_metrics(self, name, lsb, rsb, users, written):
        """Moves the free sides to the plan's targets (whole units, see
        _moved); every LSB move holds the composites drawing the glyph in
        place, so their cached metrics (read fresh by the RevertPoint) stay
        true. The RSB moves through the width (64 µs instead of the RSB
        setter's 350). True if a side moved."""
        glyph = _glyph(self.font, name)
        layer = _layer(glyph, self.plan.master_id)
        if layer is None:
            self._detail["missing_glyphs"] += 1
            return False
        self._undo.add(glyph)
        sides = 0
        width = float(layer.width) if name in self.plan.keep_width else None
        cur = float(layer.LSB)
        new = _moved(lsb, cur)
        if new is not None:
            layer.LSB = new
            if math.isfinite(cur):
                self._held(users, name, new - cur)
            sides += 1
        if width is not None and abs(float(layer.width) - width) > 1e-9:
            layer.width = width
        cur = float(layer.RSB)
        new = _moved(rsb, cur)
        if new is not None:
            if math.isfinite(cur):
                layer.width = float(layer.width) + (new - cur)
            else:
                layer.RSB = new
            sides += 1
        if sides:
            written.append((name, glyph, layer, width))
            self._detail["metric_sides"] += sides
        return bool(sides)

    def _sync_one(self, name, reached, users, revert):
        """Lets Glyphs recompute a glyph that follows a glyph that moved
        (targets come first in plan.sync, so chains settle in one pass):
        syncMetrics for metrics keys, alignComponents for auto-aligned
        composites. Wherever that moves the glyph's outline, the components
        drawing it are held like after any other move."""
        if not any(t in reached for t in self.plan.follow.get(name, ())):
            return
        reached.add(name)
        glyph = _glyph(self.font, name)
        layer = _layer(glyph, self.plan.master_id)
        if layer is None:
            return
        self._undo.add(glyph)
        if _has_metrics_key(glyph, layer):
            before = float(layer.LSB)
            layer.syncMetrics()
            after = float(layer.LSB)
            if math.isfinite(before) and math.isfinite(after):
                self._held(users, name, after - before)
            self._counts["synced"] += 1
        elif getattr(layer, "isAligned", False) and hasattr(layer, "alignComponents"):
            layer.alignComponents()
            _refresh(glyph, layer)
            self._detail["realigned"] += 1
            # its outline moved with its base (and the alignment): measured
            # against its LSB from before the Apply
            saved = revert.metrics.get(name)
            after = float(layer.LSB)
            if saved is not None and math.isfinite(saved[0]) and math.isfinite(after):
                self._held(users, name, after - saved[0])

    def _read_back_metrics(self, row):
        """A written glyph ends within half a unit of its targets (whole-unit
        moves from a fractional side) and a kept advance is exactly as it was.
        (Each was written through its own setter, which recomputes its cache.)
        Returns the number of sides that do not."""
        name, _glyph_, layer, width = row
        lsb, rsb = self.plan.metrics[name]
        bad = 0
        checks = (("LSB", lsb, layer.LSB, 0.5), ("RSB", rsb, layer.RSB, 0.5), ("width", width, layer.width, 0.0))
        for side, want, have, tolerance in checks:
            if want is None:
                continue
            self._detail["readback_metrics"] += 1
            if have is None or not abs(float(have) - want) <= tolerance + 1e-6:
                bad += 1
                if len(self._examples) < EXAMPLES:
                    self._examples.append(("metrics", name, side, want, have))
        return bad

    def _finished(self, value):
        p, counts, detail = self.plan, self._counts, self._detail
        error = self.error
        metric_bad, kerning_bad = self._bad
        ms = dict(self.ms)
        ms["total"] = self.busy_ms
        ms["wall"] = 1000.0 * (time.perf_counter() - self.started)
        # flat ints and one *_ms float: the window's status line lists them
        summary = {
            "ok": error is None and not metric_bad and not kerning_bad,
            "kerning_entries": counts["kerning"],
            "entries_removed": counts["removed"],
            "group_sides_set": counts["groups"],
            "glyphs_respaced": counts["respaced"],
            "keyed_glyphs_synced": counts["synced"],
            "metric_mismatches": metric_bad,
            "kerning_mismatches": kerning_bad,
            "apply_ms": self.busy_ms,
        }
        if detail["group_conflicts"]:
            summary["group_conflicts"] = detail["group_conflicts"]  # glyphs that got a group since the snapshot
        if detail["missing_glyphs"]:
            summary["missing_glyphs"] = detail["missing_glyphs"]
        if error is not None:
            summary["write_errors"] = 1  # the status line shows it; the traceback is in "error" and the Macro panel
        summary["master_id"] = p.master_id
        summary["error"] = error
        summary["examples"] = self._examples
        summary["details"] = detail
        summary["counts"] = dict(p.counts)
        summary["ms"] = ms
        summary["slowest_slice"] = {"ms": self.slice_ms[0], "phases": self.slice_ms[1],
                                    "end_slice_ms": self.end_slice_ms}
        self.summary = summary


def apply(font, snapshot, plan):
    """Carries out a plan in one call (see Applier). Returns (summary, RevertPoint)."""
    applier = Applier(font, snapshot, plan).run()
    return applier.summary, applier.revert


def _n(x):
    return "{:,}".format(x)


def describe_plan(plan):
    """The confirmation text for a plan (one fact per line)."""
    c = plan.counts
    kinds = [(c.get("class_pairs", 0), "class pairs"), (c.get("exceptions", 0), "exceptions"),
             (c.get("glyph_pairs", 0), "glyph pairs")]
    parts = ["%s %s" % (_n(v), label) for v, label in kinds if v]
    lines = ["Kerning: %s." % (", ".join(parts) if parts else "nothing to write")]
    if plan.replace and c.get("removals"):
        lines.append("%s existing entries between these glyphs and their groups are removed." % _n(c["removals"]))
    if not plan.replace and c.get("overriding"):
        lines.append("%s existing glyph pairs keep overriding new class pairs (Replace would remove them)."
                     % _n(c["overriding"]))
    if c.get("frozen"):
        lines.append("%s frozen glyphs are left as they are (sidebearings, groups and the kerning between them)."
                     % _n(c["frozen"]))
    if c.get("groups"):
        lines.append("%s glyphs join kerning groups (only sides without a group)." % _n(c["groups"]))
    if c.get("group_moves"):
        lines.append("%s glyphs leave a kerning group they shared with frozen glyphs for a group of their own."
                     % _n(c["group_moves"]))
    if c.get("joining_existing") and c.get("other_masters"):
        lines.append("%s of them join existing groups. Groups belong to the glyph, not the master: in the other "
                     "%s, these glyphs take on the kerning of the groups they join."
                     % (_n(c["joining_existing"]), "master" if c["other_masters"] == 1
                        else "%s masters" % _n(c["other_masters"])))
    lines.append("%s glyphs get new sidebearings; sides driven by metrics keys or aligned components follow "
                 "Glyphs." % _n(c.get("metrics", 0)))
    lines.append("Undo does not cover Apply, and the undo history of the kerning and of the glyphs Apply writes is "
                 "cleared. Revert Last Apply puts back what Apply changed.")
    return "\n".join(lines)


def describe_conflicts(restorer):
    """The question for a Restorer that found changes made after the Apply."""
    c = restorer.conflicts
    parts = []
    if c["kerning"]:
        parts.append("%s kerning entries" % _n(c["kerning"]))
    if c["glyphs"]:
        parts.append("%s glyphs (groups or sidebearings)" % _n(c["glyphs"]))
    if c["components"]:
        parts.append("%s component positions" % _n(c["components"]))
    return ("Since the last Apply, %s were changed in Glyphs. Revert can keep those changes and put back only the "
            "rest, or put everything back as it was before the Apply (the later changes are lost; Undo does not "
            "cover Revert)." % ", ".join(parts))


def describe_summary(summary):
    """One status line for an apply summary."""
    if summary.get("error"):
        return "Apply stopped with an error (see the Macro panel); Revert Last Apply undoes what was written."
    text = ("%s kerning entries written, %s removed, %s group sides set, %s glyphs re-spaced in %.2f s"
            % (_n(summary["kerning_entries"]), _n(summary["entries_removed"]), _n(summary["group_sides_set"]),
               _n(summary["glyphs_respaced"]), summary["apply_ms"] / 1000.0))
    bad = summary["metric_mismatches"] + summary["kerning_mismatches"]
    if bad:
        text += "; %s values read back differently" % _n(bad)
    if summary.get("group_conflicts"):
        text += "; %s glyphs kept a group they got after the preview" % _n(summary["group_conflicts"])
    return text + "."
