# encoding: utf-8
"""
kk2_apply — writes a solve into a UFO font, and takes it back.

RoboFont's counterpart of the Glyphs plugin's kk2_apply. A Planner turns a
Result into the exact writes an Apply makes (kerning pairs and values, new
kerning-group memberships, sidebearings, removed pairs) without touching the
font, so the window can say what will happen before it happens. An Applier
carries a plan out; the RevertPoint it takes first lets a Restorer put back
what the Apply changed (Revert Last Apply: RoboFont's undo is per glyph and
does not cover kerning and groups, so it does not cover an Apply either).

All three work in slices: step(budget_s) does at most about `budget_s` of
work and returns, so the window's timer can drive them between events and
show progress. The notifications of the glyphs Apply and Revert edit are
held (glyph by glyph) and posted in batches sized from what they cost
RoboFont (see _Held): it redraws its whole font overview after every batch,
so a batch per slice would leave it redrawing most of the time. Kerning is written in one batch
(the table rebuilt with the removals out and the new pairs in: two
notifications however many pairs), groups likewise. plan(), apply() and RevertPoint.restore() run a stepper to the end
in one call (tools and tests).

Main thread only. Everything is done on the font's defcon objects (what
RoboFont edits) in its default layer. Ink is measured fresh from the points
(kk2_snapshot.ink_metrics), never from bounds a held notification has not
refreshed yet.

Kerning keys are the UFO's: a glyph is its name; a class on the left of a
pair is "public.kern1.<group>" (its glyphs' right side), on the right
"public.kern2.<group>". Precedence in a UFO and in the engine is the same:
glyph–glyph, glyph–class, class–glyph, class–class.

Sides that follow other glyphs (a metrics key a UFO carries from Glyphs, an
aligned composite) are written as the engine resolved them: RoboFont does
not recompute them. A glyph a re-spaced glyph reaches through such a rule is
re-spaced with it (Ntilde moves with N).

Composites stay rigid. A component draws its base glyph's outline where the
base has it, so moving a base would move that part inside every composite
built from it: the accent of Aacute would slide off its A. Whenever Apply
moves an outline it therefore moves each component drawing it back by the
same amount; a composite that is re-spaced itself then moves as a whole.
An accented glyph the snapshot spaces as aligned follows its base exactly
this way: its base component back at the origin, its marks moved with the
base's outline, the base's new advance (what Glyphs' alignment gives).
"""

from __future__ import division, print_function, unicode_literals

import math
import time
import traceback

import kk2_bridge as kb
import kk2_host as host
from kk2_snapshot import ink_metrics

# KK2Metrics.flags: the side follows a rule (a metrics key, an aligned
# component, a frozen glyph) instead of Pass 1
METRIC_LSB_RULED = 1
METRIC_RSB_RULED = 2

LEFT_PREFIX = "public.kern1."  # class on the left of a pair: its glyphs' right-side group
RIGHT_PREFIX = "public.kern2."  # class on the right of a pair: its glyphs' left-side group
NEW_GROUP_SUFFIX = ".kk2"
READBACK_KERNING = 500
READBACK_REMOVALS = 100
EXAMPLES = 20
SAME = 1e-6  # a kerning value read back equals the one written
SAME_METRIC = 1e-3  # a sidebearing or width is still the one Apply left

_FOLLOW = (kb.RULE_FOLLOW_SAME, kb.RULE_FOLLOW_OPPOSITE)
_DECIDE = object()  # a Restorer's work yields this when it needs the caller's decision
_PAUSE = object()  # the work yields this to end the slice early (a batch of notifications is due)


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


def _value(v):
    """A kerning value as read from the font, or None for "no entry"."""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def pair_value(kerning, pair):
    """The value of a kerning pair, or None when the font has no such pair.
    (defcon's Kerning.get answers 0 for a missing pair: never used here.)"""
    return _value(kerning[pair]) if pair in kerning else None


def _same_value(a, b):
    """Two kerning values (None: no entry) are the same entry."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= SAME


def _number(v):
    """A kerning value as the UFO keeps it: an int when it is one."""
    v = float(v)
    return int(v) if v == int(v) else v


def _layer(dfont, layer_name=None):
    layers = dfont.layers
    if layer_name is not None and layer_name in layers:
        return layers[layer_name]
    return layers.defaultLayer


def _glyph(layer, name):
    return layer[name] if name in layer else None


def _move_glyph(glyph, dx):
    """Moves a glyph's outline, components, anchors and guidelines by dx (what
    RoboFont's margin setters move)."""
    if not dx:
        return
    for contour in glyph:
        contour.move((dx, 0))
    for component in glyph.components:
        component.move((dx, 0))
    for anchor in glyph.anchors:
        anchor.move((dx, 0))
    for guideline in getattr(glyph, "guidelines", ()):
        if guideline.x is not None:
            guideline.x = guideline.x + dx


def _points_of(contour):
    points = getattr(contour, "_points", None)  # defcon's own list: no copy
    return points if points is not None else list(contour)


def _xs_of(glyph):
    """The x of every point, anchor and guideline of a glyph and its
    components' x offsets: what a move changes. A move there and back can
    end a unit in the last place off (509.521 + 3 - 3 is 509.52099999999996,
    which a .glif would keep), so a Revert puts these back instead."""
    return (tuple(tuple(p.x for p in _points_of(c)) for c in glyph),
            tuple(a.x for a in glyph.anchors),
            tuple(g.x for g in getattr(glyph, "guidelines", ())),
            tuple(c.transformation[4] for c in glyph.components))


