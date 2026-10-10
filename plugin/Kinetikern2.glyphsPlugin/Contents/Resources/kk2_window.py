# encoding: utf-8
"""
kk2_window — the Kinetikern2 window (vanilla + PyObjC).

Layout: physics and kerning controls on top, a progress row with Cancel under
them, then a vertical split with two proofing panes side by side (left: the
font as it is, with its own spacing and kerning; right: the live engine
result) over the editable multi-paragraph sample text. Editing or scrolling
the sample text updates and scrolls both panes.

Nothing here waits. One NSTimer in the common run-loop modes (so it keeps
firing during slider drags, open menus and the confirmation alert, where the
window ignores it) drives everything:

* reading — kk2_snapshot.SnapshotReader copies the master out of Glyphs in
  slices of at most 8 ms per tick;
* engine jobs — Phase 1 (prepare), the sample-text preview and whole-font
  solves run on the library's own threads; the timer polls them at ~15 Hz
  (a few atomic loads) and takes the output once a job is done;
* Apply and Revert — kk2_apply's Planner, Applier and Restorer, in slices
  of at most 8 ms per tick, with progress;
* the proof panes — each is laid out on a tick of its own, a pause after the
  last keystroke in the sample text.

There is at most one engine job at a time. A change of the physics, the
threshold or the glyphs of the sample text replaces the running preview (the
newest request wins: a slow preview is cancelled at once, a fast one may land
first so that a slider drag keeps showing results). A whole-font run disables
the controls that would restart it and can be cancelled. Apply plans, asks in
an NSAlert (the only modal wait), writes, and keeps a kk2_apply.RevertPoint for
Revert Last Apply; the controls are disabled meanwhile. After an Apply or a
Revert the snapshot no longer matches the font (groups, sidebearings): the
next Apply reads the master again first.

Only the pairs of the sample text are ever looked at here: the right pane asks
the result for the values of its adjacent glyph pairs, the left pane looks the
same pairs up in the master's kerning table.
"""

from __future__ import division, print_function, unicode_literals

import math
import os
import time
import traceback
import weakref

import objc
import vanilla
from AppKit import NSAlert, NSAlertFirstButtonReturn, NSFont, NSObject
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer

import kk2_apply as ka
import kk2_args
import kk2_bridge as kb
import kk2_groups as kg
import kk2_harness as kh
import kk2_proof as kp
import kk2_snapshot as ks

try:
    from GlyphsApp import Glyphs, Message
except ImportError:  # outside Glyphs (tests)
    Glyphs = None
    Message = None

PREFIX = "com.mirkovelimirovic.Kinetikern2."
# preferences the window remembers between sessions (keys under PREFIX)
PERSISTED = ("tightness", "intensity", "threshold", "maxPairs", "threads", "scope", "size", "sample", "replace")
SAMPLE_TEXT = (
    "Hamburgefonstiv HOHOHOHO nonononono\n"
    "AVATAR TYPE WAVE LT Tolerance Yo Ta Te Vo P. F, L’\n"
    "The quick brown fox jumps over the lazy dog. Typography is the craft of endowing "
    "human language with a durable visual form.\n"
    "/T/o/T/a/T/e /V/A/V/o /P/A /r/period /y/period /f/parenright"
)
SIZES = ["24", "36", "48", "60", "72", "96", "128"]
SCOPES = ["Glyphs in sample text", "Whole font"]
SCOPE_SAMPLE, SCOPE_WHOLE = 0, 1
LOOSENESS_RATIO = 3.86
DEFAULT_THRESHOLD = 5.0  # units per 1000 em
MAX_THRESHOLD = 20.0
DEFAULT_MAX_PAIRS = 30000
# the Connected script modes (the join mode popup)
JOIN_MODES = ["Keep joins", "Space joined letters"]
JOIN_KEEP, JOIN_SPACE = 0, 1
LARGE_MAX_PAIRS = 100000  # above this the window warns about file size

READ_SLICE = 0.008  # seconds of outline reading (and of planning, applying, reverting) per timer tick
READ_INTERVAL = 0.012  # timer interval while reading: the run loop gets the rest
POLL_INTERVAL = 1.0 / 15.0  # timer interval while an engine job runs
KEY_BUDGET = 5  # index of the pair budget in _solve_key()
PREVIEW_KEEP = 0.25  # a preview that took less than this may finish when the next is asked for
PROGRESS_DELAY = 0.25  # previews show progress only when they take longer than this
TYPING_PAUSE = 0.15  # the panes are laid out again this long after the last keystroke


def _class(name, factory):
    """One Objective-C class per process: Glyphs runs every plugin in one runtime."""
    try:
        return objc.lookUpClass(name)
    except objc.nosuchclass_error:
        return factory()


def _make_timer_class():
    class KK2WindowTimer(NSObject):
        """Target of the window's NSTimer: hands each tick to Python."""

        def tick_(self, timer):
            callback = getattr(self, "kk2_callback", None)
            if callback is None:
                return
            try:
                callback()
            except Exception:  # never let an exception unwind into the run loop
                print(traceback.format_exc())

    return KK2WindowTimer


KK2WindowTimer = _class("KK2WindowTimer", _make_timer_class)


def physics_from_sliders(tightness, intensity):
    """Slider positions → (spring, repulsion, coupling).

    `tightness` runs from -1 (tight) to +1 (loose). The springs pull, the
    bounding repulsion pushes, and their ratio sets the rest gap. At 0 the
    ratio is 3.86, the median best fit to the 30 most popular Google Fonts
    families (v1 tools/benchmark.py). `intensity` is the SDF kerning intensity
    in percent (the contour-field coupling; 100 % is calibrated against the
    same fonts' kerning).
    """
    spring = math.exp(-0.55 * tightness)
    repulsion = LOOSENESS_RATIO * math.exp(0.55 * tightness)
    return spring, repulsion, max(0.0, intensity / 100.0)


# The argument domain, and whether this is a test instance (selfTestFont /
# devScript arguments): the window then reads its settings from the arguments
# only and writes no preferences (the user's defaults domain is shared with
# that instance).
arguments = kk2_args.arguments
unattended = kk2_args.unattended


def _as_bool(value):
    if isinstance(value, (bytes, str)):
        return value.strip().lower() in ("1", "yes", "true", "y")
    return bool(value)


def _count(n):
    return "{:,}".format(int(n))


_engines = {}


def load_engine(resources):
    """The engine library (loaded once per process)."""
    path = os.path.join(resources, kb.DYLIB_NAME)
    engine = _engines.get(path)
    if engine is None:
        engine = _engines[path] = kb.Engine(path)
    return engine


class FontKerning(object):
    """The kerning a master holds, looked up pair by pair the way Glyphs
    resolves it: glyph–glyph, glyph–group, group–glyph, group–group. Only the
    pairs of the sample text are looked up; the table is never walked."""

    def __init__(self, font):
        self.font = font
        self._keys = {}

    def reset(self):
        """Forget glyph ids and groups (after an apply or a revert)."""
        self._keys = {}

    def table(self, master_id):
        for attribute in ("kerningLTR", "kerning"):  # `kerning` before Glyphs 3.2
            try:
                return getattr(self.font, attribute)[master_id]
            except Exception:
                continue
        return None

    def keys(self, name):
        """(glyph id, key as a left glyph's group, key as a right glyph's group)."""
        keys = self._keys.get(name)
        if keys is None:
            glyph = self.font.glyphs[name]
            if glyph is None:
                keys = (None, None, None)
            else:
                left_side = glyph.rightKerningGroup  # the group used when the glyph stands on the left
                right_side = glyph.leftKerningGroup
                keys = (glyph.id, "@MMK_L_" + left_side if left_side else None,
                        "@MMK_R_" + right_side if right_side else None)
            self._keys[name] = keys
        return keys

    def value(self, table, left, right):
        if not table:
            return 0.0
        left_id, left_group, _ = self.keys(left)
        right_id, _, right_group = self.keys(right)
        for a, b in ((left_id, right_id), (left_id, right_group), (left_group, right_id), (left_group, right_group)):
            if a is None or b is None:
                continue
            row = table.get(a)
            if row is None:
                continue
            v = row.get(b)
            if v is not None:
                v = float(v)
                return v if abs(v) < 1.0e6 else 0.0  # Glyphs' "no kerning" marker
        return 0.0


