# encoding: utf-8
"""
kk2_joins_window — a connected script's joins.

The join checker (the engine's kk2_join_* functions) on the master, as drawn
and under the result the main window shows:

* Findings (as drawn): every pair of the basic a–z set the way the master
  sets it, judged as Spacing QA judges them. Two joining sides (each joins
  at least half its a–z partners) that do not meet make a broken join — in a
  script that joins consistently every such pair; in a partly connected hand
  (1 in 20 such pairs or more do not meet) only a pair whose sides both join
  three in four of their partners, the others listed as "not joined: partly
  connected" (style). Closer than 3 units per 1000 em, the strokes nearly
  touch. A join is fragile when it has less than 5 units of room to open; a
  crossing when the two letters enclose white that neither has alone, or
  change a counter. A broken join lists the kerning that would join it, or
  says that kerning cannot join it: the strokes would cross, or more than
  100 units per 1000 em would be needed (a longer stroke or an alternate
  joins it). In a design whose glyphs touch by construction (a line, a grid
  or a background through every glyph) the same findings say where the line
  does not meet.
* Under the preview: what the result does to every pair of letters that
  joins in the master (Keep joins: nothing breaks), the sides that break the
  most, and the a–z pairs it breaks.
* Drawing advice (Keep joins): the joining sides keep the designer's
  sidebearings; for each, the sidebearing Kinetikern2 would give its body,
  relative to the font's own body rhythm (the median kept a–z side). Negative:
  the body sits that much further from its neighbour than the other letters
  do — shorten the stroke that reaches out and the advance follows. Advice,
  not edits.

Select rows and Open Proof to see them in context ("n" + pair + "n"): an Edit
tab in Glyphs, the Space Center in RoboFont (the host decides).

Everything here is read from the main window (its context, result and
snapshot); the window holds nothing of its own but the rows.
"""

from __future__ import division, print_function, unicode_literals

import traceback

import vanilla

import kk2_bridge as kb

VIEWS = ["Findings (as drawn)", "Under the preview", "Drawing advice"]
FINDINGS, PREVIEW, ADVICE = 0, 1, 2
FRAGILE = 5.0  # units per 1000 em (the engine's JOIN_FRAGILE)
JOINING = 0.5  # a side joins when it joins at least this share of its a–z partners
STRONG = 0.75  # in a partly connected hand only sides this joining make broken joins
PARTLY = 0.05  # a hand whose joining sides fail to meet this often joins partly by design
BROKEN_GAP = 3.0  # units per 1000 em: closer, the strokes nearly touch (Spacing QA's BROKEN_GAP)
KERN_FIX_MAX = 100.0  # units per 1000 em: more kerning than this does not fix a broken join


def _num(v, digits=0):
    if v is None or v != v or v in (float("inf"), float("-inf")):
        return "–"
    return ("%%.%df" % digits) % v


def _signed(v, digits=0):
    if v is None or v != v:
        return "–"
    return ("%%+.%df" % digits) % v


class GlyphsHost(object):
    """Opens proofs in a new Edit tab of the font."""

    def __init__(self, font):
        self.font = font

    def open_proof(self, lines):
        text = "\n".join(" ".join("".join("/" + n for n in word) for word in line) for line in lines)
        try:
            self.font.newTab(text)
        except Exception:
            print(traceback.format_exc())


class RoboFontHost(object):
    """Opens proofs in a Space Center of the font: `font`, or a function
    that returns the font the main window works on now (its Font popup can
    switch fonts while this window is open)."""

    def __init__(self, font):
        self._font = font

    @property
    def font(self):
        return self._font() if callable(self._font) else self._font

    def open_proof(self, lines):
        # the Space Center's input takes "\n" (backslash, n) as a line break
        text = "\\n".join(" ".join("".join("/" + n for n in word) for word in line) for line in lines)
        try:
            from mojo.UI import OpenSpaceCenter
            center = OpenSpaceCenter(self.font)
            center.setRaw(text)
        except Exception:
            print(traceback.format_exc())