def _unmove(glyph, saved, d):
    """Undoes a move of `glyph` by `d` units to the last bit: every x back to
    `saved` (_xs_of before the move). Only if the glyph is exactly as that
    move left it (the same points, each at its saved x + d); else it changes
    nothing and returns False. A component offset that more than the move
    changed (one held in place when its base moved) is moved back by d: the
    Restorer puts the offset it captured back afterwards."""
    contours, anchors, guides, comps = saved
    shapes = list(glyph)
    if len(shapes) != len(contours):
        return False
    points = []
    for contour, xs in zip(shapes, contours):
        pts = _points_of(contour)
        if len(pts) != len(xs) or any(p.x != x + d for p, x in zip(pts, xs)):
            return False
        points.append(pts)
    marks = list(glyph.anchors)
    if len(marks) != len(anchors) or any(a.x != x + d for a, x in zip(marks, anchors)):
        return False
    lines = list(getattr(glyph, "guidelines", ()))
    if len(lines) != len(guides) or any(x is not None and g.x != x + d for g, x in zip(lines, guides)):
        return False
    parts = list(glyph.components)
    if len(parts) != len(comps):
        return False
    for contour, pts, xs in zip(shapes, points, contours):
        for p, x in zip(pts, xs):
            p.x = x
        contour.postNotification("Contour.PointsChanged")  # as fontParts does after a point edit
        contour.dirty = True
    for a, x in zip(marks, anchors):
        a.x = x
    for g, x in zip(lines, guides):
        if x is not None:
            g.x = x
    for comp, x in zip(parts, comps):
        t = comp.transformation
        if t[4] == x + d:
            comp.transformation = (t[0], t[1], t[2], t[3], x, t[5])
        else:
            comp.move((-d, 0))
    return True


def _offset(component):
    t = component.transformation
    return float(t[4]), float(t[5])


def _raw_offset(component):
    """A component's offset as the font has it (100 stays 100, 100.0 stays
    100.0: a .glif writes them differently)."""
    t = component.transformation
    return t[4], t[5]


def _set_offset(component, x, y):
    """Sets a component's offset to exactly (x, y), number types included."""
    t = component.transformation
    if (t[4], t[5]) != (x, y):
        component.transformation = (t[0], t[1], t[2], t[3], x, y)


class GroupIndex(object):
    """The kerning groups of a font, kept current while an Apply or a Revert
    edits them: which public.kern2 group (left side) and public.kern1 group
    (right side) each glyph is in, by short name. Edits go to a working copy
    and reach the font in one update (flush)."""

    def __init__(self, dfont):
        self.groups = dfont.groups
        self.left, self.right, _doubles = _maps(self.groups)
        self.members = {}  # full group name → working member list (only the groups edited)
        self.created = set()  # full names of groups that did not exist before
        self.deleted = set()

    def of(self, name):
        """(left group, right group) of a glyph: short names, None for none."""
        return self.left.get(name), self.right.get(name)

    def _list(self, full):
        members = self.members.get(full)
        if members is None:
            if full in self.groups:
                members = [str(m) for m in self.groups[full]]
            else:
                members = []
                self.created.add(full)
            self.members[full] = members
        return members

    def set(self, name, side, group):
        """Puts glyph `name`'s `side` ("left" or "right") into `group` (short
        name; None: into no group). True if anything changed."""
        table, prefix = (self.left, RIGHT_PREFIX) if side == "left" else (self.right, LEFT_PREFIX)
        now = table.get(name)
        if now == group:
            return False
        if now is not None:
            members = self._list(prefix + now)
            while name in members:
                members.remove(name)
            del table[name]
        if group is not None:
            members = self._list(prefix + group)
            if name not in members:
                members.append(name)
            table[name] = group
        return True

    def drop_if_empty(self, full):
        members = self.members.get(full)
        if members is not None and not members:
            self.deleted.add(full)

    def flush(self):
        """Writes the edited groups into the font (one update, plus a delete
        per group emptied that Apply had created)."""
        changed = dict((full, list(m)) for full, m in self.members.items() if full not in self.deleted)
        if changed:
            self.groups.update(changed)
        for full in self.deleted:
            if full in self.groups:
                del self.groups[full]
        self.members = {}
        self.deleted = set()


def _maps(groups):
    left, right = {}, {}
    doubles = []
    for full in sorted(groups.keys()):
        full = str(full)
        if full.startswith(RIGHT_PREFIX):
            table, short = left, full[len(RIGHT_PREFIX):]
        elif full.startswith(LEFT_PREFIX):
            table, short = right, full[len(LEFT_PREFIX):]
        else:
            continue
        for member in groups[full]:
            member = str(member)
            if member in table:
                doubles.append(member)
                continue
            table[member] = short
    return left, right, doubles


def _component_users(layer, bases, candidates=None, users=None):
    """Fills `users` (a new dict if None) with {base glyph: [(composite,
    component index, dx, dy)]} for every component in the layer that draws
    one of `bases`; (dx, dy) is where a base outline moved by one unit along
    x lands in the composite (the component's first matrix column).
    `candidates`: names of the glyphs that may hold such components (the
    snapshot knows them); None looks at every glyph. A generator: it yields
    after every few glyphs, so the Applier can slice it."""
    if users is None:
        users = {}
    if not bases:
        return users
    names = sorted(candidates) if candidates is not None else list(layer.keys())
    for n, name in enumerate(names):
        if not n & 15:
            yield
        glyph = _glyph(layer, name)
        if glyph is None:
            continue
        for k, comp in enumerate(glyph.components):
            if comp.baseGlyph not in bases:
                continue
            t = comp.transformation
            users.setdefault(comp.baseGlyph, []).append((name, k, float(t[0]), float(t[1])))
    return users


def font_kerning(font):
    """A plain-Python copy of the font's kerning: {(left, right): value}. For
    tools and tests: O(pairs)."""
    return dict(((str(a), str(b)), float(v)) for (a, b), v in host.naked(font).kerning.items())


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
        self.end_slice_ms = 0.0  # longest _end_slice (posting the held notifications)
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
                if out is _PAUSE or now >= deadline:
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