class KK2Window(object):

    def __init__(self, resources):
        # read by kk2_selftest and tools/kk2_ui_smoke_test.py
        self.w = None
        self.state = "error"  # reading | preparing | previewing | ready | solving | applying | error
        self.font = Glyphs.font
        self.snapshot = None
        self.context = None
        self.result = None
        self.last_apply = None
        self.last_error = None
        self._leases = {}  # id(context or result) -> [object, count, close when released]
        self.revert_point = None
        self.tick_ms = {}  # longest timer tick per kind of work, in ms (diagnostics for kk2_selftest)
        self.last_tick = None  # (kind of work, ms) of the latest tick
        self.last_revert = None  # counts of the last Revert Last Apply
        if self.font is None:
            Message("Open a font first.", title="Kinetikern2")
            return
        try:
            self.engine = load_engine(resources)
        except Exception as e:
            Message("The Kinetikern2 engine could not be loaded:\n%s\n\n%s"
                    % (os.path.join(resources, kb.DYLIB_NAME), e), title="Kinetikern2")
            return
        self._unattended = kk2_args.unattended()
        self._arguments = kk2_args.arguments() if self._unattended else None
        self.tokens = []
        self._generation = 0  # counts snapshots: results of an older one are never reused
        self._reader = None
        self._read_done = False  # the reader finished; its snapshot is taken on the next tick
        self._job = None  # the one engine job: prepare | preview | whole
        self._job_kind = None
        self._job_key = None
        self._job_requested = None
        self._job_elapsed = 0.0
        self._preview_pending = False
        self._preview_started = 0.0
        self._preview_seconds = None  # engine time of the last preview that finished
        self._apply_when_done = False  # whole-font run started by Apply to Font
        self._apply_after_preview = False  # Apply to Font (sample scope) while a preview runs
        self._whole_after_preview = False  # Apply to Font (whole font) after re-reading the master
        self._result_kind = None  # "preview" | "whole"
        self._result_key = None
        self._requested = None  # glyph indices the current result kerned (None: all)
        self._stepper = None  # kk2_apply Planner | Applier | Restorer at work
        self._stepper_kind = None  # "plan" | "apply" | "revert"
        self._confirm_plan = False  # ask before applying the plan being made
        self._font_written = False  # Apply or Revert changed the master since it was read
        self._left_due = self._right_due = False  # panes waiting for a tick of their own
        self._render_at = 0.0  # not before this time (a pause after typing)
        self._ruled = set()  # glyphs with a side driven by a metrics key or an aligned component
        self._stale = set()  # glyphs whose metrics in the font differ from the snapshot
        self._font_metrics = {}
        self._applied_names = set()
        self._kerning = FontKerning(self.font)
        # spacing groups (kept in the font's userData) and their windows
        self.groups = kg.GroupSet.load(self.font)
        self.groups_window = None
        self.pairs_window = None
        # the designer harness: its window, the plan of the running job and of the result shown
        self.harness_window = None
        # the conventions it follows: text faces, display or handwriting punctuation
        self.harness_style = self._setting("harnessStyle", "text", str)
        if self.harness_style not in [k for k, _label, _cat in kh.STYLES]:
            self.harness_style = "text"
        self._harness_cache = None
        self._job_harness = None
        self._result_harness = None
        self._fitted = None  # Looseness offset the last solve fitted to the frozen glyphs
        # a connected script: the joins the context was prepared with (None: none)
        self.joins = None
        self.join_count = 0
        self.join_note = ""
        self.join_ms = 0.0
        self.join_kinds = None  # JOINKIND_* per glyph, and the master's kerning, for the join checker
        self.join_decorated = False  # the glyphs touch by construction (kk2_bridge.Engine.join_decorated)
        self.join_current = ()
        self.join_check = None  # (stats, sides) of the result shown (kk2_bridge.Engine.join_check)
        self.joins_window = None
        self._timer = None
        self._timer_interval = None
        self._timer_target = None
        self._modal = False
        self.sync = kp.KK2PaneSync.alloc().init()
        t = time.perf_counter()
        self._build()
        self.revert_point = _adopt_revert_point(self.font)  # an Apply made before the window was closed
        t1 = time.perf_counter()
        self._load_master(self._master())
        self.open_ms = {"build": 1000.0 * (t1 - t), "load": 1000.0 * (time.perf_counter() - t1)}

    # ------------------------------------------------------------ settings
    def _setting(self, key, fallback, kind=float):
        if self._unattended:
            value = self._arguments.get(PREFIX + key) if self._arguments else None
        else:
            value = Glyphs.defaults[PREFIX + key]
        if value is None:
            return fallback
        try:
            return _as_bool(value) if kind is bool else kind(value)
        except (TypeError, ValueError):
            return fallback

    def _save(self, key, value):
        if not self._unattended:
            Glyphs.defaults[PREFIX + key] = value

    # ------------------------------------------------------------------ UI
    def _build(self):
        w = vanilla.FloatingWindow((1240, 860), "Kinetikern2 — %s" % self.font.familyName, minSize=(1200, 600),
                                   autosaveName=None if self._unattended else PREFIX + "window")
        self.w = w

        # row 1: physics and threshold
        x = 14
        w.tightLabel = vanilla.TextBox((x, 10, 220, 17), "Looseness / Tightness", sizeStyle="small")
        w.tightMin = vanilla.TextBox((x, 32, 34, 14), "Tight", sizeStyle="mini")
        w.tightness = vanilla.Slider((x + 34, 28, 240, 22), minValue=-1.0, maxValue=1.0,
                                     value=self._setting("tightness", 0.0), callback=self.physicsChanged)
        w.tightMax = vanilla.TextBox((x + 280, 32, 40, 14), "Loose", sizeStyle="mini")
        w.tightValue = vanilla.TextBox((x, 52, 330, 14), "", sizeStyle="mini")

        x = 360
        w.sdfLabel = vanilla.TextBox((x, 10, 220, 17), "SDF Kerning Intensity", sizeStyle="small")
        w.intensity = vanilla.Slider((x, 28, 210, 22), minValue=0.0, maxValue=200.0,
                                     value=self._setting("intensity", 100.0), callback=self.physicsChanged)
        w.sdfValue = vanilla.TextBox((x, 52, 230, 14), "", sizeStyle="mini")

        x = 600
        threshold = min(MAX_THRESHOLD, max(0.0, self._setting("threshold", DEFAULT_THRESHOLD)))
        w.thresholdLabel = vanilla.TextBox((x, 10, 280, 17), "Ignore threshold (units per 1000 em)",
                                           sizeStyle="small")
        w.threshold = vanilla.Slider((x, 28, 170, 22), minValue=0.0, maxValue=MAX_THRESHOLD, value=threshold,
                                     callback=self.thresholdChanged)
        w.thresholdField = vanilla.EditText((x + 178, 28, 48, 21), "%g" % round(threshold, 1),
                                            callback=self.thresholdFieldChanged, sizeStyle="small")
        w.thresholdValue = vanilla.TextBox((x, 52, 280, 14), "", sizeStyle="mini")
        w.threshold.getNSSlider().setToolTip_(
            "Kerning values smaller than this are left out: the preview shows and Apply writes only pairs that "
            "matter.")

        x = 886
        w.harness = vanilla.CheckBox((x, 9, 180, 18), "Designer harness", sizeStyle="small",
                                     value=self._setting("harness", False, bool), callback=self.harnessChanged)
        w.harness.getNSButton().setToolTip_(
            "Nudges the result toward what the designers of well-spaced fonts do where Kinetikern2 consistently "
            "differs: more space in parentheses and around / ? ! &, less around quotes, period, comma and "
            "hyphen; more on the open sides of E, F, L and T (most at light weights and tight settings), less on "
            "the diagonals of A, V, W, Y. Learned from the text fonts on Google Fonts.")
        w.harnessStrength = vanilla.Slider((x, 28, 92, 22), minValue=0.0, maxValue=100.0,
                                           value=self._setting("harnessStrength", 100.0),
                                           callback=self.harnessChanged)
        w.harnessStrength.getNSSlider().setToolTip_("How much of the harness to apply (100 % = what the data say).")
        w.harnessButton = vanilla.Button((-96, 26, -14, 22), "Harness…", callback=self.openHarness,
                                         sizeStyle="small")
        w.harnessButton.getNSButton().setToolTip_("The pairs the harness changes most, drawn with and without it.")
        w.harnessValue = vanilla.TextBox((x, 52, -14, 14), "", sizeStyle="mini")

        # row 2: master, size, threads, budget
        w.masterLabel = vanilla.TextBox((14, 69, 46, 17), "Master", sizeStyle="small")
        w.master = vanilla.PopUpButton((62, 66, 170, 20), [m.name for m in self.font.masters],
                                       callback=self.masterChanged, sizeStyle="small")
        selected = self.font.selectedFontMaster  # start on the master the font window shows
        ids = [m.id for m in self.font.masters]
        w.master.set(ids.index(selected.id) if selected is not None and selected.id in ids else 0)
        w.sizeLabel = vanilla.TextBox((246, 69, 30, 17), "Size", sizeStyle="small")
        w.size = vanilla.PopUpButton((278, 66, 64, 20), SIZES, callback=self.sizeChanged, sizeStyle="small")
        size = str(self._setting("size", "48", str))
        w.size.set(SIZES.index(size) if size in SIZES else SIZES.index("48"))
        w.threadsLabel = vanilla.TextBox((358, 69, 52, 17), "Threads", sizeStyle="small")
        cores = self.engine.cpu_count
        w.threads = vanilla.PopUpButton(
            (410, 66, 160, 20), ["Auto (%d of %d cores)" % (self.engine.default_threads, cores)] +
            [str(n) for n in range(1, cores + 1)], callback=self.threadsChanged, sizeStyle="small")
        threads = int(self._setting("threads", 0, int))
        w.threads.set(threads if 0 <= threads <= cores else 0)
        w.maxPairsLabel = vanilla.TextBox((586, 69, 64, 17), "Max pairs", sizeStyle="small")
        max_pairs = self._setting("maxPairs", DEFAULT_MAX_PAIRS, int)
        w.maxPairs = vanilla.EditText((652, 66, 72, 21), str(max_pairs) if max_pairs > 0 else "",
                                      callback=self.maxPairsChanged, placeholder="unlimited", sizeStyle="small")
        w.maxPairs.getNSTextField().setToolTip_(
            "Most kerning entries a whole-font Apply writes (the least important are left out). Empty or 0: "
            "no limit.")
        w.maxPairsNote = vanilla.TextBox((732, 70, 112, 14), "", sizeStyle="mini")
        w.connected = vanilla.CheckBox((850, 67, 128, 18), "Connected script", sizeStyle="small",
                                       value=self._setting("connected", True, bool), callback=self.connectedChanged)
        w.connected.getNSButton().setToolTip_(
            "For scripts whose letters join. The joins are learned from the font's own spacing and kerning: when "
            "most lowercase letters overlap most of their partners, the font is a connected script. A font whose "
            "letters do not join is spaced as usual, so this can stay on.")
        mode = int(self._setting("joinMode", JOIN_KEEP, int))
        w.joinMode = vanilla.PopUpButton((980, 66, 150, 20), JOIN_MODES, callback=self.joinModeChanged,
                                         sizeStyle="small")
        w.joinMode.set(mode if mode in (JOIN_KEEP, JOIN_SPACE) else JOIN_KEEP)
        w.joinMode.getNSPopUpButton().setToolTip_(
            "Keep joins (the default): every letter side that joins keeps its sidebearing and every pair of two "
            "joining sides the font's kerning, so every join stays as drawn; Kinetikern2 spaces and kerns the "
            "rest (punctuation, figures, capitals that do not join, a letter next to a period) at the tightness "
            "of the kept letters. What it would have done to the joining sides is shown as drawing advice "
            "(Joins…).\n\nSpace joined letters: the letter bodies are spaced without their join strokes and two "
            "joining letters overlap with no kerning; joins whose strokes stop meeting break (Joins… counts them).")
        # the note lives in Joins…: the label keeps its text, hidden. (Not
        # (0, 0, 0, 0): a width or height of 0 stretches a vanilla view to the
        # window's edge, and the label then covered every control above.)
        w.connectedValue = vanilla.TextBox((14, 0, 10, 10), "", sizeStyle="mini")
        w.connectedValue.show(False)
        w.reload = vanilla.Button((-50, 64, 36, 22), "↻", callback=self.reloadOutlines, sizeStyle="small")
        w.reload.getNSButton().setToolTip_("Read the outlines of the selected master again")

        # row 3: apply
        w.scopeLabel = vanilla.TextBox((14, 99, 60, 17), "Apply to", sizeStyle="small")
        w.scope = vanilla.PopUpButton((74, 96, 180, 20), SCOPES, callback=self.scopeChanged, sizeStyle="small")
        scope = int(self._setting("scope", SCOPE_SAMPLE, int))
        w.scope.set(scope if scope in (SCOPE_SAMPLE, SCOPE_WHOLE) else SCOPE_SAMPLE)
        w.replace = vanilla.CheckBox((270, 97, 200, 18), "Replace existing kerning", sizeStyle="small",
                                     value=self._setting("replace", True, bool), callback=self.replaceChanged)
        w.replace.getNSButton().setToolTip_(
            "Remove the master's existing kerning between the glyphs and groups that get new kerning.")
        w.groupsButton = vanilla.Button((480, 94, 150, 22), "Spacing Groups…", callback=self.openGroups)
        w.groupsButton.getNSButton().setToolTip_(
            "Paint glyphs into colour-coded groups: each spaced with its own Looseness and kerning force, or "
            "frozen so only the rest of the font is spaced.")
        w.pairsButton = vanilla.Button((636, 94, 90, 22), "Pairs…", callback=self.openPairs)
        w.pairsButton.getNSButton().setToolTip_(
            "The font's pairs from loosest to tightest, measured against Kinetikern2.")
        w.joinsButton = vanilla.Button((732, 94, 136, 22), "Joins…", callback=self.openJoins)
        w.joinsButton.getNSButton().setToolTip_(
            "A connected script's joins: where the strokes meet as drawn (broken, nearly touching, partly connected, "
            "fragile and crossing joins), "
            "what the preview does to them, and drawing advice for the kept sides, with proofs.")
        w.revert = vanilla.Button((-318, 94, 160, 22), "Revert Last Apply", callback=self.revertLastApply)
        w.revert.getNSButton().setToolTip_(
            "Put the kerning, groups and sidebearings back as they were before the last Apply "
            "(Apply writes without undo).")
        w.apply = vanilla.Button((-150, 94, 136, 22), "Apply to Font", callback=self.applyToFont)

        # row 4: progress
        w.progress = vanilla.ProgressBar((14, 129, 260, 12), minValue=0, maxValue=100, sizeStyle="small")
        w.phase = vanilla.TextBox((286, 126, -312, 17), "", sizeStyle="small")
        w.alongSlant = vanilla.CheckBox((-300, 124, 196, 18), "Along the italic angle", sizeStyle="small",
                                        value=self._setting("alongSlant", True, bool), callback=self.alongSlantChanged)
        w.alongSlant.getNSButton().setToolTip_(
            "Measure an italic master along its italic angle (3° or more): Kinetikern2 sees the outlines sheared "
            "upright about half the x-height, where Glyphs measures italic sidebearings, the way its model, built "
            "on upright letters, measures. Sidebearings and kerning are horizontal, so they apply to the slanted "
            "outlines as they are. Measured upright instead, italics come out too loose and uneven.")
        w.cancel = vanilla.Button((-100, 122, 86, 22), "Cancel", callback=self.cancelJob)

        w.line = vanilla.HorizontalLine((0, 152, -0, 1))
        w.status = vanilla.TextBox((14, -22, -14, 16), "", sizeStyle="mini")

        # proofing panes side by side
        self.left = kp.ProofPane("Font")
        self.right = kp.ProofPane("Kinetikern2")
        left_group = vanilla.Group((0, 0, -0, -0))
        left_group.caption = vanilla.TextBox((10, 4, -10, 16), "Font as it is — current spacing and kerning",
                                             sizeStyle="small")
        left_group.scroll = vanilla.ScrollView((0, 22, -0, -0), self.left.view, hasHorizontalScroller=False,
                                               autohidesScrollers=True)
        right_group = vanilla.Group((0, 0, -0, -0))
        right_group.caption = vanilla.TextBox((10, 4, -10, 16), "Kinetikern2 — live sidebearings + kerning",
                                              sizeStyle="small")
        right_group.scroll = vanilla.ScrollView((0, 22, -0, -0), self.right.view, hasHorizontalScroller=False,
                                                autohidesScrollers=True)
        proofs = vanilla.SplitView((0, 0, -0, -0), [
            dict(view=left_group, identifier="font", minSize=250),
            dict(view=right_group, identifier="kinetikern2", minSize=250),
        ], isVertical=True, dividerStyle="thin")

        input_group = vanilla.Group((0, 0, -0, -0))
        input_group.caption = vanilla.TextBox((10, 4, -10, 16),
                                              "Sample text (multi-paragraph; /glyphname inserts a glyph)",
                                              sizeStyle="small")
        input_group.editor = vanilla.TextEditor((0, 22, -0, -0), self._setting("sample", SAMPLE_TEXT, str),
                                                callback=self.textChanged)
        self.editor = input_group.editor
        tv = self.editor.getNSTextView()
        tv.setFont_(NSFont.systemFontOfSize_(13.0))
        tv.setAutomaticQuoteSubstitutionEnabled_(False)
        tv.setAutomaticDashSubstitutionEnabled_(False)
        tv.setAutomaticTextReplacementEnabled_(False)

        w.split = vanilla.SplitView((0, 154, -0, -26), [
            dict(view=proofs, identifier="proofs", minSize=220),
            dict(view=input_group, identifier="input", size=190, minSize=90),
        ], isVertical=False, dividerStyle="thin")
        self.left_group, self.right_group, self.input_group = left_group, right_group, input_group

        w.bind("close", self.windowClosed)
        w.open()
        panes = [left_group.scroll.getNSScrollView(), right_group.scroll.getNSScrollView()]
        self.sync.attach(panes + [self.editor.getNSScrollView()], fit_width=panes)
        self._update_labels()
        self._update_max_pairs_note()

    def _master(self):
        return self.font.masters[self.w.master.get()]

    def _physics(self):
        return physics_from_sliders(self.w.tightness.get(), self.w.intensity.get())

    def _point_size(self):
        return float(SIZES[self.w.size.get()])

    def _threads(self):
        return int(self.w.threads.get())  # item 0 is Auto (0), item n is n threads

    def _threshold(self):
        """Ignore threshold in units per 1000 em."""
        return min(MAX_THRESHOLD, max(0.0, float(self.w.threshold.get())))

    def _upm(self):
        return float(self.snapshot.upm if self.snapshot is not None else self.font.upm)

    def _budget(self):
        """Max pairs for a whole-font run; 0 = unlimited."""
        text = (self.w.maxPairs.get() or "").strip().replace(",", "").replace(".", "").replace(" ", "")
        try:
            return max(0, int(text)) if text else 0
        except ValueError:
            return DEFAULT_MAX_PAIRS

    def _glyph_opts(self):
        """Per-glyph options of the spacing groups (None: no group changes anything)."""
        if self.snapshot is None or not self.engine.features & kb.FEATURE_GLYPH_OPTS:
            return None
        return self.groups.opts_for(self.snapshot.names)

    def _kept_sides(self):
        """(left sides, right sides) Keep joins keeps as drawn, glyph
        indices; None when the context keeps none."""
        if not self._keeps_joins() or self.context is None:
            return None
        try:
            bits = self.engine.join_sides(self.context)
        except Exception:
            print(traceback.format_exc())
            return None
        left = [i for i, b in enumerate(bits) if b & kb.JOINSIDE_LEFT_KEPT]
        right = [i for i, b in enumerate(bits) if b & kb.JOINSIDE_RIGHT_KEPT]
        return (left, right) if left or right else None

    def _keeps_joins(self):
        """The context keeps a connected script's joins (Keep joins)."""
        return self.joins is not None and self.join_kinds is not None and self._join_mode() == JOIN_KEEP

    def _join_mode(self):
        if self.w is None or getattr(self.w, "joinMode", None) is None:
            return int(self._setting("joinMode", JOIN_KEEP, int))
        return int(self.w.joinMode.get())

    def _params(self, budget):
        spring, repulsion, coupling = self._physics()
        opts = self._glyph_opts()
        # frozen glyphs (Match the frozen spacing) and kept joins set the tightness
        fit = (bool(opts) and self.groups.match_frozen and any(o[0] for o in opts)) or self._keeps_joins()
        return kb.make_params(spring=spring, repulsion=repulsion, coupling=coupling, classes=True, window=True,
                              scope_scripts=True, threshold=self._threshold() * self._upm() / 1000.0,
                              budget=budget, threads=self._threads(), fit_frozen=fit)

    def _solve_key(self, whole):
        """What a result depends on, to tell whether it can be reused (the
        budget at KEY_BUDGET)."""
        spring, repulsion, coupling = self._physics()
        return (self._generation, round(spring, 9), round(repulsion, 9), round(coupling, 9),
                round(self._threshold(), 6), self._budget() if whole else 0, self.groups.key(),
                self.join_count, self._join_mode(), self.harness_style, round(self._harness_strength(), 4))

    def _update_labels(self):
        if self.w is None:
            return
        spring, repulsion, coupling = self._physics()
        gap = ""
        if self.result is not None and self.result.ptr is not None:
            rest = self.result.stats["rest_gap"]
            parts = [("lc", rest[kb.GROUP_LOWERCASE]), ("UC", rest[kb.GROUP_UPPERCASE])]
            parts = ["%s %d" % (k, v) for k, v in parts if v == v and v > 0]
            gap = " · rest gap " + ", ".join(parts) if parts else ""
        fitted = ""
        if self._fitted is not None:
            fitted = " · matched to the %s: %+.2f" % (
                "kept joins" if self._keeps_joins() else "frozen glyphs", self._fitted)
        self.w.tightValue.set("spring %.2f · repulsion %.2f%s%s" % (spring, repulsion, gap, fitted))
        self.w.sdfValue.set("contour field coupling β = %.2f%s" % (coupling, " (no kerning)" if coupling == 0 else ""))
        t = self._threshold()
        upm = self._upm()
        if t > 0:
            text = "|kern| under %g/1000 em is left out" % round(t, 1)
            if abs(upm - 1000.0) > 0.5:
                text += " (%.1f units at %d UPM)" % (t * upm / 1000.0, upm)
        else:
            text = "every non-zero kern is kept"
        self.w.thresholdValue.set(text)
        self._update_harness_label()
        self._update_join_label()

    def _update_harness_label(self):
        if self.w is None or getattr(self.w, "harness", None) is None:
            return
        if not self.harness_available():
            text = ("unavailable: rebuild the plugin (build.sh)" if not self.engine.features & kb.FEATURE_HARNESS
                    else "unavailable: kk2_harness.json is missing")
        elif not self.w.harness.get():
            text = "off · Harness… shows what it would change"
        else:
            plan = self._harness_plan()
            text = plan.summary() if plan is not None else "on · waiting for the font to be read"
        self.w.harnessValue.set(text)
        self.w.harnessStrength.enable(bool(self.w.harness.get()) and self.harness_available())

    def _update_max_pairs_note(self):
        n = self._budget()
        if n == 0:
            note, tip = "unlimited: large files", ("A whole font can produce hundreds of thousands of kerning "
                                                  "entries: big source files, slow exports, large kern tables.")
        elif n > LARGE_MAX_PAIRS:
            note, tip = "large: big files", "More than %s entries make big source files and slow exports." % _count(
                LARGE_MAX_PAIRS)
        else:
            note, tip = "", None
        self.w.maxPairsNote.set(note)
        self.w.maxPairsNote.getNSTextField().setToolTip_(tip)

    def _update_controls(self):
        w = self.w
        if w is None:
            return
        solving = self._job is not None and self._job_kind == "whole"
        busy = solving or self._stepper is not None or self.state == "applying"
        for control in (w.tightness, w.intensity, w.threshold, w.thresholdField, w.maxPairs, w.threads, w.master,
                        w.scope, w.replace, w.reload):
            control.enable(not busy)
        w.connected.enable(not busy and bool(self.engine.features & kb.FEATURE_JOINS))
        w.joinMode.enable(not busy and bool(w.connected.get()) and bool(self.engine.features & kb.FEATURE_JOIN_CHECK))
        snap = self.snapshot
        w.alongSlant.enable(not busy and snap is not None and ks.slant_measurable(snap.slant_degrees))
        w.apply.enable(not busy and self.context is not None)
        w.revert.enable(not busy and self._reader is None and self.revert_point is not None)
        # a plan can be dropped; writes, once started, run to the end
        w.cancel.enable(self._reader is not None or self._job is not None or self._stepper_kind == "plan")

    def _set_status(self, text):
        if self.w is not None:
            self.w.status.set(text)

    def _show_progress(self, text, fraction):
        if self.w is None:
            return
        # straight to the NSProgressIndicator: vanilla's set() spins the run loop
        bar = self.w.progress.getNSProgressIndicator()
        bar.setDoubleValue_(100.0 * max(0.0, min(1.0, fraction)))
        bar.setHidden_(False)
        self.w.phase.set(text)

    def _idle_progress(self, text=""):
        """Nothing runs: the bar hides (instead of sweeping back to 0 on screen)."""
        if self.w is None:
            return
        bar = self.w.progress.getNSProgressIndicator()
        bar.setHidden_(True)
        bar.setDoubleValue_(0.0)
        self.w.phase.set(text)

    def _set_state(self, state):
        self.state = state
        self._update_controls()

    def _fail(self, message, trace=None):
        self.last_error = trace or message  # for the self-test and bug reports
        if trace:
            print(trace)
        self._set_state("error")
        self._idle_progress()
        self._set_status(message)

    # --------------------------------------------------------------- timer
    def _run_timer(self, interval):
        """(Re)starts the window's one timer at `interval` seconds."""
        if self.w is None:
            return
        if self._timer is not None and self._timer_interval == interval:
            return
        self._stop_timer()
        if self._timer_target is None:
            self._timer_target = KK2WindowTimer.alloc().init()
            self._timer_target.kk2_callback = self._tick
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self._timer_target, "tick:", None, True)
        timer.setTolerance_(interval * 0.1)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        self._timer = timer
        self._timer_interval = interval

    def _stop_timer(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
            self._timer_interval = None

    def _tick(self):
        if self.w is None or self._modal:
            return
        t0 = time.perf_counter()
        what = "poll"
        try:
            if self._reader is not None:
                what = "reading"
                self._step_reader()
                return
            if self._stepper is not None:
                what = "%s: %s" % (self._stepper_kind, self._stepper.phase)
                self._step_stepper()
                return
            if (self._left_due or self._right_due) and time.time() >= self._render_at:
                # one pane per tick: each lays out in its own display pass;
                # what a long sample needs for the first time is made first,
                # a slice per tick
                pane = self.left if self._left_due else self.right
                what = "left pane" if self._left_due else "right pane"
                if self.snapshot is not None and not pane.warm(self.tokens, self.snapshot, self._point_size(),
                                                               READ_SLICE):
                    what += " (warming)"
                    return
                if self._left_due:
                    self._render_left()
                else:
                    self._render_right()
                return
            if self._job is not None:
                what = "poll " + (self._job_kind or "")
                self._poll_job()
            if self._job is None and self._preview_pending:
                what = "start preview"
                self._start_preview()
            if (self._reader is None and self._job is None and not self._preview_pending and
                    self._stepper is None and not self._left_due and not self._right_due):
                self._stop_timer()
        except Exception:
            self._fail("Kinetikern2 stopped on an error — see the Macro panel.", traceback.format_exc())
        finally:
            ms = 1000.0 * (time.perf_counter() - t0)
            self.last_tick = (what, ms)
            if ms > self.tick_ms.get(what, 0.0):
                self.tick_ms[what] = ms

    # ------------------------------------------------------------- reading
    def _load_master(self, master):
        """Read `master` again from scratch: outlines (sliced), then Phase 1."""
        self._release_engine()
        self.snapshot = None
        self._font_written = False
        self._generation += 1
        self._stale = set()
        self._font_metrics = {}
        self._kerning.reset()
        self._reader = ks.SnapshotReader(self.font, master, along_slant=self._along_slant())
        self._read_done = False
        self._set_state("reading")
        self._show_progress("Reading outlines [0%]", 0.0)
        self._set_status("Reading the outlines of %s…" % master.name)
        self._run_timer(READ_INTERVAL)

    def _step_reader(self):
        reader = self._reader
        if self._read_done:
            # read on the previous tick; the panes and Phase 1 get a tick of their own
            self._reader = None
            self._run_timer(POLL_INTERVAL)
            self._snapshot_ready(reader.snapshot)
            return
        try:
            self._read_done = reader.step(READ_SLICE)
        except Exception:
            self._reader = None
            self._fail("Could not read the outlines — see the Macro panel.", traceback.format_exc())
            return
        fraction = 1.0 if self._read_done else reader.fraction
        self._show_progress("Reading outlines [%d%%]" % int(100 * fraction), fraction)

    def _snapshot_ready(self, snapshot):
        self.snapshot = snapshot
        self._update_slant_label()
        self._ruled = set(s.name for s in snapshot.specs
                          if s.lsb_rule != kb.RULE_FREE or s.rsb_rule != kb.RULE_FREE)
        if self.groups_window is not None:
            try:
                self.groups_window.refresh()
            except Exception:
                print(traceback.format_exc())
        self._harness_cache = None
        if self.harness_window is not None:
            try:
                self.harness_window.refresh()
            except Exception:
                print(traceback.format_exc())
        self.tokens = ks.tokenize(self.editor.get(), snapshot)
        self._render_left()
        self._panes_due(right=True)  # the font as it is until the first result
        if not snapshot.names:
            self._fail("%s has no exporting letters, figures, punctuation or symbols to space." %
                       snapshot.master_name)
            return
        self._start_prepare()

    def _start_prepare(self):
        """Phase 1 on the snapshot read: with a connected script's joins when
        the setting is on (found first, in milliseconds)."""
        snapshot = self.snapshot
        joins = self._detect_joins(snapshot) if self._connected() else None
        if not self._connected():
            self.joins, self.join_count, self.join_note = None, 0, ""
        self.join_check = None
        self._update_join_label()
        try:
            if joins is not None and self.join_kinds is not None and self.engine.features & kb.FEATURE_JOIN_CHECK:
                # the join checker, and Keep joins or Space joined letters
                self._job = self.engine.prepare(snapshot.packer, snapshot.upm, self._threads(), joins=joins,
                                                join_kinds=self.join_kinds, current=self.join_current,
                                                keep_joins=self._join_mode() == JOIN_KEEP)
            else:
                self._job = self.engine.prepare(snapshot.packer, snapshot.upm, self._threads(), joins=joins)
        except Exception as e:
            self._fail("The engine could not start: %s" % e, traceback.format_exc())
            return
        self._job_kind = "prepare"
        self._job_elapsed = 0.0
        self._set_state("preparing")
        self._show_progress("Phase 1/3: %s [0%%]" % kb.PHASE_NAMES[1], 0.0)
        skipped = getattr(snapshot, "glyph_count_skipped", 0)
        self._set_status("Analyzing %s glyphs of %s (outlines read in %.0f ms%s)%s…" % (
            _count(len(snapshot.names)), snapshot.master_name, snapshot.read_ms,
            ", %s glyphs without outlines or not exporting left out" % _count(skipped) if skipped else "",
            " · connected script: %s" % self.join_note if self._connected() and self.join_note else ""))

    # ------------------------------------------------------ connected scripts
    def _connected(self):
        """The Connected script setting is on (and the engine has the mode)."""
        return (self.w is not None and bool(self.w.connected.get())
                and bool(self.engine.features & kb.FEATURE_JOINS))

    def _x_height(self, snap):
        """The ink top of x, else the master's x-height (what Spacing QA uses)."""
        info = snap.infos.get("x")
        if info is not None and not info.empty:
            return info.bounds[1] + info.bounds[3]
        return snap.x_height

    def _detect_joins(self, snap):
        """The join bands of a connected script, learned from the master's
        own spacing and kerning (Spacing QA's rule); None when its letters do
        not join. Sets the note the window shows, and the letters' kinds and
        the master's kerning the join checker reads."""
        self.joins, self.join_count = None, 0
        self.join_kinds, self.join_current = None, ()
        t = time.perf_counter()
        try:
            from kk2_pairs_window import current_kerning
            # every letter is measured against the basic a–z (Spacing QA's
            # partners): milliseconds even for a font of a thousand letters
            kinds = bytearray(len(snap.names))
            letters = 0
            for i, spec in enumerate(snap.specs):
                if spec.group in (kb.GROUP_LOWERCASE, kb.GROUP_UPPERCASE):
                    cp = getattr(snap.infos.get(snap.names[i]), "unicode", None)
                    kinds[i] = kb.JOINKIND_LOWER if cp is not None and 0x61 <= cp <= 0x7A else kb.JOINKIND_UPPER
                    letters += 1
            current = current_kerning(snap, self._kerning.table(snap.master_id))
            bands = self.engine.detect_joins(snap.packer, snap.upm, self._x_height(snap), kinds, current,
                                             kb.JOINRULE_BOTH)
        except Exception as e:
            print(traceback.format_exc())
            self.join_note = "could not look for joins: %s" % e
            return None
        finally:
            self.join_ms = 1000.0 * (time.perf_counter() - t)
        n = sum(1 for left, right in bands if left or right)
        if not n:
            # nothing overlaps; a line or grid drawn exactly from edge to edge
            # still touches every neighbour, figures included: keep it whole
            decorated = False
            try:
                decorated = self.engine.detect_decorated(snap.packer, snap.upm, kinds, current)
            except Exception:
                print(traceback.format_exc())
            if decorated:
                self.joins, self.join_count = bands, 0
                self.join_kinds, self.join_current = kinds, current
                self.join_note = ("its glyphs touch by construction (a line, a grid or an effect through every "
                                  "glyph): every side that touches is kept")
                return bands
            # strokes that meet flush, without overlapping: Spacing QA's rule
            # (at least half the a–z touch at least half their partners as set)
            joining, measured = 0, 0
            try:
                joining, measured = self.engine.detect_contact(snap.packer, snap.upm, kinds, current)
            except Exception:
                print(traceback.format_exc())
            if measured and joining >= 0.5 * measured:
                self.joins, self.join_count = bands, 0
                self.join_kinds, self.join_current = kinds, current
                self.join_note = "%s of %s lowercase letters join, touching without overlapping" % (
                    _count(joining), _count(measured))
                return bands
            # a hand that joins only in part: its exit strokes reach letters
            # print faces never join (n n, m i, u n): Spacing QA's rule
            partly, counts = False, None
            try:
                letters_az = bytearray(len(snap.names))
                for i, kind in enumerate(kinds):
                    if kind == kb.JOINKIND_LOWER:
                        letters_az[i] = getattr(snap.infos.get(snap.names[i]), "unicode", 0) or 0
                partly, counts = self.engine.detect_partly(snap.packer, snap.upm, self._x_height(snap), letters_az,
                                                           current)
            except Exception:
                print(traceback.format_exc())
            if partly:
                self.joins, self.join_count = bands, 0
                self.join_kinds, self.join_current = kinds, current
                pairs, joined, stem_pairs, stem_joined = counts
                self.join_note = ("joins in part: %s of %s lowercase pairs and %s of %s pairs of two stem letters "
                                  "(n n, m i, u n …), which print faces never join, join above the baseline" % (
                                      _count(joined), _count(pairs), _count(stem_joined), _count(stem_pairs)))
                return bands
            self.join_note = "no joins: its letters do not join as a script's do"
            return None
        self.joins, self.join_count = bands, n
        self.join_kinds, self.join_current = kinds, current
        self.join_note = "%s of %s letters join" % (_count(n), _count(letters))
        return bands

    def _update_join_label(self):
        if self.w is None or getattr(self.w, "connected", None) is None:
            return
        if not self.engine.features & kb.FEATURE_JOINS:
            text = "unavailable: rebuild the plugin (build.sh)"
        elif not self.w.connected.get():
            text = ""
        elif self.snapshot is None:
            text = "on · waiting for the font to be read"
        else:
            text = self.join_note
        self.w.connectedValue.set(text)
        # the checker's count on the Joins… button
        button = getattr(self.w, "joinsButton", None)
        if button is not None:
            title = "Joins…"
            if self.join_check is not None and self.join_check[0]["joins"]:
                js = self.join_check[0]
                # the basic a–z when the font has them (the count the eye knows)
                joins, broken = (js["az_joins"], js["az_broken"]) if js["az_joins"] else (js["joins"], js["broken"])
                title = ("%s joins break…" % _count(broken) if broken else "%s joins kept…" % _count(joins))
            elif self.joins is None and self.w.connected.get() and self.snapshot is not None:
                title = "No joins"
            button.setTitle(title)
            button.getNSButton().setToolTip_(
                (text + "\n\n" if text else "") +
                "A connected script's joins: where the strokes meet as drawn (broken, nearly touching, partly connected, "
            "fragile and crossing joins), "
                "what the preview does to them, and drawing advice for the kept sides, with proofs.")

    # ------------------------------------------------------ italic angle
    def _along_slant(self):
        """The Along the italic angle setting."""
        if self.w is None or getattr(self.w, "alongSlant", None) is None:
            return bool(self._setting("alongSlant", True, bool))
        return bool(self.w.alongSlant.get())

    def _update_slant_label(self):
        """The checkbox names the master's angle; it is disabled for an upright
        master (and for an angle of 60° or more, which is an error in the font)."""
        box = getattr(self.w, "alongSlant", None) if self.w is not None else None
        if box is None:
            return
        snap = self.snapshot
        degrees = snap.slant_degrees if snap is not None else 0.0
        if snap is not None and ks.slant_measurable(degrees):
            box.setTitle("Along the %s° italic angle" % ("%g" % round(abs(degrees), 1)))
            box.enable(self.state not in ("applying",) and self._stepper is None)
        else:
            box.setTitle("Along the italic angle")
            box.enable(False)

    def alongSlantChanged(self, sender):
        self._save("alongSlant", bool(self.w.alongSlant.get()))
        if self.snapshot is None or self._reader is not None or self._stepper is not None:
            return  # the next read follows the setting
        if self._job is not None and self._job_kind == "whole":
            return
        self._load_master(self._master())  # the engine's frame changes: read again

    def joinModeChanged(self, sender):
        self._save("joinMode", int(self.w.joinMode.get()))
        self.connectedChanged(None)

    def connectedChanged(self, sender):
        self._save("connected", bool(self.w.connected.get()))
        self._update_controls()
        self._update_join_label()
        if self.snapshot is None or self._reader is not None:
            return  # the end of reading prepares with the setting
        if self._stepper is not None or (self._job is not None and self._job_kind == "whole"):
            return  # the checkbox is disabled meanwhile
        # Phase 1 again, with or without the joins; the preview follows
        self._release_engine()
        self._panes_due(right=True)
        self._start_prepare()
        self._run_timer(POLL_INTERVAL)

    # --------------------------------------------------------- engine jobs
    def _poll_job(self):
        job, kind = self._job, self._job_kind
        state, phase, phases, fraction, elapsed = job.poll()
        self._job_elapsed = elapsed
        if state == kb.STATE_RUNNING:
            if kind != "preview" or elapsed > PROGRESS_DELAY:
                if phases:
                    text = "Phase %d/%d: %s [%d%%]" % (phase, phases, kb.PHASE_NAMES.get(phase, ""),
                                                       int(100 * fraction))
                else:
                    text = "Starting…"
                if kind == "whole":
                    text += " · %.0f s" % elapsed
                elif kind == "preview":
                    text = "Preview — " + text
                self._show_progress(text, fraction)
            return
        self._job = None
        self._job_kind = None
        if state == kb.STATE_DONE:
            try:
                out = job.take()
            except Exception as e:
                job.free()
                self._job_failed(kind, str(e))
                return
            job.free()
            if kind == "prepare":
                self._context_ready(out)
            elif kind == "preview":
                self._preview_seconds = elapsed
                self._preview_ready(out)
            else:
                self._whole_ready(out)
        elif state == kb.STATE_FAILED:
            message = job.error() or self.engine.last_error()
            job.free()
            self._job_failed(kind, message)
        else:  # cancelled elsewhere
            job.free()
            self._set_state("ready" if self.context is not None else "error")
            self._idle_progress()

    def _job_failed(self, kind, message):
        self._apply_when_done = self._apply_after_preview = self._whole_after_preview = False
        what = {"prepare": "Phase 1", "preview": "The preview", "whole": "The whole-font run"}.get(kind, "The engine")
        self._fail("%s failed: %s" % (what, message or "unknown error"))

    def _context_ready(self, context):
        self.context = context
        # a design whose glyphs touch by construction (underline, charted,
        # guide lines): every touching side is kept, not only the letters'
        self.join_decorated = False
        if self.joins is not None:
            try:
                self.join_decorated = self.engine.join_decorated(context)
            except Exception:
                print(traceback.format_exc())
            if self.join_decorated:
                self.join_note = ("its glyphs touch by construction (a line, a grid or an effect through every "
                                  "glyph): every side that touches is kept")
                self._update_join_label()
        self._idle_progress()
        self._preview_pending = True
        self._start_preview()

    def _sample_indices(self):
        index = self.snapshot.index
        return frozenset(index[t] for t in self.tokens if t is not None and t in index)

    def _start_preview(self):
        """Kerns the glyphs of the sample text (Phase 2/3 on them, Pass 1 on all)."""
        if self._stepper is not None:
            return  # Apply or Revert at work: the preview waits (and the result they read stays)
        self._preview_pending = False
        if self.context is None or self.snapshot is None or self._job is not None:
            return
        requested = self._sample_indices()
        if self.harness_window is not None:
            requested = requested | self.harness_window.glyphs()  # the pairs it draws
        mask = bytearray(len(self.snapshot.names))
        for i in requested:
            mask[i] = 1
        try:
            plan = self._harness_plan()
            self._job = self.engine.solve(self.context, self._params(budget=0), bytes(mask),
                                          glyph_opts=self._glyph_opts(),
                                          harness=plan.engine_arg() if plan is not None else None)
            self._job_harness = plan
        except Exception as e:
            self._fail("The preview could not start: %s" % e, traceback.format_exc())
            return
        self._job_kind = "preview"
        self._job_key = self._solve_key(False)
        self._job_requested = requested
        self._job_elapsed = 0.0
        self._preview_started = time.time()
        self._set_state("previewing")
        self._run_timer(POLL_INTERVAL)

    def _request_preview(self):
        """The sample text or the physics changed: the newest preview wins."""
        if self.context is None or self.snapshot is None or self.w is None:
            return  # the end of Phase 1 starts a preview with the current settings
        if self._job is not None and self._job_kind == "whole":
            return  # its controls are disabled; the text only re-renders
        self._preview_pending = True
        if self._stepper is not None:
            return  # started once Apply or Revert is done
        if self._job is not None and self._job_kind == "preview":
            self._poll_job()  # a finished preview is shown before the next starts
            if self._job is not None and (self._preview_seconds is None or self._preview_seconds > PREVIEW_KEEP):
                self._job.free()  # slow and stale: cancel it
                self._job = None
                self._job_kind = None
        if self._job is None and time.time() - self._preview_started >= POLL_INTERVAL:
            self._start_preview()
        else:
            self._run_timer(POLL_INTERVAL)

    def _preview_ready(self, result):
        self._set_result(result, "preview", self._job_requested, self._job_key)
        self._idle_progress()
        if self._preview_pending:
            self._set_state("previewing")
            return  # the tick starts the next one
        self._set_state("ready")
        if self._apply_after_preview:
            self._apply_after_preview = False
            self._apply(self.result, confirm=True)
        elif self._whole_after_preview:
            self._whole_after_preview = False
            self.start_whole_font(apply_when_done=True)
        elif self.pairs_window is not None:
            self.pairs_window.preview_ready()  # a measurement waiting for the font to be read again

    def _whole_ready(self, result):
        self._set_result(result, "whole", None, self._job_key)
        self._idle_progress()
        self._set_state("ready")
        if self._apply_when_done:
            self._apply_when_done = False
            self._apply(self.result, confirm=True)
        elif self.pairs_window is not None:
            self.pairs_window.whole_ready()

    # -------------------------------------------------------------- leases
    def lease(self, obj):
        """Keeps an engine context or result open while another thread reads
        it (the Pairs window measuring): the window's own close of it waits
        for release()."""
        if obj is not None:
            entry = self._leases.setdefault(id(obj), [obj, 0, False])
            entry[1] += 1

    def release(self, obj):
        if obj is None:
            return
        entry = self._leases.get(id(obj))
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] <= 0:
            del self._leases[id(obj)]
            if entry[2]:
                obj.close()

    def _retire(self, obj):
        """Closes a context or result now, or when its last lease ends."""
        entry = self._leases.get(id(obj))
        if entry is not None:
            entry[2] = True
        else:
            obj.close()

    def _set_result(self, result, kind, requested, key):
        old = self.result
        self.result = result
        self._fitted = result.fitted_looseness if result is not None else None
        self._result_kind = kind
        self._requested = requested
        self._result_key = key
        self._result_harness = self._job_harness if result is not None else None
        if old is not None and old is not result:
            self._retire(old)
        self._check_joins()
        self._panes_due(right=True)
        self._update_labels()
        self._set_status(self._result_status())
        if self.harness_window is not None:
            self.harness_window.result_ready()
        if self.joins_window is not None:
            self.joins_window.result_ready()

    def _check_joins(self):
        """The join checker on the result shown: what it does to the
        connected script's joins (milliseconds: the engine works in parallel)."""
        self.join_check = None
        if (self.result is None or self.context is None or self.joins is None
                or not self.engine.features & kb.FEATURE_JOIN_CHECK):
            return
        try:
            self.join_check = self.engine.join_check(self.context, self.result)
        except Exception:
            print(traceback.format_exc())
            self.join_check = None

    def _result_status(self):
        res, snap = self.result, self.snapshot
        st = res.stats
        dropped = ""
        budget = self._result_key[KEY_BUDGET] if self._result_key else 0
        if st["dropped_by_budget"] and budget:
            dropped = " · %s dropped by the %s-pair budget" % (_count(st["dropped_by_budget"]), _count(budget))
        # kept joins and the designer harness add entries after the budget
        extra = res.entry_count - st["class_entries"] - st["exception_entries"]
        more = ", %s for kept joins and the harness" % _count(extra) if extra > 0 else ""
        if self._result_kind == "whole":
            return ("Whole font (%s): %s glyphs kerned · %s pairs in scope · %s class pairs solved · %s entries "
                    "(%s class pairs, %s exceptions%s)%s · %.1f s on %d threads"
                    % (snap.master_name, _count(st["kern_glyphs"]), _count(st["pairs_in_scope"]),
                       _count(st["class_pairs"]), _count(res.entry_count), _count(st["class_entries"]),
                       _count(st["exception_entries"]), more, dropped, self._job_elapsed, st["threads"]))
        prep = self.context.prep_ms if self.context is not None else 0.0
        joined = ""
        if self.joins is not None:
            joined = " · connected script (%s): %s" % (JOIN_MODES[self._join_mode()].lower(), self.join_note)
            if self.join_check is not None and self.join_check[0]["joins"]:
                js = self.join_check[0]
                joined += ", %s of %s joins kept" % (_count(js["kept"]), _count(js["joins"]))
        return ("%s · %s glyphs spaced, %s of the sample kerned · %s entries (%s class pairs, %s exceptions%s) · "
                "pass 1 %.0f ms · pass 2 %.0f ms · %d threads · Phase 1 took %.1f s%s"
                % (snap.master_name, _count(len(snap.names)), _count(st["kern_glyphs"]), _count(res.entry_count),
                   _count(st["class_entries"]), _count(st["exception_entries"]), more, st["pass1_ms"], st["pass2_ms"],
                   st["threads"], prep / 1000.0, joined))

    def start_whole_font(self, apply_when_done=False):
        """Kerns every glyph of the master (mask None, budget from Max pairs)
        with progress and Cancel. Returns False when it cannot start now."""
        if self.w is None or self.context is None or self.snapshot is None or self._stepper is not None:
            return False
        if self._job is not None:
            if self._job_kind == "whole":
                return False
            self._job.free()  # a running preview: the whole-font result covers it
            self._job = None
            self._job_kind = None
        self._preview_pending = False
        self._apply_after_preview = self._whole_after_preview = False
        budget = self._budget()
        try:
            plan = self._harness_plan()
            self._job = self.engine.solve(self.context, self._params(budget=budget), None,
                                          glyph_opts=self._glyph_opts(),
                                          harness=plan.engine_arg() if plan is not None else None)
            self._job_harness = plan
        except Exception as e:
            self._fail("The whole-font run could not start: %s" % e, traceback.format_exc())
            return False
        self._job_kind = "whole"
        self._job_key = self._solve_key(True)
        self._job_requested = None
        self._job_elapsed = 0.0
        self._apply_when_done = apply_when_done
        self._set_state("solving")
        self._show_progress("Starting…", 0.0)
        n = len(self.snapshot.names)
        self._set_status("Kerning the whole font: %s glyphs, up to %s pairs, %s…" % (
            _count(n), _count(n * n), "at most %s entries" % _count(budget) if budget else "no pair limit"))
        self._run_timer(POLL_INTERVAL)
        return True

    def cancel_job(self):
        """Stops whatever runs: reading, Phase 1, a preview or a whole-font run."""
        if self.w is None:
            return False
        if self._stepper is not None and self._stepper_kind != "plan":
            return False  # writing: Apply and Revert run to the end
        stopped = None
        if self._stepper is not None:
            self._stepper = self._stepper_kind = None  # nothing written yet
            stopped = "plan"
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
            stopped = "reading"
        if self._job is not None:
            self._job.free()  # cancels; never blocks
            stopped = self._job_kind
            self._job = None
            self._job_kind = None
        self._preview_pending = False
        self._apply_when_done = self._apply_after_preview = self._whole_after_preview = False
        self._stop_timer()
        if self._left_due or self._right_due:
            self._run_timer(POLL_INTERVAL)
        if stopped is None:
            return False
        self._idle_progress()
        if self.context is None:
            self._set_state("error")
            self._set_status("Stopped before the engine was ready — choose ↻ to read the outlines again.")
        else:
            self._set_state("ready")
            self._set_status({"whole": "Whole-font run cancelled.", "preview": "Preview cancelled.",
                              "plan": "Apply cancelled — nothing was written."}.get(stopped, "Cancelled."))
        return True

    def _release_engine(self):
        """Stops reading and the job, frees the result and the context (a
        plan being made goes too: it reads the result)."""
        if self._stepper_kind == "plan":
            self._stepper = self._stepper_kind = None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._job is not None:
            self._job.free()
            self._job = None
            self._job_kind = None
        self._preview_pending = False
        self._apply_when_done = self._apply_after_preview = self._whole_after_preview = False
        if self.result is not None:
            self._retire(self.result)
            self.result = None
        self._result_kind = self._result_key = self._requested = None
        if self.context is not None:
            self._retire(self.context)
            self.context = None

    # -------------------------------------------------------------- proofs
    def _metrics_in_font(self, name):
        """Left pane: the font's current metrics of a glyph Apply or Revert
        changed (read lazily, only for glyphs of the sample text), measured
        on the ink of the decomposed outline like the snapshot's (layer.LSB
        runs along the italic angle on an italic master)."""
        if name not in self._stale:
            return None
        m = self._font_metrics.get(name)
        if m is None:
            glyph = self.font.glyphs[name]
            layer = glyph.layers[self.snapshot.master_id] if glyph is not None else None
            if layer is None:
                return None
            width = float(layer.width)
            path = ks.layer_path(layer)
            if path is not None and path.elementCount():
                ink = path.bounds()
                x = float(ink.origin.x)
                m = (x, width - (x + float(ink.size.width)), width)
            else:
                m = (None, None, width)  # no ink: only the advance
            self._font_metrics[name] = m
        return m

    @property
    def panes_due(self):
        """True while a proof pane waits to be laid out again (tests)."""
        return self._left_due or self._right_due

    def _panes_due(self, left=False, right=False, delay=0.0):
        """Lays out the panes again on the coming ticks, one pane per tick
        (`delay`: not before that many seconds from now)."""
        self._left_due = self._left_due or left
        self._right_due = self._right_due or right
        if delay:
            self._render_at = max(self._render_at, time.time() + delay)
        if self._timer is None:
            self._run_timer(POLL_INTERVAL)

    def _render_left(self):
        self._left_due = False
        snap = self.snapshot
        if snap is None or self.w is None:
            return
        table = self._kerning.table(snap.master_id)
        kerning = self._kerning

        def kern(left, right):
            try:
                return kerning.value(table, left, right)
            except Exception:
                return 0.0

        self.left.render(self.tokens, snap, metrics=self._metrics_in_font, kern=kern, point_size=self._point_size())

    def _render_right(self):
        self._right_due = False
        snap = self.snapshot
        if snap is None or self.w is None:
            return
        res = self.result
        caption = "Kinetikern2 — live sidebearings + kerning"
        if res is None or res.ptr is None:
            self.right.render(self.tokens, snap, metrics=None, kern=None, point_size=self._point_size())
            self.right_group.caption.set(caption)
            return
        index = snap.index
        view = res.metrics
        value = res.value
        kerning = {}

        def metrics(name):
            i = index.get(name)
            if i is None:
                return None
            m = view[i]
            return (m.lsb, m.rsb, m.advance) if m.valid else None

        def kern(left, right):
            k = kerning.get((left, right))
            if k is None:
                a, b = index.get(left), index.get(right)
                k = value(a, b) if a is not None and b is not None else 0.0
                if k != k:  # NaN: a glyph this result did not kern
                    k = 0.0
                kerning[(left, right)] = k
            return k

        self.right.render(self.tokens, snap, metrics=metrics, kern=kern, point_size=self._point_size())
        if self._result_kind == "whole":
            caption += " (whole-font result: %s entries)" % _count(res.entry_count)
        self.right_group.caption.set(caption)

    def _font_changed(self, names):
        """Apply or Revert wrote to the master: the left pane re-reads the
        metrics of these glyphs (when it shows them) and the kerning keys."""
        self._stale.update(names)
        self._font_metrics = {}
        self._kerning.reset()
        self._font_written = True
        self._panes_due(left=True)

    # --------------------------------------------------------------- apply
    def _font_open(self):
        """False once the user closed the font (checked where the user acts)."""
        fonts = getattr(Glyphs, "fonts", None)
        if fonts is None:
            return True  # no list of open fonts to check against (tests)
        try:
            return any(f == self.font for f in fonts)
        except Exception:
            return True

    def _ask(self, message, info, buttons):
        """An alert (the only modal wait); the index of the button chosen."""
        alert = NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.setInformativeText_(info)
        for title in buttons:
            alert.addButtonWithTitle_(title)
        self._modal = True  # the timer keeps firing in the modal session; ticks wait
        try:
            answer = alert.runModal()
        finally:
            self._modal = False
        return int(answer) - int(NSAlertFirstButtonReturn)

    def _confirm(self, plan):
        """The confirmation alert. True for Apply."""
        return self._ask("Apply Kinetikern2 to “%s”?" % self.snapshot.master_name, ka.describe_plan(plan),
                         ("Apply", "Cancel")) == 0

    def plan_names(self, result):
        """Glyphs whose sidebearings an apply of `result` writes: every glyph
        the right pane shows for a sample preview (also those the engine
        spaced but did not kern) and every glyph the preview kerned (the
        text may have lost some since: their kerning is written, so is the
        spacing it was computed for); None (kk2_apply decides) for a
        whole-font result."""
        if result is not self.result or self._result_kind == "whole" or self.snapshot is None:
            return None
        snap = self.snapshot
        index = snap.index
        names = set(t for t in self.tokens if t is not None and t in index)
        names.update(snap.names[i] for i in (self._requested or ()))
        return names

    def _start_stepper(self, kind, stepper, text):
        self._stepper = stepper
        self._stepper_kind = kind
        self._set_state("applying")
        self._show_progress("%s [0%%]" % text, 0.0)
        self._run_timer(READ_INTERVAL)

    def _step_stepper(self):
        stepper, kind = self._stepper, self._stepper_kind
        try:
            done = stepper.step(READ_SLICE)
        except Exception:
            self._stepper = self._stepper_kind = None
            what = {"plan": "Apply could not be planned", "apply": "Applying failed",
                    "revert": "Reverting failed"}.get(kind, "Kinetikern2 stopped")
            self._fail("%s — see the Macro panel." % what, traceback.format_exc())
            return
        if stepper.waiting:
            self._revert_decision(stepper)
            return
        if not done:
            self._show_progress("%s [%d%%]" % (stepper.text, int(100 * stepper.fraction)), stepper.fraction)
            return
        self._stepper = self._stepper_kind = None
        self._run_timer(POLL_INTERVAL)
        if kind == "plan":
            self._plan_ready(stepper.plan)
        elif kind == "apply":
            self._apply_done(stepper)
        else:
            self._revert_done(stepper)

    def _settled(self):
        """The state once nothing writes: a preview waits for its turn."""
        self._idle_progress()
        if self._job is not None:
            self._set_state("previewing")
        elif self._preview_pending:
            self._set_state("previewing")
            self._run_timer(POLL_INTERVAL)
        else:
            self._set_state("ready" if self.context is not None else "error")

    def _apply(self, result, confirm):
        """plan (sliced) → alert → apply (sliced). Returns True if it started."""
        snap = self.snapshot
        if result is None or result.ptr is None or snap is None or self.w is None or self._stepper is not None:
            return False
        replace = bool(self.w.replace.get())
        try:
            opts = self._glyph_opts()
            frozen = self.groups.frozen_names() if opts else None
            planner = ka.Planner(snap, result, replace, metrics_names=self.plan_names(result), frozen=frozen)
        except Exception as e:
            self._fail("Apply could not be planned: %s" % e, traceback.format_exc())
            return False
        self._confirm_plan = confirm
        self._start_stepper("plan", planner, "Planning")
        self._set_status("Planning the Apply to %s…" % snap.master_name)
        return True

    def _plan_ready(self, plan):
        snap = self.snapshot
        if snap is None or self.w is None:
            return
        if self._confirm_plan and not self._confirm(plan):
            self._settled()
            self._set_status("Apply cancelled — nothing was written.")
            return
        try:
            applier = ka.Applier(self.font, snap, plan)
        except Exception as e:
            self._fail("Applying failed: %s" % e, traceback.format_exc())
            return
        self._applied_names = set(plan.metrics) | set(plan.groups_to_set) | set(plan.sync) | self._ruled
        self._start_stepper("apply", applier, "Applying")
        self._set_status("Applying to %s…" % snap.master_name)

    def _apply_done(self, applier):
        summary, revert = applier.summary, applier.revert
        # a failure halfway comes back in the summary, with the RevertPoint that undoes what was written
        if revert is not None:
            self.revert_point = revert
        self.last_apply = summary
        failed = bool(summary.get("error"))
        if not summary["ok"] and summary.get("examples"):
            print("Kinetikern2 apply read-back: %r" % (summary["examples"],))
        master = applier.plan.master_name
        if failed:
            self._fail(ka.describe_summary(summary))
        else:
            self._settled()
            self._set_status("Applied to %s: %s" % (master, ka.describe_summary(summary)))
        if revert is not None:
            self._font_changed(self._applied_names)

    def apply_whole_font_result(self, confirm=False):
        """Applies the finished whole-font result (no alert unless `confirm`).
        Returns True when the Apply started (window.last_apply is its
        summary once window.state has left "applying")."""
        if self.result is None or self._result_kind != "whole":
            raise RuntimeError("no whole-font result to apply (run start_whole_font() first)")
        return self._apply(self.result, confirm)

    def revert_last_apply(self):
        """Puts back what the last Apply changed (sliced; asks first when
        something was changed since). Returns True when it started."""
        point = self.revert_point
        if point is None or self.w is None or self._stepper is not None or self._reader is not None:
            return False
        self._start_stepper("revert", point.restorer(), "Reverting")
        self._set_status("Reverting the last apply…")
        return True

    def _revert_decision(self, restorer):
        """Some of what the Apply wrote was changed since: keep those changes,
        put everything back, or leave it all as it is."""
        answer = self._ask("Revert the last Apply?", ka.describe_conflicts(restorer),
                           ("Keep Later Changes", "Revert Everything", "Cancel"))
        if answer in (0, 1):
            restorer.resolve(overwrite=answer == 1)
            return
        self._stepper = self._stepper_kind = None
        self._settled()
        self._set_status("Revert cancelled — nothing was changed.")

    def _revert_done(self, restorer):
        counts = restorer.counts
        self.last_revert = counts
        self.revert_point = None
        self._settled()
        if self.snapshot is not None:
            self._font_changed(self._applied_names | set(restorer.point.metrics))
        name = self.snapshot.master_name if self.snapshot is not None else "the master"
        kept = counts.get("kept_kerning", 0) + counts.get("kept_glyphs", 0) + counts.get("kept_components", 0)
        if kept:
            self._set_status("Reverted the last apply on %s; %s values changed since were kept." % (name, _count(kept)))
        else:
            self._set_status("Reverted the last apply: the kerning, groups and sidebearings of %s are back as they "
                             "were." % name)

    # ----------------------------------------------------------- callbacks
    def physicsChanged(self, sender):
        self._save("tightness", float(self.w.tightness.get()))
        self._save("intensity", float(self.w.intensity.get()))
        self._update_labels()
        self._request_preview()
        if self.harness_window is not None:
            self.harness_window.settings_changed()  # its corrections follow the Looseness

    # ------------------------------------------------------- designer harness
    def harness_available(self):
        return bool(self.engine.features & kb.FEATURE_HARNESS) and os.path.exists(kh.TABLE_PATH)

    def _harness_strength(self):
        """0 … 1: how much of the harness the solves use (0 = off)."""
        if self.w is None or not self.w.harness.get() or not self.harness_available():
            return 0.0
        return max(0.0, min(100.0, float(self.w.harnessStrength.get()))) / 100.0

    def _harness_plan(self, force_strength=None):
        """The harness for the master read, the Looseness and the strength
        (`force_strength`: that strength whether it is on or off), or None."""
        s = self._harness_strength() if force_strength is None else float(force_strength)
        snap = self.snapshot
        if s <= 0 or snap is None or self.w is None:
            return None
        looseness = float(self.w.tightness.get()) + (self._fitted or 0.0)
        opts = self._glyph_opts()
        frozen = frozenset(i for i, o in enumerate(opts or ()) if o[0])
        kept = self._kept_sides()
        key = (id(snap), self._generation, round(looseness, 4), round(s, 4), self.groups.key(), self.harness_style,
               id(self.context) if kept else None)
        if self._harness_cache is not None and self._harness_cache[0] == key:
            return self._harness_cache[1]
        try:
            plan = kh.Plan(snap, looseness, s, frozen=frozen, style=self.harness_style, kept=kept)
        except Exception:
            print(traceback.format_exc())
            plan = None
        self._harness_cache = (key, plan)
        return plan

    def model_pair(self, a, b):
        """Kinetikern2's spacing of glyph pair (a, b) without the harness, from
        the result shown: ((lsb, rsb, advance) of a, the same of b, kerning),
        font units; None if the result does not kern the pair."""
        res = self.result
        if res is None or res.ptr is None:
            return None
        v = res.value(a, b)
        if v != v:
            return None
        h = self._result_harness
        sa = h.sides[a] if h is not None else (0.0, 0.0)
        sb = h.sides[b] if h is not None else (0.0, 0.0)
        ma, mb = res.metrics[a], res.metrics[b]
        k = float(v) - (h.pair_value.get((a, b), 0.0) if h is not None else 0.0)
        return ((ma.lsb - sa[0], ma.rsb - sa[1], ma.advance - sa[0] - sa[1]),
                (mb.lsb - sb[0], mb.rsb - sb[1], mb.advance - sb[0] - sb[1]), k)

    def harnessChanged(self, sender):
        self._save("harness", bool(self.w.harness.get()))
        self._save("harnessStrength", float(self.w.harnessStrength.get()))
        self._update_labels()
        self._request_preview()
        if self.harness_window is not None:
            self.harness_window.settings_changed()

    def set_harness_style(self, style):
        """Follow the conventions of `style` ("text", "display",
        "handwriting"): the punctuation of that category."""
        if style == self.harness_style or style not in [k for k, _label in kh.available_styles()]:
            return
        self.harness_style = style
        self._save("harnessStyle", style)
        self._update_labels()
        self._request_preview()
        if self.harness_window is not None:
            self.harness_window.settings_changed()

    def openHarness(self, sender=None):
        if self.harness_window is not None:
            self.harness_window.w.getNSWindow().makeKeyAndOrderFront_(None)
            return self.harness_window
        import kk2_harness_window
        self.harness_window = kk2_harness_window.HarnessWindow(self)
        self.harness_window.refresh()  # its pairs join the preview from here on
        return self.harness_window

    def harness_window_closed(self):
        self.harness_window = None

    def thresholdChanged(self, sender):
        t = self._threshold()
        self.w.thresholdField.set("%g" % round(t, 1))
        self._save("threshold", t)
        self._update_labels()
        self._request_preview()

    def thresholdFieldChanged(self, sender):
        text = (self.w.thresholdField.get() or "").strip().replace(",", ".")
        try:
            t = float(text)
        except ValueError:
            return  # still typing
        if t != t:
            return
        t = min(MAX_THRESHOLD, max(0.0, t))
        if abs(t - self._threshold()) < 1.0e-9:
            return
        self.w.threshold.set(t)
        self._save("threshold", t)
        self._update_labels()
        self._request_preview()

    def maxPairsChanged(self, sender):
        self._save("maxPairs", self._budget())
        self._update_max_pairs_note()

    def threadsChanged(self, sender):
        self._save("threads", self._threads())  # used from the next job on

    def scopeChanged(self, sender):
        self._save("scope", int(self.w.scope.get()))

    def replaceChanged(self, sender):
        self._save("replace", bool(self.w.replace.get()))

    def sizeChanged(self, sender):
        self._save("size", SIZES[self.w.size.get()])
        self._panes_due(left=True, right=True)

    def textChanged(self, sender):
        self._save("sample", self.editor.get())
        if self.snapshot is None:
            return  # rendered once the outlines are read
        self.tokens = ks.tokenize(self.editor.get(), self.snapshot)
        # laid out once typing pauses: a long sample takes a while to lay out
        self._panes_due(left=True, right=True, delay=TYPING_PAUSE)
        # which glyphs the result on its way (or the one shown) kerns
        if self._job is not None and self._job_kind == "preview":
            known = self._job_requested
        elif self._job is not None and self._job_kind == "whole":
            return  # its result kerns every glyph
        elif self.result is not None and self._result_kind == "whole":
            return
        elif self.result is not None and self._requested is not None:
            known = self._requested
        else:
            known = frozenset()
        if not self._sample_indices() <= known:
            self._request_preview()  # new glyphs need their pairs

    def masterChanged(self, sender):
        self._load_master(self._master())

    def reloadOutlines(self, sender):
        self._load_master(self._master())

    def applyToFont(self, sender):
        if self.snapshot is None or self.context is None or self._stepper is not None:
            return
        if not self._font_open():
            self._set_status("The font was closed; nothing was applied.")
            return
        whole = self.w.scope.get() == SCOPE_WHOLE
        if self._font_written:
            # the last Apply or Revert changed groups and sidebearings the
            # snapshot (and the engine's classes) still have as they were
            name = self.snapshot.master_name
            self._load_master(self._master())
            if whole:
                self._whole_after_preview = True
            else:
                self._apply_after_preview = True
            self._set_status("Reading %s again (the last Apply or Revert changed it), then applying…" % name)
            return
        if whole:
            if self._result_kind == "whole" and self._job is None and self._result_key == self._solve_key(True):
                self._apply(self.result, confirm=True)  # the finished run still matches the settings
            else:
                self.start_whole_font(apply_when_done=True)
            return
        if (self._result_kind == "preview" and self._job is None and not self._preview_pending and
                self._result_key == self._solve_key(False)):
            self._apply(self.result, confirm=True)
            return
        self._apply_after_preview = True  # apply once the preview for these settings lands
        if self._job is None:
            self._preview_pending = True
            self._start_preview()
        self._set_status("Applying once the preview is ready…")

    def revertLastApply(self, sender):
        if not self._font_open():
            self._set_status("The font was closed; nothing to revert.")
            return
        self.revert_last_apply()

    def cancelJob(self, sender):
        self.cancel_job()

    # ------------------------------------------------------ spacing groups
    def openGroups(self, sender=None):
        if self.groups_window is not None:
            self.groups_window.w.getNSWindow().makeKeyAndOrderFront_(None)
            return self.groups_window
        import kk2_groups_window
        self.groups_window = kk2_groups_window.GroupsWindow(self)
        return self.groups_window

    def openJoins(self, sender=None):
        import kk2_joins_window as kj
        if self.joins_window is not None:
            self.joins_window.front()
            return
        self.joins_window = kj.JoinsWindow(self, host=kj.GlyphsHost(self.font))

    def joins_window_closed(self):
        self.joins_window = None

    def openPairs(self, sender=None):
        if self.pairs_window is not None:
            self.pairs_window.w.getNSWindow().makeKeyAndOrderFront_(None)
            return self.pairs_window
        import kk2_pairs_window
        self.pairs_window = kk2_pairs_window.PairsWindow(self)
        return self.pairs_window

    def groups_window_closed(self):
        self.groups_window = None

    def pairs_window_closed(self):
        self.pairs_window = None

    def set_groups(self, groups, preview=True):
        """Replaces the spacing groups (a script, a test, a font's own set):
        the Groups window shows them, the preview follows."""
        self.groups = groups
        if self.groups_window is not None:
            self.groups_window.groups_replaced()
        self.groups_changed(preview)

    def groups_changed(self, preview=True):
        """The Spacing Groups window changed the groups: save, preview again."""
        if not self._unattended and self.font is not None:
            self.groups.save(self.font)
        if preview:
            self._request_preview()

    def groups_note(self):
        """A line for the groups window: the fitted Looseness, when there is one."""
        if self._fitted is not None:
            frozen = bool(self.groups is not None and self.groups.frozen_names())
            what = ("the frozen glyphs and the kept joins" if frozen and self._keeps_joins() else
                    "the kept joins" if self._keeps_joins() else "the frozen glyphs")
            return ("\n%s %s spaced at Looseness %+.2f of Kinetikern2's: new glyphs follow them "
                    "(plus the main slider)." % (what[0].upper() + what[1:], "is" if what == "the kept joins" else "are",
                                                 self._fitted))
        return ""

    def windowClosed(self, sender):
        for sub in (self.groups_window, self.pairs_window, self.harness_window, self.joins_window):
            if sub is not None:
                sub.close()
        self.groups_window = self.pairs_window = self.harness_window = self.joins_window = None
        self._stop_timer()
        if self._timer_target is not None:
            self._timer_target.kk2_callback = None
            self._timer_target = None
        stepper, kind = self._stepper, self._stepper_kind
        self._stepper = self._stepper_kind = None
        if kind in ("apply", "revert"):
            # writing: finish (a second or two at most) rather than leave the
            # master half applied or half reverted
            try:
                while not stepper.step(1.0) and not stepper.waiting:
                    pass
                if kind == "apply" and stepper.revert is not None:
                    self.revert_point = stepper.revert
                elif kind == "revert" and stepper.done:
                    self.revert_point = None
            except Exception:
                print(traceback.format_exc())
        if self.revert_point is not None and self.font is not None:
            _keep_revert_point(self.font, self.revert_point)  # Revert stays possible in the next window
        self.revert_point = None
        self._release_engine()
        self.sync.detach()
        # nothing of the font stays reachable from a closed window
        for pane in (self.left, self.right):
            pane.clear()
            pane._forget()
        self.snapshot = None
        self.tokens = []
        self._font_metrics = {}
        self._stale = set()
        self._kerning = None
        self.font = None
        self.w = None
        if self in _open_windows:
            _open_windows.remove(self)