class JoinsWindow(object):
    """The joins of the main window's master and result."""

    def __init__(self, kw, host):
        self.kw = kw
        self.host = host
        self.rows = []
        self.view = FINDINGS
        w = vanilla.FloatingWindow((760, 560), "Joins — Kinetikern2", minSize=(620, 360))
        self.w = w
        w.summary = vanilla.TextBox((14, 10, -14, 64), "", sizeStyle="small")
        # not w.show: vanilla windows have a show() method, which Glyphs' vanilla refuses to replace
        w.viewPicker = vanilla.SegmentedButton((14, 80, 420, 22), [dict(title=t) for t in VIEWS],
                                               callback=self.viewChanged, sizeStyle="small")
        w.viewPicker.set(FINDINGS)
        w.refresh = vanilla.Button((-110, 79, -14, 22), "Refresh", callback=self.refresh, sizeStyle="small")
        columns = [dict(title="Pair / side", key="what", width=110), dict(title="Finding", key="finding", width=210),
                   dict(title="Value", key="value", width=90), dict(title="Note", key="note")]
        w.list = vanilla.List((14, 110, -14, -46), [], columnDescriptions=columns, allowsMultipleSelection=True,
                              doubleClickCallback=self.openProof)
        w.proof = vanilla.Button((14, -36, 140, 22), "Open Proof", callback=self.openProof, sizeStyle="small")
        w.status = vanilla.TextBox((164, -32, -14, 16), "", sizeStyle="mini")
        w.bind("close", self._closed)
        w.open()
        self.refresh()

    # ---------------------------------------------------------- the host window
    def front(self):
        self.w.makeKey()

    def close(self):
        self.w.close()

    def _closed(self, sender):
        cb = getattr(self.kw, "joins_window_closed", None)
        if cb is not None:
            cb()

    def result_ready(self):
        self.refresh()

    def viewChanged(self, sender):
        self.view = int(sender.get())
        self._show()

    # ---------------------------------------------------------- the checker
    def _letters(self):
        """The basic a–z of the snapshot (JOINKIND_LOWER), in index order."""
        kinds = self.kw.join_kinds or ()
        return [i for i, k in enumerate(kinds) if k == kb.JOINKIND_LOWER]

    def refresh(self, sender=None):
        kw = self.kw
        self.findings, self.under, self.advice = [], [], []
        self.text = ""
        if kw.snapshot is None or kw.context is None:
            self.text = "Waiting for the font to be read."
        elif kw.joins is None:
            self.text = ("Not a connected script: %s." % kw.join_note if kw.join_note else
                         "Connected script is off: turn it on in the main window to check the joins.")
        elif kw.join_kinds is None or not kw.engine.features & kb.FEATURE_JOIN_CHECK:
            self.text = "This engine build has no join checker: rebuild it with build.sh."
        else:
            try:
                self._measure()
            except Exception as e:
                print(traceback.format_exc())
                self.text = "The join checker failed: %s" % e
        self.w.summary.set(self.text)
        self._show()

    def _measure(self):
        kw = self.kw
        snap, ctx, res = kw.snapshot, kw.context, kw.result
        names = snap.names
        per = 1000.0 / snap.upm
        az = self._letters()
        pairs = [(a, b) for a in az for b in az]
        detail = kw.engine.join_pairs(ctx, pairs, res) if pairs else []
        # each side's share of joined a–z partners, as drawn
        right, left = {}, {}
        for d in detail:
            r = right.setdefault(d["left"], [0, 0])
            l = left.setdefault(d["right"], [0, 0])
            r[1] += 1
            l[1] += 1
            if d["joins"]:
                r[0] += 1
                l[0] += 1
        rate = lambda tally, i: tally[i][0] / float(tally[i][1]) if i in tally and tally[i][1] else 0.0
        sides = lambda d, share: rate(right, d["left"]) >= share and rate(left, d["right"]) >= share
        pair_name = lambda d: (names[d["left"]], names[d["right"]])
        label = lambda d: "%s %s" % pair_name(d)
        # Spacing QA's rule: a consistent joiner's exceptions are broken
        # joins; a partly connected hand's only between strongly joining sides
        between = [d for d in detail if sides(d, JOINING)]
        apart = [d for d in between if not d["joins"]]
        exceptions = len(apart) / float(len(between)) if between else 0.0
        partly = exceptions >= PARTLY
        gap = lambda d: d["gap"] * per if d["gap"] == d["gap"] else float("inf")
        candidates = [d for d in apart if not partly or sides(d, STRONG)]
        broken = [d for d in candidates if gap(d) >= BROKEN_GAP]
        near = [d for d in candidates if gap(d) < BROKEN_GAP]
        partial = [d for d in apart if partly and not sides(d, STRONG)]
        broken.sort(key=lambda d: -(d["gap"] if d["gap"] == d["gap"] else 0))
        partial.sort(key=lambda d: -(d["gap"] if d["gap"] == d["gap"] else 0))
        decorated = bool(getattr(kw, "join_decorated", False))
        fragile = [d for d in detail if d["joins"] and d["open"] * per < FRAGILE]
        fragile.sort(key=lambda d: d["open"])
        crossing = [d for d in detail if d["joins"] and d["crossing"]]
        for d in broken:
            fix = d["fix"] * per
            self.findings.append(dict(what=label(d), finding="the line does not meet" if decorated else
                                      "broken: the strokes do not meet",
                                      value="%s apart" % _num(d["gap"] * per, 1),
                                      note="the kern that joins it makes the strokes cross: a longer stroke or an alternate" if d.get("fix_crosses") else
                                      "too far for kerning: a longer stroke or an alternate" if abs(fix) > KERN_FIX_MAX else
                                      "kern %s joins it" % _signed(fix) if fix == fix else "no small kern joins it",
                                      proof=[pair_name(d)]))
        for d in near:
            self.findings.append(dict(what=label(d), finding="nearly touch: a hairline gap",
                                      value="%s apart" % _num(d["gap"] * per, 1), note="", proof=[pair_name(d)]))
        for d in partial:
            self.findings.append(dict(what=label(d), finding="not joined: partly connected",
                                      value="%s apart" % _num(d["gap"] * per, 1), note="style, not flagged",
                                      proof=[pair_name(d)]))
        for d in fragile:
            self.findings.append(dict(what=label(d), finding="fragile: little room to open",
                                      value="room %s" % _num(d["open"] * per, 1), note="", proof=[pair_name(d)]))
        for d in crossing:
            self.findings.append(dict(what=label(d), finding="the strokes cross (enclosed white)", value="",
                                      note="", proof=[pair_name(d)]))
        joins = sum(1 for d in detail if d["joins"])
        lines = ["%s: %s of %s a–z pairs join as drawn (%s); %s %s, %s nearly touching, %s fragile, %s crossing." % (
            snap.master_name, "{:,}".format(joins), "{:,}".format(len(detail)), kw.join_note,
            len(broken), "where the line does not meet" if decorated else "broken", len(near), len(fragile),
            len(crossing))]
        if partial:
            lines.append("A partly connected hand: %.0f %% of the pairs of its joining letters do not meet, so %s %s "
                         "listed as style, not flagged as broken." % (
                             100.0 * exceptions, len(partial), "is" if len(partial) == 1 else "are"))

        # under the result
        check = kw.join_check
        if res is not None and check is not None:
            st, sides = check
            mode = "Keep joins" if kw._keeps_joins() else "Space joined letters"
            lines.append("%s, %s: %s of %s joins between letters kept, %s broken%s." % (
                "Whole-font result" if getattr(kw, "_result_kind", "") == "whole" else "Preview", mode,
                "{:,}".format(st["kept"]), "{:,}".format(st["joins"]), "{:,}".format(st["broken"]),
                ", %s a–z pairs cross" % st["az_crossings_made"] if st["az_crossings_made"] else ""))
            for g, side, n, delta in sides:
                self.under.append(dict(what="%s %s" % (names[g], side), finding="breaks %d join%s" % (n, "" if n == 1 else "s"),
                                       value="%s moved" % _signed(delta * per), note="",
                                       proof=self._side_proof(g, side == "right", az)))
            after = [d for d in detail if d["joins"] and not d["joins_after"]]
            after.sort(key=lambda d: -abs(d["delta"]))
            for d in after:
                self.under.append(dict(what=label(d), finding="breaks", value="%s" % _signed(d["delta"] * per),
                                       note="offset change", proof=[pair_name(d)]))
            # drawing advice: what the model wanted for the kept sides
            if kw._keeps_joins():
                self._advice(az, per)
        elif res is None:
            lines.append("No result yet: the preview is running.")
        self.text = "\n".join(lines)

    def _side_proof(self, g, right, az):
        names = self.kw.snapshot.names
        return [(names[g], names[b]) for b in az] if right else [(names[a], names[g]) for a in az]

    def _advice(self, az, per):
        kw = self.kw
        snap = kw.snapshot
        wanted = kw.result.wanted()
        bits = kw.engine.join_sides(kw.context)
        if not wanted or not bits:
            return
        raw = []
        for i in range(len(snap.names)):
            spec = snap.specs[i]
            b = bits[i] if i < len(bits) else 0
            for right, flag, cur in ((False, kb.JOINSIDE_LEFT_KEPT, spec.cur_lsb), (True, kb.JOINSIDE_RIGHT_KEPT, spec.cur_rsb)):
                if b & flag and cur == cur:
                    v = (wanted[i][1 if right else 0] - cur) * per
                    if v == v:
                        raw.append((i, right, v))
        lower = sorted(v for i, _r, v in raw if i in set(az))
        if not lower:
            return
        offset = lower[len(lower) // 2]
        self.text += ("\nDrawing advice for %d kept sides, relative to the font's own body rhythm: overall the model "
                      "would set the bodies %s units per 1000 em %s." % (
                          len(raw), _num(abs(offset)), "closer together" if offset < 0 else "further apart"))
        rows = sorted(((i, r, v - offset) for i, r, v in raw), key=lambda x: -abs(x[2]))
        for i, right, v in rows:
            if abs(v) < 5:
                continue
            name = snap.names[i]
            self.advice.append(dict(
                what="%s %s" % (name, "right" if right else "left"),
                finding="the body sits further out" if v < 0 else "the body sits closer in",
                value=_signed(v), note="shorten the %s stroke" % ("exit" if right else "entry") if v < 0 else
                "lengthen the %s stroke" % ("exit" if right else "entry"),
                proof=self._side_proof(i, right, az)))

    def _show(self):
        rows = {FINDINGS: self.findings, PREVIEW: self.under, ADVICE: self.advice}.get(self.view, []) \
            if hasattr(self, "findings") else []
        self.rows = rows
        self.w.list.set([dict(what=r["what"], finding=r["finding"], value=r["value"], note=r["note"]) for r in rows])
        empty = {FINDINGS: "No broken, nearly touching, fragile or crossing joins among a–z.",
                 PREVIEW: "Nothing breaks." if self.kw.result is not None else "",
                 ADVICE: "Every kept body sits within 5 units of the font's own rhythm (or the mode is Space joined letters)."}
        self.w.status.set("%d rows" % len(rows) if rows else empty.get(self.view, ""))

    def openProof(self, sender=None):
        """The selected rows in context, one line each: n + pair + n."""
        snap = self.kw.snapshot
        if snap is None or not self.rows:
            return
        n = "n" if "n" in snap.index else snap.names[0]
        sel = self.w.list.getSelection() or [0]
        lines = [[(n, a, b, n) for a, b in self.rows[k]["proof"]] for k in sel[:24] if k < len(self.rows)]
        if lines:
            self.host.open_proof(lines)