class _Held(object):
    """The notifications of the glyphs being edited, held glyph by glyph and
    posted in batches. RoboFont redraws its font overview after each batch:
    every cell on screen, and the cells of the glyphs the batch changed drawn
    anew (from a fraction of a millisecond to several under Rosetta, for a
    composite of quadratic curves), so batches per slice (80 a second) kept
    it redrawing most of the time, and a large batch of glyphs on screen
    stalls it. A batch is therefore sized from what the previous ones cost
    RoboFont (posting them, then the redraw until the next slice): about
    TARGET_S, FEWEST to MOST glyphs, FIRST before anything is measured; and
    posted at least every BATCH_S, to show progress.

    Held per glyph, not for the whole font: defcon compares each
    notification with the ones held under the same key before keeping it,
    which over a whole batch's would grow with its square. Kerning and group
    writes are never made while held: they are batched instead."""

    BATCH_S = 0.5
    TARGET_S = 0.15
    FIRST, FEWEST, MOST = 30, 6, 60

    def __init__(self, dfont):
        self.dispatcher = getattr(dfont, "dispatcher", None)
        self.glyphs = {}  # id → glyph, held until the batch is posted
        self.since = None
        self.batch = self.FIRST
        self.per_glyph = None  # seconds a glyph of a batch costs RoboFont, as measured
        self.batches = 0
        self.costliest = 0.0  # seconds, the batch that cost RoboFont most
        self._posted = None  # (glyphs, seconds posting them, when that ended) until the next slice

    def hold(self, glyph):
        """Holds the notifications of `glyph` (a defcon glyph) until release()."""
        if self.dispatcher is None or glyph is None or id(glyph) in self.glyphs:
            return
        self.dispatcher.holdNotifications(observable=glyph, note="Kinetikern2")
        self.glyphs[id(glyph)] = glyph
        if self.since is None:
            self.since = time.perf_counter()

    def due(self):
        """True when the batch held should be posted."""
        return bool(self.glyphs) and (len(self.glyphs) >= self.batch or
                                      time.perf_counter() - self.since >= self.BATCH_S)

    def release(self):
        glyphs, self.glyphs = self.glyphs, {}
        self.since = None
        if not glyphs:
            return
        t = time.perf_counter()
        for glyph in glyphs.values():
            self.dispatcher.releaseHeldNotifications(observable=glyph)
        end = time.perf_counter()
        self._posted = (len(glyphs), end - t, end)
        self.batches += 1

    def stats(self):
        """How the notifications went out: batches, the last batch size, the
        costliest batch (ms)."""
        return {"batches": self.batches, "batch": self.batch, "costliest_ms": round(1000.0 * self.costliest, 1)}

    def resumed(self):
        """A slice starts: what RoboFont did since the last batch was posted
        (redrawing) is that batch's cost, and sizes the next one. A costlier
        glyph counts at once, a cheaper one gradually."""
        if self._posted is None:
            return
        n, posting, end = self._posted
        self._posted = None
        cost = posting + time.perf_counter() - end
        self.costliest = max(self.costliest, cost)
        per = cost / n
        if self.per_glyph is None or per > self.per_glyph:
            self.per_glyph = per
        else:
            self.per_glyph = 0.7 * self.per_glyph + 0.3 * per
        self.batch = max(self.FEWEST, min(self.MOST, int(self.TARGET_S / max(self.per_glyph, 1e-6))))