_open_windows = []

# Revert points of closed windows. Apply writes without undo, so a window
# closed after an Apply hands its RevertPoint on to the next window opened
# for the same font. Kept with a weak reference to the font only (a closed
# document must be able to go away); a window that adopts the point gives
# it its font back.
_kept_revert_points = []  # [(weak reference to the font, RevertPoint)]


def _weak(obj):
    """A weak reference to a GSFont (or to a plain Python object), or None."""
    try:
        return objc.WeakRef(obj)
    except Exception:
        pass
    try:
        return weakref.ref(obj)
    except TypeError:
        return None


def _keep_revert_point(font, point):
    ref = _weak(font)
    if ref is None:
        return
    point.font = None
    _kept_revert_points.append((ref, point))


def _adopt_revert_point(font):
    """The kept RevertPoint of `font`, if any (taken out of the list); the
    points of fonts that are gone are dropped."""
    found = None
    for entry in list(_kept_revert_points):
        ref, point = entry
        kept = ref()
        if kept is None:
            _kept_revert_points.remove(entry)
        elif found is None and font is not None and kept == font:
            _kept_revert_points.remove(entry)
            point.font = font
            found = point
    return found


def open_window(resources):
    """The window for the current font: brought to the front when it is
    already open, otherwise a new one. Open windows are kept here, so a window
    stays alive however often the menu item is chosen."""
    font = Glyphs.font
    for win in list(_open_windows):
        if getattr(win, "w", None) is None:
            _open_windows.remove(win)  # closed
        elif not win._font_open():
            win.w.close()  # its font was closed
        elif font is not None and win.font == font:
            win.w.getNSWindow().makeKeyAndOrderFront_(None)
            return win
    win = KK2Window(resources)
    if getattr(win, "w", None) is not None:
        _open_windows.append(win)
    return win