# ----------------------------------------------------------------- plan
class ApplyPlan(object):
    """Everything one Apply writes, decided before anything is written.

    kerning        [(left key, right key, int value)], one per result entry
    groups_to_set  {glyph: (left group or None, right group or None)}: only
                   sides without a group, of glyphs joining a class the
                   kerning uses (short names)
    metrics        {glyph: (LSB or None, RSB or None)}: whole-unit targets
    metrics_ink    the same targets as the engine has them (what the Applier
                   moves to and checks)
    keep_width     glyphs whose advance stays (tabular figures): the LSB
                   moves, the width is put back
    followers      glyphs re-spaced because a side follows a re-spaced glyph
    removals       [(left key, right key)] existing pairs Replace removes
    groups_to_move {glyph: (left group or None, right group or None)}: sides
                   that leave a group they shared with frozen glyphs for a
                   class of their own (spacing groups)
    frozen         glyphs Apply leaves alone (frozen spacing groups)
    counts         for the confirmation dialog
    details        a finer breakdown
    """

    def __init__(self, master_id, master_name, replace):
        self.master_id = master_id
        self.master_name = master_name
        self.replace = bool(replace)
        self.kerning = []
        self.groups_to_set = {}
        self.metrics = {}
        self.metrics_ink = {}
        self.keep_width = set()
        self.followers = []
        self.sync = []  # the Glyphs plugin's name for what follows; RoboFont writes followers directly
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
    glyphs, or by kerning pairs of a group that no longer exists): the new
    class must not merge into either."""
    out = []
    for origin in origins:
        if origin & kb.EXISTING:
            gid = origin & 0x7FFFFFFF
            if gid >= len(group_names):
                raise ValueError("class origin names group %d of %d" % (gid, len(group_names)))
            out.append(group_names[gid])
            continue
        if origin & kb.GLYPH_KEYED:
            out.append(None)  # a frozen glyph without a group: written with its own name
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


class Planner(_Stepper):
    """Builds the ApplyPlan for applying `result` to the snapshot's font, in
    slices (`plan` once done).

    `replace`: remove existing pairs between the kerned glyphs (and their
    classes) that the plan does not overwrite. `metrics_names`: glyphs whose
    sidebearings are written; None = every glyph when the solve covered the
    whole font, else the glyphs it kerned (either way plus the glyphs their
    rules follow and the glyphs that follow them). `scope_scripts`: the
    solve paired only glyphs of one script (plus Common), as the window's
    solves do; Replace then keeps existing pairs across scripts, which the
    engine never saw. The result must stay open until the planner is done.
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
        dfont = host.naked(snapshot.font)
        p = ApplyPlan(snapshot.master_id, getattr(snapshot, "master_name", ""), replace)
        infos = snapshot.infos
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

        # 1. the font's kerning as it is (pairs only), and the group names
        #    taken on each side: by groups, and by pairs of groups that are gone
        self.phase = "existing"
        existing = []
        kern_right, kern_left = set(), set()
        for full in dfont.groups.keys():
            full = str(full)
            if full.startswith(LEFT_PREFIX):
                kern_right.add(full[len(LEFT_PREFIX):])
            elif full.startswith(RIGHT_PREFIX):
                kern_left.add(full[len(RIGHT_PREFIX):])
        for k, (lk, rk) in enumerate(dfont.kerning.keys()):
            lk, rk = str(lk), str(rk)
            if lk.startswith(LEFT_PREFIX):
                kern_right.add(lk[len(LEFT_PREFIX):])
            if rk.startswith(RIGHT_PREFIX):
                kern_left.add(rk[len(RIGHT_PREFIX):])
            existing.append((lk, rk))
            if not k & 511:
                yield
        self.fraction = 0.25

        # 2. class names and keys
        self.phase = "kerning"
        right_names = _class_names(result.right_class_origin[:], right_groups, names, set(right_groups) | kern_right)
        left_names = _class_names(result.left_class_origin[:], left_groups, names, set(left_groups) | kern_left)
        r_origins, l_origins = result.right_class_origin[:], result.left_class_origin[:]
        rkeys = [LEFT_PREFIX + s if s is not None else names[r_origins[c] & ~kb.GLYPH_KEYED]
                 for c, s in enumerate(right_names)]
        lkeys = [RIGHT_PREFIX + s if s is not None else names[l_origins[q] & ~kb.GLYPH_KEYED]
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
                add((names[a], lkeys[b], iv))
                used_l[b] = 1
                gc += 1
            elif kind == kb.ENTRY_CLASS_GLYPH:
                add((rkeys[a], names[b], iv))
                used_r[a] = 1
                cg += 1
            else:
                if iv == 0 and not classes:
                    continue  # a plain glyph pair of zero overrides nothing
                add((names[a], names[b], iv))
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
            note(left_touch, names[i], s)
            note(right_touch, names[i], s)
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
        # keys that belong to frozen glyphs: their names, and the groups any
        # frozen glyph is in (what lies between two of them stays)
        frozen_keys = set()
        if frozen_names:
            for i in range(n):
                if frozen[i]:
                    frozen_keys.add(names[i])
                    spec = specs[i]
                    if spec.right_group != kb.NONE:
                        frozen_keys.add(LEFT_PREFIX + right_groups[spec.right_group])
                    if spec.left_group != kb.NONE:
                        frozen_keys.add(RIGHT_PREFIX + left_groups[spec.left_group])
        removals = []
        overriding = 0
        existing_set = set(existing)
        written = set()  # the existing pairs the plan overwrites
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
                # existing glyph-level pairs that will keep overriding a new class pair
                index = dict((names[i], i) for i in range(n) if kern[i])
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

        # 6. sidebearings: the glyphs in scope, the glyphs their rules follow
        #    and the glyphs that follow them (RoboFont recomputes neither)
        self.phase = "metrics"
        metric_rows = result.metrics
        if metrics_names is not None:
            wanted = set(metrics_names)
            in_scope = [name in wanted for name in names]
        elif _covers_font(specs, kern, metric_rows):
            in_scope = [True] * n
        else:
            in_scope = [bool(k) for k in kern]
        for i in range(n):
            if frozen[i]:
                in_scope[i] = False  # frozen: its sidebearings stay
        yield
        followers = []
        targets_of = {}
        for i, spec in enumerate(specs):
            ts = [t for rule, t in ((spec.lsb_rule, spec.lsb_glyph), (spec.rsb_rule, spec.rsb_glyph))
                  if rule in _FOLLOW and t < n]
            if ts:
                targets_of[i] = ts
        # a side that follows another glyph lands where that glyph's new
        # spacing puts it: the glyphs followed are re-spaced too (Ntilde in
        # the sample text moves N) …
        stack = [i for i in range(n) if in_scope[i]]
        while stack:
            for t in targets_of.get(stack.pop(), ()):
                if not in_scope[t] and not frozen[t]:
                    in_scope[t] = True
                    stack.append(t)
        # … and so are the glyphs that follow a glyph that moves (N moves Ntilde)
        users = {}
        for i, ts in targets_of.items():
            for t in ts:
                users.setdefault(t, []).append(i)
        stack = [i for i in range(n) if in_scope[i]]
        while stack:
            for f in users.get(stack.pop(), ()):
                if not in_scope[f] and not frozen[f]:
                    in_scope[f] = True
                    followers.append(names[f])
                    stack.append(f)
        yield
        metrics = {}
        metrics_ink = {}
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
                continue  # a kept advance and a rule on one side leave nothing free
            lsb = round_units(m.lsb) if math.isfinite(m.lsb) and spec.lsb_rule != kb.RULE_FIXED else None
            rsb = round_units(m.rsb) if math.isfinite(m.rsb) and spec.rsb_rule != kb.RULE_FIXED and not fixed \
                else None
            if lsb is None and rsb is None:
                continue
            name = names[i]
            metrics[name] = (lsb, rsb)
            metrics_ink[name] = (float(m.lsb) if lsb is not None else None, float(m.rsb) if rsb is not None else None)
            if fixed:
                keep_width.add(name)
            # the dialog's estimate, against the font as the snapshot read it
            info = infos.get(name)
            cur_l = getattr(info, "lsb", spec.cur_lsb)
            cur_r = getattr(info, "rsb", spec.cur_rsb)
            if _moved(lsb, cur_l) is not None or _moved(rsb, cur_r) is not None:
                changing += 1

        # big temporaries go a slice at a time (freeing them at once would
        # take as long as building them)
        for _ in _release(existing, existing_set, written):
            yield
        p.kerning = kerning
        p.groups_to_set = groups
        p.metrics = metrics
        p.metrics_ink = metrics_ink
        p.keep_width = keep_width
        p.followers = followers
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
            "other_masters": 0,
            "frozen": len(p.frozen),
            "group_moves": len(moves),
            "followers": len(followers),
        }
        p.details = {
            "kerning": len(kerning), "class_pairs": cc, "glyph_class": gc, "class_glyph": cg, "glyph_glyph": gg,
            "zero_exceptions": zero_exceptions, "zero_class_pairs": zero_classes, "existing": len(existing),
            "removals": len(removals), "overriding": overriding, "group_glyphs": len(groups),
            "group_sides": group_sides, "new_groups": new_groups, "joining_existing": joining,
            "metric_glyphs": len(metrics), "metrics_changing": changing, "keep_width": len(keep_width),
            "ruled_sides": ruled_sides, "followers": len(followers), "classes": classes,
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
    kerning pairs it wrote or removed, the kerning groups and metrics of the
    glyphs it wrote (and of the composites it held in place), the positions
    of those components, and the groups it created. O(what the Apply wrote):
    the font's other kerning is never copied.

    A Restorer puts back what is still as the Apply left it; anything
    changed since (by hand, by another extension) is kept unless the caller
    decides otherwise.
    """

    def __init__(self, font, master_id):
        self.font = font  # the defcon font (a closed font can go away: see kk2_window)
        self.master_id = master_id
        self.kerning = {}  # (left, right) → value before Apply, None: no pair
        self.groups = {}  # name → (left group, right group), short names
        self.metrics = {}  # name → (LSB, RSB, width) on the ink
        self.outlines = {}  # name → the x of its points, anchors, guidelines, component offsets (_xs_of)
        self.widths = {}  # name → its advance as the font had it (600 stays 600, 600.0 stays 600.0)
        self.composed = {}  # composite name → its component glyph names
        self.components = []  # (composite, index, x, y)
        self.created_groups = set()  # full names of groups the Apply created
        # as the Apply left them; None until it finished (then a revert
        # restores everything captured, unconditionally)
        self.applied_kerning = None  # (left, right) → value, None: removed
        self.applied_glyphs = None  # name → (left group, right group, LSB, width)
        self.applied_components = None  # (composite, index) → (x, y)
        self.capture_ms = 0.0
        self._index = None

    @property
    def entry_count(self):
        return len(self.kerning)

    def _layer(self):
        return _layer(self.font, self.master_id)

    def group_index(self):
        if self._index is None:
            self._index = GroupIndex(self.font)
        return self._index

    def capture_glyph(self, name, index):
        if name in self.groups:
            return
        glyph = _glyph(self._layer(), name)
        if glyph is None:
            return
        self.groups[name] = index.of(name)
        if glyph.components:
            self.composed[name] = [c.baseGlyph for c in glyph.components]
        self.metrics[name] = ink_metrics(glyph, self._layer())
        self.outlines[name] = _xs_of(glyph)
        self.widths[name] = glyph.width

    def capture_component(self, name, k):
        comp = self._component(name, k)
        if comp is not None:
            self.components.append((name, k) + _raw_offset(comp))

    def capture_kerning(self, kerning, lk, rk):
        if (lk, rk) not in self.kerning:
            self.kerning[(lk, rk)] = pair_value(kerning, (lk, rk))

    def glyph_state(self, name, index=None):
        """(left group, right group, LSB, width) of a captured glyph now, or None."""
        layer = self._layer()
        glyph = _glyph(layer, name)
        if glyph is None:
            return None
        index = index or GroupIndex(self.font)
        lsb, _rsb, width = ink_metrics(glyph, layer)
        return index.of(name) + (lsb, width)

    def _component(self, name, k):
        glyph = _glyph(self._layer(), name)
        if glyph is None:
            return None
        comps = glyph.components
        return comps[k] if 0 <= k < len(comps) else None

    def _order(self, names):
        """Of `names`, the glyphs that are put back directly, in restore order:
        composites before the glyphs they are built from (outermost first:
        until their components move they are exactly as Apply left them, so
        their own LSB moves back exactly), then plain glyphs."""
        depth = {}

        def depth_of(name, seen=()):
            if name not in self.composed:
                return 0
            if name not in depth:
                depth[name] = 1 + max([depth_of(c, seen + (name,)) for c in self.composed[name]
                                       if c is not None and c not in seen] or [0])
            return depth[name]

        out = [n for n in self.metrics if n in names]
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
        kerning = self.font.kerning
        index = GroupIndex(self.font)
        layer = self._layer()
        found = []
        count = 0
        for (lk, rk), before in self.kerning.items():
            now = pair_value(kerning, (lk, rk))
            if not _same_value(now, before):
                count += 1
                if len(found) < limit:
                    found.append(("kerning", lk, rk, now, before))
        for name, saved in self.groups.items():
            now = index.of(name)
            if now != saved:
                count += 1
                if len(found) < limit:
                    found.append(("groups", name, now, saved))
        for name, saved in self.metrics.items():
            glyph = _glyph(layer, name)
            now = ink_metrics(glyph, layer) if glyph is not None else None
            if now is None or any(not _same_metric(x, y) for x, y in zip(now, saved)):
                count += 1
                if len(found) < limit:
                    found.append(("metrics", name, now, saved))
        for name, k, x, y in self.components:
            comp = self._component(name, k)
            now = _offset(comp) if comp is not None else None
            if now is None or abs(now[0] - x) > 1e-6 or abs(now[1] - y) > 1e-6:
                count += 1
                if len(found) < limit:
                    found.append(("component", name, k, now, (x, y)))
        for full in self.created_groups:
            if full in self.font.groups:
                count += 1
                if len(found) < limit:
                    found.append(("created group", full))
        return count, found


def _same_metric(x, y):
    if x is None or y is None:
        return x is None and y is None
    return not math.isfinite(y) or abs(x - y) <= SAME_METRIC


def _same_glyph_state(a, b):
    if a is None or b is None:
        return a is None and b is None
    if tuple(a[:2]) != tuple(b[:2]):
        return False
    return all(_same_metric(x, y) for x, y in zip(a[2:], b[2:]))


class Restorer(_Stepper):
    """Puts a RevertPoint back, in slices.

    First it checks what is still as the Apply left it. If anything was
    changed since (`conflicts`), it pauses (`waiting`) for the caller's
    resolve(overwrite): False keeps those later changes and restores the
    rest, True puts everything back as it was before the Apply. A point of
    an Apply that did not finish has no record of what it left and is put
    back entirely. `counts` once done."""

    def __init__(self, point, overwrite=False):
        _Stepper.__init__(self)
        self.point = point
        self.overwrite = bool(overwrite)
        self.conflicts = {"kerning": 0, "glyphs": 0, "components": 0}
        self.examples = []
        self.counts = None
        self.text = "Checking what changed since Apply"
        self._held = _Held(point.font)

    @property
    def conflict_count(self):
        return sum(self.conflicts.values())

    def resolve(self, overwrite):
        """The caller's answer to the conflicts: carry on."""
        self.overwrite = bool(overwrite)
        self.waiting = False

    def _begin_slice(self):
        self._held.resumed()

    def _end_slice(self):
        if self._held.due():
            self._held.release()

    def _edited(self):
        """After a glyph edit: _PAUSE when a batch of notifications is due."""
        return _PAUSE if self._held.due() else None

    def _conflict(self, kind, *what):
        self.conflicts[kind] += 1
        if len(self.examples) < EXAMPLES:
            self.examples.append((kind,) + what)

    def _work(self):
        point = self.point
        dfont = point.font
        kerning = dfont.kerning
        layer = point._layer()
        counts = {"kerning_set": 0, "kerning_removed": 0, "groups": 0, "metrics": 0, "components": 0,
                  "groups_deleted": 0, "kept_kerning": 0, "kept_glyphs": 0, "kept_components": 0}
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
            now = pair_value(kerning, key)
            now_kerning[key] = now
            if checked and not _same_value(now, point.applied_kerning.get(key)):
                changed_keys.add(key)
                self._conflict("kerning", key[0], key[1], point.applied_kerning.get(key), now)
            if not k & 63:
                self.fraction = 0.3 * k / max(1, len(keys))
                yield
        index = GroupIndex(dfont)
        if checked and point.applied_glyphs is not None:
            for k, name in enumerate(names):
                if not _same_glyph_state(point.glyph_state(name, index), point.applied_glyphs.get(name)):
                    changed_glyphs.add(name)
                    self._conflict("glyphs", name)
                if not k & 15:
                    yield
            for name, k, _x, _y in point.components:
                comp = point._component(name, k)
                applied = (point.applied_components or {}).get((name, k))
                now = _offset(comp) if comp is not None else None
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

        # 2. metrics (composites before their bases), then the components
        #    every base moved back dragged along
        self.phase = "metrics"
        restore = set(n for n in point.metrics if n not in changed_glyphs)
        for name in point._order(restore):
            changed = self._restore_metrics(layer, name)
            if changed:
                counts["metrics"] += 1
            done += 1
            self.fraction = done / total
            yield self._edited() if changed else None
        self.phase = "components"
        for name, k, x, y in point.components:
            if (name, k) in changed_comps:
                continue
            comp = point._component(name, k)
            if comp is not None and _raw_offset(comp) != (x, y):
                self._held.hold(_glyph(layer, name))
                _set_offset(comp, x, y)
                counts["components"] += 1
                yield self._edited()
            else:
                yield
        self._held.release()

        # 3. groups (one update), and the groups the Apply created, if empty now
        self.phase = "groups"
        index = GroupIndex(dfont)
        for name, (left, right) in point.groups.items():
            if name in changed_glyphs:
                continue
            counts["groups"] += index.set(name, "left", left) + index.set(name, "right", right)
            done += 1
            if not done & 63:
                self.fraction = done / total
                yield
        for full in point.created_groups:
            members = index._list(full) if full in dfont.groups or full in index.members else None
            if members is not None and not members:
                index.drop_if_empty(full)
                counts["groups_deleted"] += 1
        index.flush()
        yield

        # 4. kerning: the table rebuilt once
        self.phase = "kerning"
        sets, removes = {}, []
        for key in keys:
            if key in changed_keys:
                continue
            before = point.kerning[key]
            if not _same_value(now_kerning[key], before):
                if before is None:
                    removes.append(key)
                else:
                    sets[key] = _number(before)
        _write_kerning(kerning, sets, removes)
        counts["kerning_set"] = len(sets)
        counts["kerning_removed"] = len(removes)
        return counts

    def _restore_metrics(self, layer, name):
        """Puts a glyph's LSB (on the ink) and width back; the RSB follows. A
        composite is read before any glyph it is built from moves back (see
        RevertPoint._order), so its outline is still as Apply left it. True
        if anything was set."""
        glyph = _glyph(layer, name)
        if glyph is None:
            return False
        lsb, _rsb, width = self.point.metrics[name]
        changed = False
        self._held.hold(glyph)
        now, _r, _w = ink_metrics(glyph, layer)
        if lsb is not None and now is not None:
            shift = lsb - now
            whole = round(shift)
            if abs(shift - whole) <= 1e-6:
                # Apply moves by whole units: the outline goes back by exactly
                # as many (a curve extreme's last bits do not reach the points)
                shift = whole
            if abs(shift) > 1e-6:
                # every coordinate back as it was, if the glyph is as Apply
                # left it; else (edited since) moved back as a whole
                saved = self.point.outlines.get(name)
                if not (saved is not None and shift == whole and _unmove(glyph, saved, -whole)):
                    _move_glyph(glyph, shift)
                changed = True
        raw = self.point.widths.get(name)
        if raw is None or float(raw) != width:
            raw = _number(width)
        if glyph.width != raw or type(glyph.width) is not type(raw):
            glyph.width = raw  # the advance as the font had it, int or float
            changed = True
        return changed

    def _finished(self, value):
        self._held.release()
        self.counts = dict(value or {})
        self.counts["conflicts"] = self.conflict_count
        self.counts["restore_ms"] = self.busy_ms
        self.counts["notifications"] = self._held.stats()
        self.counts["slowest_slice"] = {"ms": self.slice_ms[0], "phases": self.slice_ms[1],
                                        "end_slice_ms": self.end_slice_ms}


def _write_kerning(kerning, sets, removes):
    """Writes pairs and removes pairs in one batch: the table rebuilt once
    (a clear and an update: two notifications, however many pairs)."""
    if not sets and not removes:
        return
    if not removes:
        kerning.update(sets)
        return
    gone = set(removes)
    rebuilt = dict((k, v) for k, v in kerning.items() if k not in gone)
    rebuilt.update(sets)
    kerning.clear()
    kerning.update(rebuilt)


# ---------------------------------------------------------------- apply
class Applier(_Stepper):
    """Carries out a plan on the snapshot's font, in slices.

    Order: revert point, sidebearings (holding composites rigid), groups
    (one update), kerning (one batch); then what the Apply left is recorded
    (for a later revert), and up to 500 kerning pairs (and 100 removals) and
    every written sidebearing are read back. A failure halfway is reported
    in the summary ("error") rather than raised; `revert` then undoes the
    part already written. `summary` and `revert` once done.
    """

    def __init__(self, font, snapshot, plan):
        _Stepper.__init__(self)
        if snapshot is not None and snapshot.master_id != plan.master_id:
            raise ValueError("the plan is for layer %s, the snapshot for %s" % (plan.master_id, snapshot.master_id))
        self.font = font
        self.dfont = host.naked(font)
        self.snapshot = snapshot
        self.plan = plan
        self.summary = None
        self.revert = None
        self.error = None
        self.text = "Applying"
        self._held = _Held(self.dfont)
        self._counts = {"kerning": 0, "removed": 0, "groups": 0, "respaced": 0, "followers": 0}
        self._detail = {"group_conflicts": 0, "missing_glyphs": 0, "metric_sides": 0, "composites": 0,
                        "components_held": 0, "readback_metrics": 0, "readback_kerning": 0,
                        "readback_removals": 0}
        self._examples = []
        self._bad = [0, 0]  # metric, kerning mismatches

    def _begin_slice(self):
        self._held.resumed()

    def _end_slice(self):
        if self._held.due():
            self._held.release()

    def _edited(self):
        """After a glyph edit: _PAUSE when a batch of notifications is due."""
        return _PAUSE if self._held.due() else None

    def _failed(self, error):
        self.error = error
        print("Kinetikern2 apply failed:\n" + error)
        return True

    def _held_components(self, layer, users, name, shift):
        """The outline of `name` moved by `shift`: its components move back."""
        if not shift:
            return
        for composite, k, dx, dy in users.get(name, ()):
            glyph = _glyph(layer, composite)
            comps = glyph.components if glyph is not None else ()
            if not 0 <= k < len(comps):
                continue
            self._held.hold(glyph)
            x, y = _raw_offset(comps[k])
            if dx == 1 and dy == 0:
                # the common case, kept to the number types the font has
                _set_offset(comps[k], x - shift, y)
            else:
                _set_offset(comps[k], _number(x - dx * shift), _number(y - dy * shift))
            self._detail["components_held"] += 1

    def _work(self):
        dfont, p = self.dfont, self.plan
        layer = _layer(dfont, p.master_id)
        kerning = dfont.kerning
        detail, counts = self._detail, self._counts
        total = max(1, 2 * (len(p.groups_to_set) + len(p.metrics)) + len(p.removals) // 8 + len(p.kerning) // 8 + 1)
        done = 0

        # 1. the components drawing a glyph this apply may move
        self.phase = "composites"
        self.text = "Applying: preparing"
        names = set(p.metrics)
        names.update(p.groups_to_set)
        names.update(getattr(p, "groups_to_move", {}))
        users = {}
        if p.metrics:
            bases = set(p.metrics)
            candidates = None
            composites_of = getattr(self.snapshot, "composites_of", None)
            if composites_of is not None:
                candidates = composites_of(bases)
            yield from _component_users(layer, bases, candidates, users)
        held = [(c, k) for row in users.values() for (c, k, _dx, _dy) in row]
        composites = sorted(set(c for c, _k in held))
        names.update(composites)
        detail["composites"] = len(composites)
        yield

        # 2. the revert point: everything this apply writes, as it is now
        self.phase = "revert_point"
        self.text = "Applying: saving the revert point"
        revert = RevertPoint(dfont, p.master_id)
        index = GroupIndex(dfont)
        for k, name in enumerate(sorted(names)):
            revert.capture_glyph(name, index)
            if not k & 7:
                yield
        for k, (c, j) in enumerate(held):
            revert.capture_component(c, j)
            if not k & 15:
                yield
        for k, (lk, rk, _v) in enumerate(p.kerning):
            revert.capture_kerning(kerning, lk, rk)
            if not k & 255:
                yield
        for k, (lk, rk) in enumerate(p.removals):
            revert.capture_kerning(kerning, lk, rk)
            if not k & 255:
                yield
        revert.capture_ms = self.ms.get("revert_point", 0.0)
        self.revert = revert  # from here on the font changes

        # 3. sidebearings
        self.phase = "metrics"
        self.text = "Applying: sidebearings"
        written = []
        for name, (lsb, rsb) in p.metrics.items():
            moved = self._write_metrics(layer, name, lsb, rsb, users, written)
            if moved:
                counts["respaced"] += 1
            done += 2
            self.fraction = done / total
            yield self._edited() if moved else None
        counts["followers"] = sum(1 for name in getattr(p, "followers", ()) if name in p.metrics)
        self._held.release()

        # 4. groups: sides without a group join theirs, frozen-group leavers
        #    move to their own (one update)
        self.phase = "groups"
        self.text = "Applying: kerning groups"
        for name, (left, right) in p.groups_to_set.items():
            if _glyph(layer, name) is None:
                detail["missing_glyphs"] += 1
                continue
            for side, want in (("left", left), ("right", right)):
                if want is None:
                    continue
                now = index.of(name)[0 if side == "left" else 1]
                if now is None:
                    counts["groups"] += index.set(name, side, want)
                elif now != want:
                    detail["group_conflicts"] += 1
                    if len(self._examples) < EXAMPLES:
                        self._examples.append(("group conflict", name, side, now, want))
            done += 1
            if not done & 63:
                self.fraction = done / total
                yield
        for name, (left, right) in getattr(p, "groups_to_move", {}).items():
            if _glyph(layer, name) is None:
                detail["missing_glyphs"] += 1
                continue
            for side, want in (("left", left), ("right", right)):
                if want is not None:
                    counts["groups"] += index.set(name, side, want)
        revert.created_groups = set(index.created)
        index.flush()
        yield

        # 5. removals and kerning, one batch
        self.phase = "kerning"
        self.text = "Applying: kerning"
        sets = dict(((lk, rk), v) for lk, rk, v in p.kerning)
        _write_kerning(kerning, sets, [pair for pair in p.removals if pair not in sets])
        counts["kerning"] = len(p.kerning)
        counts["removed"] = len(p.removals)
        yield

        # 6. what the apply left, for a revert that keeps later changes
        self.phase = "record"
        self.text = "Applying: checking"
        applied = {}
        for k, (lk, rk, v) in enumerate(p.kerning):
            applied[(lk, rk)] = float(v)
            if not k & 4095:
                yield
        for lk, rk in p.removals:
            applied.setdefault((lk, rk), None)
        index = GroupIndex(dfont)
        glyphs = {}
        for k, name in enumerate(list(revert.groups)):
            glyphs[name] = revert.glyph_state(name, index)
            if not k & 7:
                yield
        comps = {}
        for n, (name, k, _x, _y) in enumerate(revert.components):
            comp = revert._component(name, k)
            if comp is not None:
                comps[(name, k)] = _offset(comp)
            if not n & 15:
                yield
        revert.applied_kerning, revert.applied_glyphs, revert.applied_components = applied, glyphs, comps

        # 7. read-back
        self.phase = "read_back"
        for k, row in enumerate(written):
            self._bad[0] += self._read_back_metrics(layer, row)
            if not k & 15:
                yield
        k = len(p.kerning)
        stride = max(1, -(-k // READBACK_KERNING))
        for idx in range(0, k, stride):
            lk, rk, want = p.kerning[idx]
            have = pair_value(kerning, (lk, rk))
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
            if (lk, rk) in sets:
                continue
            have = pair_value(kerning, (lk, rk))
            detail["readback_removals"] += 1
            if have is not None:
                self._bad[1] += 1
                if len(self._examples) < EXAMPLES:
                    self._examples.append(("removal", lk, rk, None, have))

    def _write_metrics(self, layer, name, lsb, rsb, users, written):
        """Moves the free sides to the plan's targets in whole units, worked
        out on the ink (the engine's frame); every LSB move holds the
        composites drawing the glyph in place. The RSB moves through the
        width. True if a side moved."""
        glyph = _glyph(layer, name)
        if glyph is None:
            self._detail["missing_glyphs"] += 1
            return False
        sides = 0
        self._held.hold(glyph)
        keep = glyph.width if name in self.plan.keep_width else None
        ink_l, ink_r = self.plan.metrics_ink.get(name, (None, None))
        now_l, _now_r, _w = ink_metrics(glyph, layer)
        if lsb is not None and now_l is not None:
            target = ink_l if ink_l is not None else float(lsb)
            d = round_units(target - now_l)
            if d:
                _move_glyph(glyph, d)
                glyph.width = glyph.width + d  # d is whole: an int advance stays an int
                self._held_components(layer, users, name, d)
                sides += 1
        if keep is not None and glyph.width != keep:
            glyph.width = keep
        _l, now_r, _w = ink_metrics(glyph, layer)
        if rsb is not None and now_r is not None:
            target = ink_r if ink_r is not None else float(rsb)
            d = round_units(target - now_r)
            if d:
                glyph.width = glyph.width + d
                sides += 1
        if sides:
            written.append((name, keep))
            self._detail["metric_sides"] += sides
        return bool(sides)

    def _read_back_metrics(self, layer, row):
        """A written glyph ends within half a unit of its targets (whole-unit
        moves from a fractional side), and a kept advance is exactly as it
        was. Returns the number of sides that do not."""
        name, keep = row
        lsb, rsb = self.plan.metrics[name]
        ink_l, ink_r = self.plan.metrics_ink.get(name, (None, None))
        glyph = _glyph(layer, name)
        if glyph is None:
            return 1
        have_l, have_r, width = ink_metrics(glyph, layer)
        bad = 0
        checks = (("LSB", ink_l if ink_l is not None else lsb, have_l, 0.5),
                  ("RSB", ink_r if ink_r is not None else rsb, have_r, 0.5),
                  ("width", keep, width, 0.0))
        for side, want, have, tolerance in checks:
            if want is None or (side != "width" and (lsb if side == "LSB" else rsb) is None):
                continue
            self._detail["readback_metrics"] += 1
            if have is None or not abs(float(have) - want) <= tolerance + 1e-6:
                bad += 1
                if len(self._examples) < EXAMPLES:
                    self._examples.append(("metrics", name, side, want, have))
        return bad

    def _finished(self, value):
        self._held.release()
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
            "followers_respaced": counts["followers"],
            "metric_mismatches": metric_bad,
            "kerning_mismatches": kerning_bad,
            "apply_ms": self.busy_ms,
        }
        if detail["group_conflicts"]:
            summary["group_conflicts"] = detail["group_conflicts"]  # glyphs that got a group since the snapshot
        if detail["missing_glyphs"]:
            summary["missing_glyphs"] = detail["missing_glyphs"]
        if error is not None:
            summary["write_errors"] = 1  # the status line shows it; the traceback is in "error" and the Output window
        summary["master_id"] = p.master_id
        summary["error"] = error
        summary["examples"] = self._examples
        summary["details"] = detail
        summary["notifications"] = self._held.stats()
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
        lines.append("%s existing pairs between these glyphs and their groups are removed." % _n(c["removals"]))
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
    line = "%s glyphs get new sidebearings" % _n(c.get("metrics", 0))
    if c.get("followers"):
        line += ", %s of them because a side follows a glyph that moves (metrics keys, aligned composites)" % _n(
            c["followers"])
    lines.append(line + ".")
    lines.append("Only this font is changed: the other masters of a family are separate fonts, with their own "
                 "groups.")
    lines.append("Undo does not cover Apply. Revert Last Apply puts back what Apply changed.")
    return "\n".join(lines)


def describe_conflicts(restorer):
    """The question for a Restorer that found changes made after the Apply."""
    c = restorer.conflicts
    parts = []
    if c["kerning"]:
        parts.append("%s kerning pairs" % _n(c["kerning"]))
    if c["glyphs"]:
        parts.append("%s glyphs (groups or sidebearings)" % _n(c["glyphs"]))
    if c["components"]:
        parts.append("%s component positions" % _n(c["components"]))
    return ("Since the last Apply, %s were changed. Revert can keep those changes and put back only the rest, or "
            "put everything back as it was before the Apply (the later changes are lost; Undo does not cover "
            "Revert)." % ", ".join(parts))


def describe_summary(summary):
    """One status line for an apply summary."""
    if summary.get("error"):
        return ("Apply stopped with an error (see %s); Revert Last Apply undoes what was written."
                % host.OUTPUT_NAME)
    took = (summary.get("ms") or {}).get("wall", summary["apply_ms"])  # what the user waited, redraws included
    text = ("%s kerning pairs written, %s removed, %s group sides set, %s glyphs re-spaced in %.2f s"
            % (_n(summary["kerning_entries"]), _n(summary["entries_removed"]), _n(summary["group_sides_set"]),
               _n(summary["glyphs_respaced"]), took / 1000.0))
    bad = summary["metric_mismatches"] + summary["kerning_mismatches"]
    if bad:
        text += "; %s values read back differently" % _n(bad)
    if summary.get("group_conflicts"):
        text += "; %s glyphs kept a group they got after the preview" % _n(summary["group_conflicts"])
    return text + "."
