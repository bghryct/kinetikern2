# encoding: utf-8
"""
kk3_window — the Kinetikern3 window (vanilla + PyObjC).

Layout: physics and kerning controls on top, a progress row with Cancel under
them, a row for the view and the text size, then a vertical split with the
proofing panes (left: the font as it is, with its own spacing and kerning;
right: the live engine result) over the editable multi-paragraph sample text.
Editing or scrolling the sample text updates and scrolls the panes. The View
shows the two side by side, or in one pane: Toggle switches between them (a
click on the preview or the space bar), Overlay lays the font over
Kinetikern3 with the same line breaks (kk3_proof).

Nothing here waits. One NSTimer in the common run-loop modes (so it keeps
firing during slider drags, open menus and the confirmation alert, where the
window ignores it) drives everything:

* reading — kk3_snapshot.SnapshotReader copies the master out of Glyphs in
  slices of at most 8 ms per tick;
* engine jobs — Phase 1 (prepare), the sample-text preview and whole-font
  solves run on the library's own threads; the timer polls them at ~15 Hz
  (a few atomic loads) and takes the output once a job is done;
* Apply and Revert — kk3_apply's Planner, Applier and Restorer, in slices
  of at most 8 ms per tick, with progress;
* the proof panes — each is laid out on a tick of its own, a pause after the
  last keystroke in the sample text.

There is at most one engine job at a time. A change of the physics, the
threshold or the glyphs of the sample text replaces the running preview (the
newest request wins: a slow preview is cancelled at once, a fast one may land
first so that a slider drag keeps showing results). A whole-font run disables
the controls that would restart it and can be cancelled. Apply plans, asks in
an NSAlert (the only modal wait), writes, and keeps a kk3_apply.RevertPoint for
Revert Last Apply; the controls are disabled meanwhile. After an Apply or a
Revert the snapshot no longer matches the font (groups, sidebearings): the
next Apply reads the master again first.

Only the pairs of the sample text are ever looked at here: the right pane asks
the result for the values of its adjacent glyph pairs, the left pane looks the
same pairs up in the master's kerning table.
"""

from __future__ import division, print_function, unicode_literals

import json
import math
import os
import time
import traceback
import weakref

import objc
import vanilla
import vanilla.dialogs
from AppKit import NSAlert, NSAlertFirstButtonReturn, NSColor, NSFont, NSObject, NSPasteboard, NSPasteboardTypeString
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer

import kk3_apply as ka
import kk3_args
import kk3_bridge as kb
import kk3_groups as kg
import kk3_harness as kh
import kk3_punctuation as kpu
import kk3_proof as kp
import kk3_snapshot as ks

try:
    from GlyphsApp import Glyphs, Message
except ImportError:  # outside Glyphs (tests)
    Glyphs = None
    Message = None

PREFIX = "com.mirkovelimirovic.Kinetikern3."
LEGACY_PREFIX = "com.mirkovelimirovic.Kinetikern2."  # read while Kinetikern3 has no setting of its own
# preferences the window remembers between sessions (keys under PREFIX)
PERSISTED = ("tightness", "intensity", "kerning", "threshold", "maxPairs", "threads", "scope", "size", "view",
             "overlayStyle", "alignWords", "keepFit", "stayClose", "categoryOffsets", "sample", "replace")
SAMPLE_TEXT = (
    "Hamburgefonstiv HOHOHOHO nonononono\n"
    "AVATAR TYPE WAVE LT Tolerance Yo Ta Te Vo P. F, L’\n"
    "The quick brown fox jumps over the lazy dog. Typography is the craft of endowing "
    "human language with a durable visual form.\n"
    "/T/o/T/a/T/e /V/A/V/o /P/A /r/period /y/period /f/parenright"
)
# the proof text size, points (a logarithmic slider, a field, a pinch on the preview)
MIN_SIZE, MAX_SIZE, DEFAULT_SIZE = 8.0, 400.0, 48.0
# how the panes show the font as it is and Kinetikern3
VIEWS = ["Side by side", "Toggle", "Overlay"]
VIEW_SIDE, VIEW_TOGGLE, VIEW_OVERLAY = 0, 1, 2
OVERLAY_STYLES = ["Font in red", "Both translucent"]
OVERLAY_RED, OVERLAY_TRANSLUCENT = 0, 1
# Copy, Paste, Save and Load Settings: a JSON object that names itself, its choices as words
SETTINGS_FORMAT, SETTINGS_VERSION = "kinetikern3", 1
SCOPE_NAMES, VIEW_NAMES, OVERLAY_NAMES, JOIN_NAMES = ["sample", "whole"], ["side", "toggle", "overlay"], \
    ["red", "translucent"], ["keep", "space"]
SCOPES = ["Glyphs in sample text", "Whole font"]
SCOPE_SAMPLE, SCOPE_WHOLE = 0, 1
LOOSENESS_RATIO = 3.86
# the Looseness slider (0 = the model's own spacing). The designers of hairline and thin
# fonts space them at about -0.4 to +0.2; -1 already sets some of them with almost no
# sidebearing, others need -2 or -3 for a tight display setting, where the sidebearings of
# a hairline go below zero (Archer Hairline's n: +50 at 0, +5 at -1, -10 at -2)
TIGHT_MIN, LOOSE_MAX = -3.0, 2.0
MAX_INTENSITY = 200.0
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
    class KK3WindowTimer(NSObject):
        """Target of the window's NSTimer: hands each tick to Python."""

        def tick_(self, timer):
            callback = getattr(self, "kk3_callback", None)
            if callback is None:
                return
            try:
                callback()
            except Exception:  # never let an exception unwind into the run loop
                print(traceback.format_exc())

    return KK3WindowTimer


KK3WindowTimer = _class("KK3WindowTimer", _make_timer_class)


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
arguments = kk3_args.arguments
unattended = kk3_args.unattended


def _as_bool(value):
    if isinstance(value, (bytes, str)):
        return value.strip().lower() in ("1", "yes", "true", "y")
    return bool(value)


def _count(n):
    return "{:,}".format(int(n))


def _number(text):
    """A number typed into a field (a comma or a minus sign as typed; a % sign is ignored), else None."""
    text = (text or "").strip().replace(",", ".").replace("\u2212", "-").replace("%", "").strip()
    try:
        v = float(text)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


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


class KK3Window(object):

    def __init__(self, resources):
        # read by kk3_selftest and tools/kk3_ui_smoke_test.py
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
        self.tick_ms = {}  # longest timer tick per kind of work, in ms (diagnostics for kk3_selftest)
        self.last_tick = None  # (kind of work, ms) of the latest tick
        self.last_revert = None  # counts of the last Revert Last Apply
        if self.font is None:
            Message("Open a font first.", title="Kinetikern3")
            return
        try:
            self.engine = load_engine(resources)
        except Exception as e:
            Message("The Kinetikern3 engine could not be loaded:\n%s\n\n%s"
                    % (os.path.join(resources, kb.DYLIB_NAME), e), title="Kinetikern3")
            return
        self._unattended = kk3_args.unattended()
        self._arguments = kk3_args.arguments() if self._unattended else None
        self.tokens = []
        self._generation = 0  # counts snapshots: results of an older one are never reused
        self._reader = None
        self._read_done = False  # the reader finished; its snapshot is taken on the next tick
        self._join_finder = None  # finding a connected script's joins, a step per tick (_join_steps)
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
        self._stepper = None  # kk3_apply Planner | Applier | Restorer at work
        self._stepper_kind = None  # "plan" | "apply" | "revert"
        self._confirm_plan = False  # ask before applying the plan being made
        self._font_written = False  # Apply or Revert changed the master since it was read
        self._left_due = self._right_due = False  # panes waiting for a tick of their own
        self._overlay_due = False  # the font laid over Kinetikern3 (Toggle, Overlay), after the right pane
        self._left_stale = False  # the left pane, hidden (Toggle, Overlay), missed a layout
        self._showing_font = False  # Toggle: the font as it is is shown (else Kinetikern3)
        self._test_pasteboard = None  # Copy and Paste Settings in a test instance (never the user's clipboard)
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
        # the conventions it follows (the Conventions menu: punctuation, the
        # harness, Pass 2): Automatic (_style), text faces, display or handwriting
        self.harness_style = self._setting("harnessStyle", kh.AUTO, str)
        if self.harness_style not in [k for k, _label in kh.style_choices()]:
            self.harness_style = kh.AUTO
        self._harness_cache = None
        self._job_harness = None
        self._result_harness = None
        # tightness by category and keeping the font's fit (the Categories window)
        self.category_offsets = self._offsets_setting()
        self.keep_fit = self._setting("keepFit", False, bool)
        self.stay_close = min(100.0, max(0.0, self._setting("stayClose", 0.0)))
        self.categories_window = None
        self._categories = None  # (snapshot, the category of each of its glyphs)
        self._category_fits = {}  # the last result's fitted Looseness per engine fit group
        # the Looseness the last solve fitted to the frozen glyphs or kept joins;
        # it ran at that plus the slider's (FEATURE_FIT_OFFSET)
        self._fitted = None
        # a connected script: the joins the context was prepared with (None: none)
        self.joins = None
        self.join_count = 0
        self.join_note = ""
        self.join_ms = 0.0
        self.join_kinds = None  # JOINKIND_* per glyph, and the master's kerning, for the join checker
        self.join_decorated = False  # the glyphs touch by construction (kk3_bridge.Engine.join_decorated)
        self.join_current = ()
        self.join_check = None  # (stats, sides) of the result shown (kk3_bridge.Engine.join_check)
        self.joins_window = None
        # a master that leans without declaring an italic angle (by master id):
        # the slant its stems show (tan; 0 upright), the probe's verdict on
        # measuring along it ((lean, numbers): kk3_snapshot.SlantProbe), the
        # user's own choice, and the probe at work
        self._stem_slants = {}
        self._lean_verdicts = {}
        self._lean_choices = {}
        self._slant_probe = None
        self._along_declared = None  # the Along the italic angle setting (kept here: tests write no defaults)
        self._timer = None
        self._timer_interval = None
        self._timer_target = None
        self._modal = False
        self.sync = kp.KK3PaneSync.alloc().init()
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
                value = Glyphs.defaults[LEGACY_PREFIX + key]
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
        w = vanilla.Window((1240, 860), "Kinetikern3 — %s" % self.font.familyName, minSize=(1200, 600),
                           autosaveName=None if self._unattended else PREFIX + "window")
        self.w = w

        # row 1: physics and threshold; each slider has a field for an exact value (Return applies it)
        x = 14
        w.tightLabel = vanilla.TextBox((x, 10, 220, 17), "Looseness / Tightness", sizeStyle="small")
        w.tightMin = vanilla.TextBox((x, 32, 30, 14), "Tight", sizeStyle="mini")
        w.tightness = vanilla.Slider((x + 30, 28, 200, 22), minValue=TIGHT_MIN, maxValue=LOOSE_MAX,
                                     value=min(LOOSE_MAX, max(TIGHT_MIN, self._setting("tightness", 0.0))),
                                     callback=self.physicsChanged)
        w.tightMax = vanilla.TextBox((x + 234, 32, 32, 14), "Loose", sizeStyle="mini")
        w.tightField = vanilla.EditText((x + 268, 28, 52, 21), "", callback=self.tightFieldChanged,
                                        continuous=False, sizeStyle="small")
        w.tightField.getNSTextField().setToolTip_(
            "The Looseness, from %g (tight) to %+g (loose); 0 is the model's own spacing." % (TIGHT_MIN, LOOSE_MAX))
        w.tightValue = vanilla.TextBox((x, 52, 330, 14), "", sizeStyle="mini")

        x = 352
        # the label is the kerning switch: off previews and applies Kinetikern3's spacing alone
        w.kerning = vanilla.CheckBox((x, 9, 220, 18), "SDF Kerning Intensity", sizeStyle="small",
                                     value=self._setting("kerning", True, bool), callback=self.kerningChanged)
        w.kerning.getNSButton().setToolTip_(
            "Kerning on or off. Off: the preview shows Kinetikern3's spacing without its kerning, and Apply writes "
            "the spacing alone (as at 0 %; with Replace existing kerning on, the kerning between the glyphs it "
            "spaces is removed). The intensity keeps its value for when kerning is on again.")
        w.intensity = vanilla.Slider((x, 28, 170, 22), minValue=0.0, maxValue=MAX_INTENSITY,
                                     value=min(MAX_INTENSITY, max(0.0, self._setting("intensity", 100.0))),
                                     callback=self.physicsChanged)
        w.intensityField = vanilla.EditText((x + 176, 28, 44, 21), "", callback=self.intensityFieldChanged,
                                            continuous=False, sizeStyle="small")
        w.intensityUnit = vanilla.TextBox((x + 223, 32, 16, 14), "%", sizeStyle="mini")
        w.sdfValue = vanilla.TextBox((x, 52, 236, 14), "", sizeStyle="mini")

        x = 600
        threshold = min(MAX_THRESHOLD, max(0.0, self._setting("threshold", DEFAULT_THRESHOLD)))
        w.thresholdLabel = vanilla.TextBox((x, 10, 236, 17), "Ignore threshold (units per 1000 em)",
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
        # the switches: right-aligned on the top row, so that the four fit the narrowest window (1200 points)
        w.harness = vanilla.CheckBox((-358, 9, 113, 18), "Designer harness", sizeStyle="small",
                                     value=self._setting("harness", False, bool), callback=self.harnessChanged)
        w.punctuation = vanilla.CheckBox((-242, 9, 84, 18), "Punctuation", sizeStyle="small",
                                         value=self._setting("punctuation", True, bool),
                                         callback=self.punctuationChanged)
        w.punctuation.getNSButton().setToolTip_(
            "Punctuation by convention (on by default): spaces each punctuation mark the way the designers of "
            "well-spaced fonts do — its sides from the model's own reading of the mark and the font's letter "
            "rhythm — and kerns each mark as designers do: a period or comma tucked well under T, V or Y, "
            "a hyphen, dash or quote hardly at all, two marks side by side hardly at all. Learned from 1,594 "
            "Google Fonts families for text, display and handwriting faces (Conventions). Off: punctuation is "
            "spaced and kerned like the letters, from its shape alone, and the designer harness (learned on top "
            "of these conventions) leaves it alone too. It covers every mark Latin text uses — guillemets, low "
            "quotes, ¡ ¿, § ¶ † ‡ … — and the symbols that sit with figures ($ € £ # ‰ + × ÷ < >), whose sides "
            "are learned against the figures; a mark's case form takes its base mark's sides. In a master with "
            "numerators and denominators the fraction slash takes the conventions of a slash in a fraction: "
            "designers tuck the figures under it by well over 100 units a side.")
        w.figures = vanilla.CheckBox((-155, 9, 60, 18), "Figures", sizeStyle="small",
                                     value=self._setting("figures", True, bool), callback=self.figuresChanged)
        w.smallCaps = vanilla.CheckBox((-92, 9, 78, 18), "Small caps", sizeStyle="small",
                                       value=self._setting("smallCaps", True, bool), callback=self.smallCapsChanged)
        w.smallCaps.getNSButton().setToolTip_(
            "Small caps by convention (on by default): small caps (a.sc, a.smcp, A.c2sc, accented ones by their "
            "letter) spaced the way the designers of well-spaced fonts space them, measured beside the capitals "
            "they follow (Caps and Small Caps). The model is already about as close to them as to the "
            "lowercase; their own conventions bring most small caps about half a unit closer, a and x about 2. "
            "Off: small caps are spaced from their shapes alone.")
        w.figures.getNSButton().setToolTip_(
            "Figures by convention (on by default): proportional figures (the default ones, or .lf / .pnum "
            "forms when the default figures are tabular) spaced the way the designers of well-spaced fonts "
            "space them. From their shapes alone the model sets every figure 6–26 units per side tighter than "
            "designers do (the 1 most, then 2, 3, 5 and 7). Learned from the Google Fonts families with "
            "proportional figures. Tabular figures (.tf, .tosf too) keep their widths either way. The switch "
            "also covers what goes with figures: old-style figures (.osf, .onum), "
            "fractions (½ ¼ ¾ ⅓ ⅛…, and the numerators and denominators of 1⁄2), superscripts and subscripts "
            "(¹ ² ³, ₁ ₂, and .sups / .sinf "
            "forms), which designers space more openly than the model, and the ordinals ª º, which they set "
            "closer to the figure before them. A set of old-style figures, numerators, denominators, "
            "superscripts or subscripts that shares one width keeps it.")
        self.styles = kh.style_choices()
        w.conventions = vanilla.PopUpButton((x + 100, 28, 106, 20), [label for _k, label in self.styles],
                                            callback=self.conventionsChanged, sizeStyle="mini")
        keys = [k for k, _label in self.styles]
        w.conventions.set(keys.index(self.harness_style) if self.harness_style in keys else 0)
        w.conventions.getNSPopUpButton().setToolTip_(
            "Whose conventions the spacing follows: text faces, display faces or handwriting. Display and "
            "handwriting faces space their punctuation more openly, and kern a capital deeper over the next "
            "letter (Ha, To, Wa): a display face's capitals reach further than a text face's, handwriting's "
            "further still. Automatic: handwriting for a connected script or a hand whose stems lean without "
            "an italic angle, else text faces. The punctuation switch and the harness follow it too.")
        w.harness.getNSButton().setToolTip_(
            "Nudges the result toward what the designers of well-spaced fonts do where Kinetikern3 consistently "
            "differs: more space in parentheses and around / ? ! &, less around quotes, period, comma and "
            "hyphen; more on the open sides of E, F, L and T (most at light weights and tight settings), less on "
            "the diagonals of A, V, W, Y. Learned from the text fonts on Google Fonts.")
        w.harnessStrength = vanilla.Slider((x, 28, 58, 22), minValue=0.0, maxValue=100.0,
                                           value=min(100.0, max(0.0, self._setting("harnessStrength", 100.0))),
                                           callback=self.harnessChanged)
        w.harnessStrength.getNSSlider().setToolTip_("How much of the harness to apply (100 % = what the data say).")
        w.harnessStrengthField = vanilla.EditText((x + 62, 28, 34, 21), "", callback=self.harnessStrengthFieldChanged,
                                                  continuous=False, sizeStyle="small")
        w.harnessStrengthField.getNSTextField().setToolTip_("How much of the harness to apply, in percent.")
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
        w.threadsLabel = vanilla.TextBox((246, 69, 52, 17), "Threads", sizeStyle="small")
        cores = self.engine.cpu_count
        w.threads = vanilla.PopUpButton(
            (298, 66, 160, 20), ["Auto (%d of %d cores)" % (self.engine.default_threads, cores)] +
            [str(n) for n in range(1, cores + 1)], callback=self.threadsChanged, sizeStyle="small")
        threads = int(self._setting("threads", 0, int))
        w.threads.set(threads if 0 <= threads <= cores else 0)
        w.maxPairsLabel = vanilla.TextBox((474, 69, 64, 17), "Max pairs", sizeStyle="small")
        max_pairs = self._setting("maxPairs", DEFAULT_MAX_PAIRS, int)
        w.maxPairs = vanilla.EditText((540, 66, 72, 21), str(max_pairs) if max_pairs > 0 else "",
                                      callback=self.maxPairsChanged, placeholder="unlimited", sizeStyle="small")
        w.maxPairs.getNSTextField().setToolTip_(
            "Most kerning entries a whole-font Apply writes (the least important are left out). Empty or 0: "
            "no limit.")
        w.maxPairsNote = vanilla.TextBox((620, 70, 112, 14), "", sizeStyle="mini")
        w.categoriesButton = vanilla.Button((738, 64, 106, 22), "Categories…", callback=self.openCategories,
                                            sizeStyle="small")
        w.connected = vanilla.CheckBox((850, 67, 128, 18), "Connected script", sizeStyle="small",
                                       value=self._setting("connected", True, bool), callback=self.connectedChanged)
        w.connected.getNSButton().setToolTip_(
            "For scripts whose letters join. The joins are learned from the font's own spacing and kerning: when "
            "most lowercase letters overlap most of their partners, the font is a connected script. A font whose "
            "letters do not join is spaced as usual, so this can stay on.")
        mode = int(self._setting("joinMode", JOIN_KEEP, int))
        w.joinMode = vanilla.PopUpButton((980, 66, 124, 20), JOIN_MODES, callback=self.joinModeChanged,
                                         sizeStyle="small")
        w.joinMode.set(mode if mode in (JOIN_KEEP, JOIN_SPACE) else JOIN_KEEP)
        w.joinMode.getNSPopUpButton().setToolTip_(
            "Keep joins (the default): every letter side that joins keeps its sidebearing and every pair of two "
            "joining sides the font's kerning, so every join stays as drawn; Kinetikern3 spaces and kerns the "
            "rest (punctuation, figures, capitals that do not join, a letter next to a period) at the tightness "
            "of the kept letters. What it would have done to the joining sides is shown as drawing advice "
            "(Joins…).\n\nSpace joined letters: the letter bodies are spaced without their join strokes and two "
            "joining letters overlap with no kerning; joins whose strokes stop meeting break (Joins… counts them).")
        # the note lives in Joins…: the label keeps its text, hidden. (Not
        # (0, 0, 0, 0): a width or height of 0 stretches a vanilla view to the
        # window's edge, and the label then covered every control above.)
        w.connectedValue = vanilla.TextBox((14, 0, 10, 10), "", sizeStyle="mini")
        w.connectedValue.show(False)
        w.settingsMenu = vanilla.ActionButton((-92, 64, 38, 22), [
            dict(title="Copy Settings", callback=self.copySettings),
            dict(title="Paste Settings", callback=self.pasteSettings),
            "----",
            dict(title="Save Settings…", callback=self.saveSettings),
            dict(title="Load Settings…", callback=self.loadSettings),
            "----",
            dict(title="Reset Settings", callback=self.resetSettings)], sizeStyle="small")
        w.settingsMenu.getNSPopUpButton().setToolTip_(
            "Settings: copy them as text (JSON) to paste into another Kinetikern3 window or keep with a project, "
            "save them to a file and load them, or go back to Kinetikern3's defaults. The sample text, the master "
            "and the font's spacing groups stay with the font.")
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
            "The font's pairs from loosest to tightest, measured against Kinetikern3.")
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
            "Measure an italic master along its italic angle (3° or more): Kinetikern3 sees the outlines sheared "
            "upright about half the x-height, where Glyphs measures italic sidebearings, the way its model, built "
            "on upright letters, measures. Sidebearings and kerning are horizontal, so they apply to the slanted "
            "outlines as they are. Measured upright instead, italics come out too loose and uneven.")
        w.cancel = vanilla.Button((-100, 122, 86, 22), "Cancel", callback=self.cancelJob)

        w.line = vanilla.HorizontalLine((0, 152, -0, 1))
        w.status = vanilla.TextBox((14, -22, -14, 16), "", sizeStyle="mini")

        # row 5: how the panes show the two, and the text size
        w.viewLabel = vanilla.TextBox((14, 162, 36, 17), "View", sizeStyle="small")
        w.viewMode = vanilla.SegmentedButton((50, 159, 270, 22), [dict(title=t) for t in VIEWS],
                                             callback=self.viewModeChanged, sizeStyle="small")
        view = int(self._setting("view", VIEW_SIDE, int))
        w.viewMode.set(view if 0 <= view < len(VIEWS) else VIEW_SIDE)
        w.viewMode.getNSSegmentedButton().setToolTip_(
            "Side by side: the font as it is beside Kinetikern3. Toggle: one at a time in one pane — click the "
            "preview or press the space bar to switch, so the change shows as a jump. Overlay: the font as it is "
            "laid over Kinetikern3, line by line with the same line breaks.")
        w.showing = vanilla.SegmentedButton((334, 159, 230, 22), [dict(title="Font as it is"), dict(title="Kinetikern3")],
                                            callback=self.showingChanged, sizeStyle="small")
        style = int(self._setting("overlayStyle", OVERLAY_RED, int))
        w.overlayStyle = vanilla.PopUpButton((334, 160, 170, 20), OVERLAY_STYLES, callback=self.overlayStyleChanged,
                                             sizeStyle="small")
        w.overlayStyle.set(style if 0 <= style < len(OVERLAY_STYLES) else OVERLAY_RED)
        w.overlayStyle.getNSPopUpButton().setToolTip_(
            "Font in red: the font as it is in translucent red over Kinetikern3 in the text colour. Both "
            "translucent: both in the text colour at half strength, so where they differ a glyph shows twice.")
        w.alignWords = vanilla.CheckBox((512, 160, 110, 18), "Align words", sizeStyle="small",
                                        value=self._setting("alignWords", True, bool), callback=self.alignWordsChanged)
        w.alignWords.getNSButton().setToolTip_(
            "Each word of the font as it is starts where the same word starts in Kinetikern3's, so the two "
            "spacings show word by word instead of drifting apart along the line (Toggle shows the drift).")
        w.viewHint = vanilla.TextBox((630, 163, -330, 14), "", sizeStyle="mini")
        size = min(MAX_SIZE, max(MIN_SIZE, self._setting("size", DEFAULT_SIZE)))
        self._size = size
        w.sizeLabel = vanilla.TextBox((-318, 162, 58, 17), "Text size", sizeStyle="small")
        w.size = vanilla.Slider((-256, 159, 166, 22), minValue=math.log(MIN_SIZE), maxValue=math.log(MAX_SIZE),
                                value=math.log(size), callback=self.sizeChanged)
        w.size.getNSSlider().setToolTip_("The text size of the preview (a pinch on the preview changes it too).")
        w.sizeField = vanilla.EditText((-84, 159, 48, 21), "", callback=self.sizeFieldChanged, continuous=False,
                                       sizeStyle="small")
        w.sizeUnit = vanilla.TextBox((-32, 163, 20, 14), "pt", sizeStyle="mini")

        # proofing panes side by side
        self.left = kp.ProofPane("Font")
        self.right = kp.ProofPane("Kinetikern3")
        # Toggle and Overlay: the font as it is, laid over Kinetikern3's pane with its line breaks
        self.overlay = kp.ProofPane("Font over Kinetikern3", overlay=True)
        left_group = vanilla.Group((0, 0, -0, -0))
        left_group.caption = vanilla.TextBox((10, 4, -10, 16), "Font as it is — current spacing and kerning",
                                             sizeStyle="small")
        left_group.scroll = vanilla.ScrollView((0, 22, -0, -0), self.left.view, hasHorizontalScroller=False,
                                               autohidesScrollers=True)
        right_group = vanilla.Group((0, 0, -0, -0))
        right_group.caption = vanilla.TextBox((10, 4, -10, 16), "Kinetikern3 — live sidebearings + kerning",
                                              sizeStyle="small")
        right_group.scroll = vanilla.ScrollView((0, 22, -0, -0), self.right.view, hasHorizontalScroller=False,
                                                autohidesScrollers=True)
        self.proofs = proofs = vanilla.SplitView((0, 0, -0, -0), [
            dict(view=left_group, identifier="font", minSize=250),
            dict(view=right_group, identifier="kinetikern3", minSize=250),
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

        w.split = vanilla.SplitView((0, 186, -0, -26), [
            dict(view=proofs, identifier="proofs", minSize=220),
            dict(view=input_group, identifier="input", size=190, minSize=90),
        ], isVertical=False, dividerStyle="thin")
        self.left_group, self.right_group, self.input_group = left_group, right_group, input_group

        w.bind("close", self.windowClosed)
        w.open()
        panes = [left_group.scroll.getNSScrollView(), right_group.scroll.getNSScrollView()]
        self.sync.attach(panes + [self.editor.getNSScrollView()], fit_width=panes)
        self.overlay.view.setFrame_(self.right.view.bounds())
        self.right.view.addSubview_(self.overlay.view)
        for pane in (self.left, self.right, self.overlay):
            pane.view.kk3_on_magnify = self._magnify
        self._show_slider_values()
        self._apply_view()
        self._update_labels()
        self._update_max_pairs_note()

    def _master(self):
        return self.font.masters[self.w.master.get()]

    def _physics(self):
        return physics_from_sliders(self.w.tightness.get(), self._intensity())

    def _kerning_on(self):
        return self.w is None or getattr(self.w, "kerning", None) is None or bool(self.w.kerning.get())

    def _intensity(self):
        """The kerning intensity the solves use, in percent: 0 while the kerning switch is off."""
        return float(self.w.intensity.get()) if self._kerning_on() else 0.0

    def _show_slider_values(self):
        """The fields show their sliders' values (after a slider moved, or a field took a value)."""
        w = self.w
        if w is None:
            return
        w.tightField.set("%+.2f" % float(w.tightness.get()))
        w.sizeField.set("%g" % round(self._size, 1))
        w.intensityField.set("%g" % round(float(w.intensity.get()), 1))
        w.harnessStrengthField.set("%g" % round(float(w.harnessStrength.get()), 1))

    def _point_size(self):
        return float(self._size)

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
        """Per-glyph options: the spacing groups' (frozen, Looseness offset,
        kerning force), each glyph's category offset on top, and with Keep the
        font's fit its fit group (None: nothing changes anything)."""
        if self.snapshot is None or not self.engine.features & kb.FEATURE_GLYPH_OPTS:
            return None
        base = self.groups.opts_for(self.snapshot.names)
        offsets = dict((k, v) for k, v in self.category_offsets.items() if v)
        fit = self._keep_fit_on()
        if not offsets and not fit:
            return base
        out = []
        for i, cat in enumerate(self._glyph_categories()):
            frozen, loose, force = tuple(base[i])[:3] if base else (False, 0.0, 1.0)
            if not frozen:
                loose += offsets.get(cat, 0.0)
            out.append((frozen, loose, force, self._fit_group(cat) if fit else 0))
        return out

    # ---------------------------------------------------------- categories
    def font_fit_available(self):
        return bool(self.engine.features & kb.FEATURE_FONT_FIT)

    def _keep_fit_on(self):
        return bool(self.keep_fit and self.font_fit_available())

    def _offsets_setting(self):
        """The categories' Looseness offsets as saved ({category: offset}, the non-zero ones)."""
        raw = self._setting("categoryOffsets", "", str)
        try:
            d = json.loads(raw) if raw else {}
        except ValueError:
            d = {}
        return self._valid_offsets(d)

    @staticmethod
    def _valid_offsets(d):
        lo, hi = kg.OFFSET_RANGE
        out = {}
        if isinstance(d, dict):
            for k, v in d.items():
                if (k in kg.CATEGORY_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool) and v == v
                        and v != 0):
                    out[k] = min(hi, max(lo, float(v)))
        return out

    def _glyph_categories(self):
        """The category of each glyph of the snapshot (kk3_groups.category_of), read once per snapshot."""
        snap = self.snapshot
        if snap is None:
            return []
        if self._categories is None or self._categories[0] is not snap:
            self._categories = (snap, kg.snapshot_categories(snap))
        return self._categories[1]

    def category_counts(self):
        """{category: glyphs} of the snapshot, None while the font is read."""
        if self.snapshot is None:
            return None
        out = {}
        for c in self._glyph_categories():
            out[c] = out.get(c, 0) + 1
        return out

    def _fit_group(self, category):
        """The engine fit group of a category: fitted on its own, or following the lowercase (small caps by
        convention follow the letters' rhythm through their convention)."""
        g = kg.FIT_GROUPS.get(category)
        if category == "smallcaps" and self._small_caps_on():
            g = None
        return g if g is not None else kg.FIT_GROUPS["lowercase"] | kb.FIT_FOLLOW

    def set_keep_fit(self, on):
        self.keep_fit = bool(on)
        self._save("keepFit", self.keep_fit)
        self._categories_changed()

    def set_stay_close(self, percent):
        self.stay_close = min(100.0, max(0.0, float(percent)))
        self._save("stayClose", self.stay_close)
        self._categories_changed()

    def set_category_offset(self, key, value):
        if key not in kg.CATEGORY_KEYS:
            return
        d = dict(self.category_offsets)
        d[key] = value
        self.set_category_offsets(d)

    def set_category_offsets(self, offsets):
        self.category_offsets = self._valid_offsets(offsets)
        self._save("categoryOffsets", json.dumps(self.category_offsets, sort_keys=True))
        self._categories_changed()

    def _categories_changed(self):
        self._update_labels()
        self._request_preview()
        if self.harness_window is not None:
            self.harness_window.settings_changed()

    def _categories_active(self):
        """How many adjustments the Categories window makes (offsets, the fit, staying close)."""
        return len([v for v in self.category_offsets.values() if v]) + int(self._keep_fit_on()) + int(
            self.stay_close > 0)

    def _update_categories_button(self):
        if self.w is None or getattr(self.w, "categoriesButton", None) is None:
            return
        n = self._categories_active()
        self.w.categoriesButton.setTitle("Categories (%d)…" % n if n else "Categories…")
        lines = ["Tightness by category: a Looseness for capitals, lowercase, figures, quotes, brackets and the "
                 "rest on top of the main one; Keep the font's fit; Stay close to the font's spacing."]
        if self._keep_fit_on():
            lines.append(self.font_fit_note())
        if self.stay_close > 0:
            lines.append("Staying %d %% of the way to the font's own sidebearings." % round(self.stay_close))
        if self.category_offsets:
            labels = dict(kg.CATEGORIES)
            lines.append("Offsets: " + ", ".join("%s %+.2f" % (labels[k], v)
                                                for k, v in sorted(self.category_offsets.items())))
        self.w.categoriesButton.getNSButton().setToolTip_("\n".join(lines))

    def font_fit_note(self):
        """A line on keeping the font's fit (the Categories window, the button's tooltip)."""
        if not self.font_fit_available():
            return "This build of the engine cannot fit to the font: rebuild the plugin (build.sh)."
        if not self._keep_fit_on():
            return "Off: every category at the main Looseness, plus its offset below."
        fits = self._category_fits
        names = {1: "capitals", 2: "lowercase", 3: "small caps", 4: "letters without case"}
        parts = ["%s %+.2f" % (names[g], fits[g]) for g in sorted(fits) if g in names]
        if not parts:
            return "On: the font's own tightness is measured with the next preview."
        return ("On: the font's own spacing sits at Looseness %s; the main Looseness and the offsets below move "
                "from there." % ", ".join(parts))

    def openCategories(self, sender=None):
        if self.categories_window is not None:
            self.categories_window.front()
            return self.categories_window
        import kk3_categories_window
        self.categories_window = kk3_categories_window.CategoriesWindow(self)
        return self.categories_window

    def categories_window_closed(self):
        self.categories_window = None

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
                              budget=budget, threads=self._threads(), fit_frozen=fit, style=self._style(),
                              fit_font=self._keep_fit_on(),
                              keep=self.stay_close / 100.0 if self.font_fit_available() else 0.0)

    def _solve_key(self, whole):
        """What a result depends on, to tell whether it can be reused (the
        budget at KEY_BUDGET)."""
        spring, repulsion, coupling = self._physics()
        return (self._generation, round(spring, 9), round(repulsion, 9), round(coupling, 9),
                round(self._threshold(), 6), self._budget() if whole else 0, self.groups.key(),
                self.join_count, self._join_mode(),
                tuple(sorted(self.category_offsets.items())), self._keep_fit_on(), round(self.stay_close, 3),
                # the last five as they are: the self-tests read them from the end
                self._small_caps_on(), self._figures_on(), self._punctuation_on(),
                self._style(),
                round(self._harness_strength(), 4))

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
            what = "kept joins" if self._keeps_joins() else "frozen glyphs"
            if self.engine.features & kb.FEATURE_FIT_OFFSET:
                # the solve ran at the fitted Looseness plus the slider's
                spring, repulsion, _c = physics_from_sliders(self.w.tightness.get() + self._fitted,
                                                             self.w.intensity.get())
                fitted = " · 0 = the %s' Looseness, %+.2f" % (what, self._fitted)
            else:
                fitted = " · matched to the %s: %+.2f" % (what, self._fitted)
        self.w.tightValue.set("spring %.2f · repulsion %.2f%s%s" % (spring, repulsion, gap, fitted))
        if not self._kerning_on():
            self.w.sdfValue.set("kerning off: Kinetikern3's spacing alone")
        else:
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
        self._update_categories_button()

    def _update_harness_label(self):
        if self.w is None or getattr(self.w, "harness", None) is None:
            return
        if not self.harness_available():
            text = ("harness unavailable: rebuild the plugin (build.sh)" if not self.engine.features & kb.FEATURE_HARNESS
                    else "harness unavailable: kk3_harness.json is missing")
        elif not self.w.harness.get():
            text = "harness off"
        else:
            plan = self._harness_plan()
            text = plan.summary() if plan is not None else "harness on · waiting for the font to be read"
        # one short line (the harness's summary first); the tooltip says it all
        figures = self.punctuation_available() and kpu.has_figures()
        caps = self.punctuation_available() and kpu.has_small_caps()
        p_on, f_on = bool(self.w.punctuation.get()), figures and bool(self.w.figures.get())
        c_on = caps and bool(self.w.smallCaps.get())
        phrase = kpu.STYLE_PHRASES.get(kpu.style_of(self._style()), "text faces' conventions")

        def listed(names):
            return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        off = [n for n, on, have in (("punctuation", p_on, True), ("figures", f_on, figures), ("small caps", c_on, caps))
               if have and not on]
        if not self.punctuation_available():
            punct = "punctuation: rebuild the plugin (build.sh)"
        elif not (p_on or f_on or c_on):
            punct = listed(off) + " as the shapes say"
        else:
            # every switch on: the conventions followed; else which ones the shapes alone set
            punct = phrase + ("" if not off else ", %s as the shapes say" % listed(off))
        self.w.harnessValue.set("%s · %s" % (text, punct))
        tip = ["Designer harness: " + text]
        if self.punctuation_available():
            tip.append("Punctuation: " + (kpu.describe(self._style()) if p_on else "spaced and kerned from its shapes alone"))
        if figures:
            tip.append("Figures: " + ("proportional figures, superscripts, subscripts and ordinals by %s; tabular "
                                      "figures keep their widths"
                                      % kpu.STYLE_PHRASES.get(kpu.style_of(self._style()), "text faces' conventions")
                                      if f_on else "spaced from their shapes alone"))
        if caps:
            tip.append("Small caps: " + ("by %s" % phrase if c_on else "spaced from their shapes alone"))
        self.w.harnessValue.getNSTextField().setToolTip_("\n".join(tip))
        self.w.punctuation.enable(self.punctuation_available())
        self.w.figures.enable(self.punctuation_available() and kpu.has_figures())
        self.w.smallCaps.enable(self.punctuation_available() and kpu.has_small_caps())
        strength = bool(self.w.harness.get()) and self.harness_available()
        self.w.harnessStrength.enable(strength)
        self.w.harnessStrengthField.enable(strength)

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
        for control in (w.tightness, w.tightField, w.kerning, w.threshold, w.thresholdField, w.maxPairs, w.threads,
                        w.master, w.scope, w.replace, w.reload):
            control.enable(not busy)
        for control in (w.intensity, w.intensityField):
            control.enable(not busy and self._kerning_on())
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
            self._timer_target = KK3WindowTimer.alloc().init()
            self._timer_target.kk3_callback = self._tick
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
            if self._join_finder is not None:
                what = "finding joins"
                self._step_join_finder()
                return
            if self._stepper is not None:
                what = "%s: %s" % (self._stepper_kind, self._stepper.phase)
                self._step_stepper()
                return
            if (self._left_due or self._right_due or self._overlay_due) and time.time() >= self._render_at:
                # one pane per tick: each lays out in its own display pass;
                # what a long sample needs for the first time is made first,
                # a slice per tick. The overlay comes after the right pane
                # (it breaks its lines where the right pane does).
                if self._left_due:
                    pane, what, render = self.left, "left pane", self._render_left
                elif self._right_due:
                    pane, what, render = self.right, "right pane", self._render_right
                else:
                    pane, what, render = self.overlay, "overlay", self._render_overlay
                if (self.snapshot is not None and pane.view.window() is not None and not (
                        pane is self.left and self._view() != VIEW_SIDE) and
                        not pane.warm(self.tokens, self.snapshot, self._point_size(), READ_SLICE)):
                    what += " (warming)"
                    return
                render()
                return
            if self._job is not None:
                what = "poll " + (self._job_kind or "")
                self._poll_job()
            if self._job is None and self._preview_pending:
                what = "start preview"
                self._start_preview()
            if self._slant_probe is not None:
                what = "checking the slant"
                self._step_slant_probe()
            if (self._reader is None and self._job is None and not self._preview_pending and
                    self._join_finder is None and self._stepper is None and not self._left_due and
                    not self._right_due and not self._overlay_due and self._slant_probe is None):
                self._stop_timer()
        except Exception:
            self._fail("Kinetikern3 stopped on an error — see the Macro panel.", traceback.format_exc())
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
        self._join_finder = None
        stem = self._stem_slant(master)
        self._reader = ks.SnapshotReader(self.font, master, along_slant=self._along_slant(master), stem_slant=stem)
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
        the setting is on, found first a step per tick (_join_steps: reading
        a big master's kerning and the four tests took 0.6 s at once on
        Lato)."""
        self._join_finder = None  # a search for the setting before is dropped
        if not self._connected():
            self.joins, self.join_count, self.join_note = None, 0, ""
            self._prepare(None)
            return
        self.join_ms = 0.0
        self._join_finder = self._join_steps(self.snapshot)
        self._set_state("preparing")
        self._show_progress("Looking for joins…", 0.0)
        self._set_status("Looking for a connected script's joins in %s…" % self.snapshot.master_name)
        self._run_timer(READ_INTERVAL)

    def _step_join_finder(self):
        """One step of finding the joins; Phase 1 once they are found. The
        time spent here is the join search's (join_ms: on the main thread)."""
        t = time.perf_counter()
        try:
            next(self._join_finder)
            return
        except StopIteration as stop:
            joins = stop.value
        except Exception:
            self._join_finder = None
            self._fail("Could not look for joins — see the Macro panel.", traceback.format_exc())
            return
        finally:
            self.join_ms += 1000.0 * (time.perf_counter() - t)
        self._join_finder = None
        self._run_timer(POLL_INTERVAL)
        self._prepare(joins)

    def _prepare(self, joins):
        """Phase 1 with `joins` (None: no connected script)."""
        snapshot = self.snapshot
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
        """_join_steps in one call."""
        steps = self._join_steps(snap)
        while True:
            try:
                next(steps)
            except StopIteration as stop:
                return stop.value

    def _join_steps(self, snap):
        """The join bands of a connected script, learned from the master's
        own spacing and kerning (Spacing QA's rule); None when its letters do
        not join. Sets the note the window shows, and the letters' kinds and
        the master's kerning the join checker reads. A generator: it yields
        between the parts (the kerning read in slices, each test of the
        engine in a step of its own) and returns the bands."""
        self.joins, self.join_count = None, 0
        self.join_kinds, self.join_current = None, ()
        try:
            from kk3_pairs_window import current_kerning_steps
            # every letter is measured against the basic a–z (Spacing QA's
            # partners): milliseconds even for a font of a thousand letters
            kinds = bytearray(len(snap.names))
            letters = 0
            for i, spec in enumerate(snap.specs):
                if spec.group in (kb.GROUP_LOWERCASE, kb.GROUP_UPPERCASE):
                    cp = getattr(snap.infos.get(snap.names[i]), "unicode", None)
                    kinds[i] = kb.JOINKIND_LOWER if cp is not None and 0x61 <= cp <= 0x7A else kb.JOINKIND_UPPER
                    letters += 1
            current = yield from current_kerning_steps(snap, self._kerning.table(snap.master_id), READ_SLICE)
            yield
            bands = self.engine.detect_joins(snap.packer, snap.upm, self._x_height(snap), kinds, current,
                                             kb.JOINRULE_BOTH)
        except Exception as e:
            print(traceback.format_exc())
            self.join_note = "could not look for joins: %s" % e
            return None
        n = sum(1 for left, right in bands if left or right)
        if not n:
            # nothing overlaps; a line or grid drawn exactly from edge to edge
            # still touches every neighbour, figures included: keep it whole
            decorated = False
            yield
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
            yield
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
            yield
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
    def _stem_slant(self, master):
        """tan of the slant `master`'s stems show when it declares no italic
        angle (Spacing QA's detector, in the engine), else 0; read once per
        master."""
        mid = str(master.id)
        if ks.slant_measurable(ks.master_italic_angle(master)):
            return 0.0
        if mid not in self._stem_slants:
            try:
                self._stem_slants[mid] = ks.stem_slant(self.engine, self.font, master)
            except Exception:
                print(traceback.format_exc())
                self._stem_slants[mid] = 0.0
        return self._stem_slants[mid]

    def _leans_undeclared(self, master=None):
        """`master` (the current one) declares no italic angle but its stems
        lean: whether to measure along them is the probe's or the user's."""
        master = master if master is not None else self._master()
        return (not ks.slant_measurable(ks.master_italic_angle(master))
                and ks.slant_measurable(math.degrees(math.atan(self._stem_slants.get(str(master.id), 0.0)))))

    def _along_slant(self, master=None):
        """Measure along the slant: the Along the italic angle setting for a
        master that declares an angle; for one whose stems lean without
        declaring it, the user's choice for it, else the probe's verdict
        (upright until it is in)."""
        if self.w is not None and self._leans_undeclared(master):
            mid = str((master if master is not None else self._master()).id)
            if mid in self._lean_choices:
                return self._lean_choices[mid]
            verdict = self._lean_verdicts.get(mid)
            return bool(verdict and verdict[0])
        if self._along_declared is None:
            self._along_declared = bool(self._setting("alongSlant", True, bool))
        return self._along_declared

    def _update_slant_label(self):
        """The checkbox names the master's angle, or the slant its stems show
        when it declares none (with what the probe found); it is disabled for
        an upright master (and for an angle of 60° or more, which is an error
        in the font)."""
        box = getattr(self.w, "alongSlant", None) if self.w is not None else None
        if box is None:
            return
        snap = self.snapshot
        degrees = snap.slant_degrees if snap is not None else 0.0
        source = snap.slant_source if snap is not None else ""
        if source:
            shown = "%g" % round(abs(degrees), 1)
            box.setTitle(("Along the %s° italic angle" if source == "declared" else "Along the %s° slant of its stems") % shown)
            box.set(self._along_slant())
            box.enable(self.state not in ("applying",) and self._stepper is None)
        else:
            box.setTitle("Along the italic angle")
            box.enable(False)
        tip = ("Measure an italic master along its italic angle (3° or more): Kinetikern3 sees the outlines sheared "
               "upright about half the x-height, where Glyphs measures italic sidebearings, the way its model, built "
               "on upright letters, measures. Sidebearings and kerning are horizontal, so they apply to the slanted "
               "outlines as they are. Measured upright instead, italics come out too loose and uneven.")
        if source == "measured":
            tip = ("This master declares no italic angle, but its stems lean by %s° (the slant of l i h n m u r k b "
                   "p, as Spacing QA reads it). Measured along that slant, Kinetikern3 sees the outlines sheared "
                   "upright about half the x-height, as for an italic. Spacing QA's rule decides the default: along "
                   "the slant where that leaves at most 80 %% of the shape error upright, or where upright the model "
                   "cannot follow the font (its fit stops at the limit of the Looseness range, or lies beyond ±2.5, "
                   "outside what fonts use) and along the slant it can. %s"
                   % ("%g" % round(abs(degrees), 1), self._verdict_text(snap)))
        box.getNSButton().setToolTip_(tip)

    def _verdict_text(self, snap=None):
        """What the probe found for the current master, in words."""
        mid = str(self._master().id)
        verdict = self._lean_verdicts.get(mid)
        if self._slant_probe is not None and self._slant_probe.master_id == mid:
            return "Checking both ways on the kernel's glyphs…"
        if verdict is None:
            return ""
        if verdict[1] is None:
            return "The check both ways could not be made: measured upright, as drawn."
        lean, ((se_up, fit_up), (se_lean, fit_lean)) = verdict
        limit = lambda f: abs(f) >= 6.0 - 1e-6
        far = lambda f: abs(f) >= 2.5
        if lean and limit(fit_up) and not limit(fit_lean):
            why = ("upright, the model cannot follow this master: its fit stops at the limit (%+.2f); along the "
                   "slant it fits at %+.2f" % (fit_up, fit_lean))
        elif lean and se_lean > 0.8 * se_up and far(fit_up) and not far(fit_lean):
            why = ("upright, the model follows this master only far outside what fonts use (Looseness %+.2f); "
                   "along the slant it fits at %+.2f" % (fit_up, fit_lean))
        elif lean:
            why = "it fits clearly better along the slant (shape error %.1f, against %.1f upright)" % (se_lean, se_up)
        else:
            why = ("upright fits as well (shape error %.1f at Looseness %+.2f, against %.1f at %+.2f along the slant), "
                   "so it is measured as drawn" % (se_up, fit_up, se_lean, fit_lean))
        return "Checked both ways on the kernel's glyphs: %s." % why

    def _start_slant_probe(self):
        """For a master that leans without declaring an italic angle and has
        no verdict yet (nor a choice of the user's): Spacing QA's check both
        ways, in the background (kk3_snapshot.SlantProbe)."""
        snap = self.snapshot
        if self._slant_probe is not None or snap is None or self.w is None:
            return
        master = self._master()
        mid = str(master.id)
        if (not self._leans_undeclared(master) or mid in self._lean_verdicts or mid in self._lean_choices
                or not self.engine.features & kb.FEATURE_STEM_SLANT):
            return
        try:
            from kk3_pairs_window import current_kerning_steps
            table = self._kerning.table(snap.master_id)
            joins = None
            if self._keeps_joins() and self.joins is not None:
                joins = (dict((snap.names[i], b) for i, b in enumerate(self.joins) if i < len(snap.names)),
                         dict((snap.names[i], int(k)) for i, k in enumerate(self.join_kinds or ()) if i < len(snap.names)))
            # the conventions as they are now, for every solve of the probe
            style = self._style()
            punct = self._punctuation_on()
            figs = self._figures_on()
            caps = self._small_caps_on()

            def harness(sub, looseness, kept):
                try:
                    return kh.Plan(sub, looseness, 1.0, style=style, kept=kept, punctuation=punct).engine_arg()
                except Exception:
                    print(traceback.format_exc())
                    return None

            def punctuation(sub):
                try:
                    return kpu.engine_arg(sub, style, punctuation=punct, figures=figs, small_caps=caps)
                except Exception:
                    print(traceback.format_exc())
                    return None

            self._slant_probe = ks.SlantProbe(
                self.engine, self.font, master, self._stem_slants.get(mid, 0.0), snap,
                physics=lambda looseness: physics_from_sliders(looseness, 100.0), joins=joins,
                current_steps=lambda sub: current_kerning_steps(sub, table, READ_SLICE),
                harness=harness if self.engine.features & kb.FEATURE_HARNESS else None,
                threads=self._threads(), budget_s=READ_SLICE,
                punctuation=punctuation if punct or figs or caps else None, style=style)
        except Exception:
            print(traceback.format_exc())
            self._slant_probe = None
            return
        self._update_slant_label()
        self._run_timer(POLL_INTERVAL)

    def _step_slant_probe(self):
        """A step of the probe; its verdict sets how the master is measured,
        read again when that changes (unless the user chose)."""
        probe = self._slant_probe
        if not probe.step(READ_SLICE):
            return
        self._slant_probe = None
        if probe.lean is None:
            if probe.error == "cancelled":
                return  # a read or a release stopped it: the next context starts it again
            if probe.trace:
                print(probe.trace)
            self._lean_verdicts[probe.master_id] = (None, None)  # upright, and not tried again
            self._set_status("Could not check whether to measure along the slant of the stems (%s): measured "
                             "upright, as drawn." % probe.error)
            self._update_slant_label()
            return
        mid = probe.master_id
        self._lean_verdicts[mid] = (probe.lean, probe.numbers)
        self._update_slant_label()
        if mid != str(self._master().id) or mid in self._lean_choices or self.snapshot is None:
            return
        if probe.lean == bool(self.snapshot.slant):
            self._set_status("%s %s" % ("Measured along the slant of the stems." if probe.lean else
                                       "Measured upright, as drawn.", self._verdict_text()))
            return
        if self._reader is not None or self._stepper is not None or (self._job is not None and self._job_kind == "whole"):
            return  # the next read follows the verdict
        self._load_master(self._master())
        self._set_status("Reading again along the %g° slant of the stems: %s" % (
            round(abs(math.degrees(math.atan(probe.slant))), 1), self._verdict_text()))

    def alongSlantChanged(self, sender):
        if self.snapshot is not None and self._leans_undeclared():
            # the user's choice for this master, over the probe's
            self._lean_choices[str(self._master().id)] = bool(self.w.alongSlant.get())
        else:
            self._along_declared = bool(self.w.alongSlant.get())
            self._save("alongSlant", self._along_declared)
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
        self._start_slant_probe()
        if self.harness_window is not None:
            self.harness_window.snapshot_changed()  # its list holds the old snapshot's glyph indices
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
                                          harness=plan.engine_arg() if plan is not None else None,
                                          punctuation=self._punctuation_arg())
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
        self._category_fits = result.category_fits() if result is not None else {}
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
        if self.categories_window is not None:
            self.categories_window.result_ready()

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

    def _word_space_note(self):
        """The master's word space against what designers of its kind set for
        its n as the result spaces it (kk3_punctuation.word_space_note), or
        None."""
        try:
            snap, res = self.snapshot, self.result
            i = snap.index.get("n") if snap is not None else None
            glyph = self.font.glyphs["space"] if self.font is not None else None
            if i is None or res is None or glyph is None:
                return None
            layer = glyph.layers[snap.master_id]
            return kpu.word_space_note(float(layer.width), float(res.metrics[i].advance), self._style())
        except Exception:
            return None

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
            space = self._word_space_note()
            return ("Whole font (%s): %s glyphs kerned · %s pairs in scope · %s class pairs solved · %s entries "
                    "(%s class pairs, %s exceptions%s)%s · %.1f s on %d threads%s"
                    % (snap.master_name, _count(st["kern_glyphs"]), _count(st["pairs_in_scope"]),
                       _count(st["class_pairs"]), _count(res.entry_count), _count(st["class_entries"]),
                       _count(st["exception_entries"]), more, dropped, self._job_elapsed, st["threads"],
                       " · " + space if space else ""))
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
                                          harness=plan.engine_arg() if plan is not None else None,
                                          punctuation=self._punctuation_arg())
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
        if self._join_finder is not None:
            self._join_finder = None
            stopped = "finding joins"
        if self._job is not None:
            self._job.free()  # cancels; never blocks
            stopped = self._job_kind
            self._job = None
            self._job_kind = None
        self._preview_pending = False
        self._apply_when_done = self._apply_after_preview = self._whole_after_preview = False
        self._stop_timer()
        if self._left_due or self._right_due or self._overlay_due:
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
        self._join_finder = None
        if self._slant_probe is not None:
            self._slant_probe.cancel()
            self._slant_probe = None
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
        return self._left_due or self._right_due or self._overlay_due

    def _panes_due(self, left=False, right=False, delay=0.0):
        """Lays out the panes again on the coming ticks, one pane per tick
        (`delay`: not before that many seconds from now)."""
        self._left_due = self._left_due or left
        self._right_due = self._right_due or right
        if delay:
            self._render_at = max(self._render_at, time.time() + delay)
        if self._timer is None:
            self._run_timer(POLL_INTERVAL)

    def _font_kern(self, snap):
        """kern(left, right) of the master's kerning as it is, for the panes that show the font."""
        table = self._kerning.table(snap.master_id)
        kerning = self._kerning

        def kern(left, right):
            try:
                return kerning.value(table, left, right)
            except Exception:
                return 0.0
        return kern

    def _render_left(self):
        self._left_due = False
        snap = self.snapshot
        if snap is None or self.w is None:
            return
        if self._view() != VIEW_SIDE:
            # hidden: laid out when it shows again; the font laid over Kinetikern3 shows the change now
            self._left_stale = True
            self._overlay_due = True
            return
        self._left_stale = False
        self.left.render(self.tokens, snap, metrics=self._metrics_in_font, kern=self._font_kern(snap),
                         point_size=self._point_size())

    def _render_overlay(self):
        """The font as it is, over Kinetikern3's pane, its lines broken where that pane's are."""
        self._overlay_due = False
        snap = self.snapshot
        if snap is None or self.w is None or self._view() == VIEW_SIDE:
            return
        self.overlay.render(self.tokens, snap, metrics=self._metrics_in_font, kern=self._font_kern(snap),
                            point_size=self._point_size(), color=self._overlay_color(), breaks=self.right.line_starts())
        if self._view() == VIEW_OVERLAY and self.w.alignWords.get():
            self.overlay.align_words(self.right)

    def _right_color(self):
        """Kinetikern3's colour: the text colour, at half strength under a translucent overlay."""
        if self._view() == VIEW_OVERLAY and self.w.overlayStyle.get() == OVERLAY_TRANSLUCENT:
            return NSColor.textColor().colorWithAlphaComponent_(0.5)
        return None

    def _overlay_color(self):
        if self._view() == VIEW_TOGGLE:
            return None  # the text colour: it covers Kinetikern3's pane
        if self.w.overlayStyle.get() == OVERLAY_TRANSLUCENT:
            return NSColor.textColor().colorWithAlphaComponent_(0.5)
        return NSColor.systemRedColor().colorWithAlphaComponent_(0.7)

    def _right_caption(self, res=None):
        mode = self._view()
        if mode == VIEW_TOGGLE:
            caption = ("Showing the font as it is — current spacing and kerning" if self._showing_font
                       else "Showing Kinetikern3 — live sidebearings + kerning")
        elif mode == VIEW_OVERLAY:
            caption = ("The font as it is (red) over Kinetikern3 — live sidebearings + kerning"
                       if self.w.overlayStyle.get() == OVERLAY_RED
                       else "The font as it is over Kinetikern3, both translucent")
        else:
            caption = "Kinetikern3 — live sidebearings + kerning"
        if res is not None and res.ptr is not None and self._result_kind == "whole" and not (
                mode == VIEW_TOGGLE and self._showing_font):
            caption += " (whole-font result: %s entries)" % _count(res.entry_count)
        return caption

    def _render_right(self):
        self._right_due = False
        snap = self.snapshot
        if snap is None or self.w is None:
            return
        res = self.result
        if self._view() != VIEW_SIDE:
            self._overlay_due = True  # its line breaks follow this pane's
        if res is None or res.ptr is None:
            self.right.render(self.tokens, snap, metrics=None, kern=None, point_size=self._point_size(),
                              color=self._right_color())
            self.right_group.caption.set(self._right_caption())
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

        self.right.render(self.tokens, snap, metrics=metrics, kern=kern, point_size=self._point_size(),
                          color=self._right_color())
        self.right_group.caption.set(self._right_caption(res))

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
        return self._ask("Apply Kinetikern3 to “%s”?" % self.snapshot.master_name, ka.describe_plan(plan),
                         ("Apply", "Cancel")) == 0

    def plan_names(self, result):
        """Glyphs whose sidebearings an apply of `result` writes: every glyph
        the right pane shows for a sample preview (also those the engine
        spaced but did not kern) and every glyph the preview kerned (the
        text may have lost some since: their kerning is written, so is the
        spacing it was computed for); None (kk3_apply decides) for a
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
                    "revert": "Reverting failed"}.get(kind, "Kinetikern3 stopped")
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
            print("Kinetikern3 apply read-back: %r" % (summary["examples"],))
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

    # ------------------------------------------------------------ settings
    def settings(self):
        """Every control's setting, as a plain dict: Copy and Save Settings write it as JSON, Paste and Load
        Settings read it back (apply_settings). The sample text, the master and the font's spacing groups
        belong to the font and stay out."""
        w = self.w
        return {
            SETTINGS_FORMAT: SETTINGS_VERSION,
            "looseness": round(float(w.tightness.get()), 4),
            "kerning": bool(w.kerning.get()),
            "intensity": round(float(w.intensity.get()), 2),
            "threshold": round(self._threshold(), 3),
            "harness": bool(w.harness.get()),
            "harnessStrength": round(float(w.harnessStrength.get()), 2),
            "conventions": self.harness_style,
            "punctuation": bool(w.punctuation.get()),
            "figures": bool(w.figures.get()),
            "smallCaps": bool(w.smallCaps.get()),
            "connected": bool(w.connected.get()),
            "joinMode": JOIN_NAMES[self._join_mode()],
            "alongSlant": bool(w.alongSlant.get()),
            "maxPairs": self._budget(),
            "threads": self._threads(),
            "applyTo": SCOPE_NAMES[int(w.scope.get())],
            "replaceKerning": bool(w.replace.get()),
            "textSize": round(self._size, 1),
            "view": VIEW_NAMES[self._view()],
            "overlay": OVERLAY_NAMES[int(w.overlayStyle.get())],
            "alignWords": bool(w.alignWords.get()),
            "keepFit": bool(self.keep_fit),
            "stayClose": round(self.stay_close, 1),
            "categories": dict(sorted(self.category_offsets.items())),
        }

    @staticmethod
    def default_settings():
        """Kinetikern3's own settings (Reset Settings)."""
        return {SETTINGS_FORMAT: SETTINGS_VERSION, "looseness": 0.0, "kerning": True, "intensity": 100.0,
                "threshold": DEFAULT_THRESHOLD, "harness": False, "harnessStrength": 100.0, "conventions": kh.AUTO,
                "punctuation": True, "figures": True, "smallCaps": True, "connected": True, "joinMode": "keep",
                "alongSlant": True, "maxPairs": DEFAULT_MAX_PAIRS, "threads": 0, "applyTo": "sample",
                "replaceKerning": True, "textSize": DEFAULT_SIZE, "view": "side", "overlay": "red",
                "alignWords": True, "keepFit": False, "stayClose": 0.0, "categories": {}}

    def apply_settings(self, d):
        """Sets the controls from a settings dict (settings()) the way a user would: each change goes through
        its control's callback, so it is saved and the preview follows. Connected script, the join mode and
        Along the italic angle only when they change, as each starts the engine again. Keys it does not know
        and values of the wrong kind are left out. Returns (the keys applied, the keys left out)."""
        if not isinstance(d, dict) or SETTINGS_FORMAT not in d:
            raise ValueError('not Kinetikern3 settings (they start with {"%s": %d, ...})'
                             % (SETTINGS_FORMAT, SETTINGS_VERSION))
        w = self.w
        known = set(self.settings())
        applied, ignored = [], [k for k in d if k not in known]

        def number(key, lo, hi):
            if key not in d:
                return None
            v = d[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                ignored.append(key)
                return None
            applied.append(key)
            return min(hi, max(lo, float(v)))

        def flag(key):
            if key not in d:
                return None
            if not isinstance(d[key], bool):
                ignored.append(key)
                return None
            applied.append(key)
            return d[key]

        def choice(key, names):
            if key not in d:
                return None
            v = d[key]
            if isinstance(v, str) and v in names:
                applied.append(key)
                return names.index(v)
            ignored.append(key)
            return None

        loose, inten = number("looseness", TIGHT_MIN, LOOSE_MAX), number("intensity", 0.0, MAX_INTENSITY)
        if loose is not None:
            w.tightness.set(loose)
        if inten is not None:
            w.intensity.set(inten)
        if loose is not None or inten is not None:
            self.physicsChanged(None)
        v = flag("kerning")
        if v is not None:
            w.kerning.set(v)
            self.kerningChanged(None)
        v = number("threshold", 0.0, MAX_THRESHOLD)
        if v is not None:
            w.threshold.set(v)
            self.thresholdChanged(None)
        on, strength = flag("harness"), number("harnessStrength", 0.0, 100.0)
        if on is not None:
            w.harness.set(on)
        if strength is not None:
            w.harnessStrength.set(strength)
        if on is not None or strength is not None:
            self.harnessChanged(None)
        if "conventions" in d:
            if d["conventions"] in [k for k, _label in self.styles]:
                applied.append("conventions")
                self.set_harness_style(d["conventions"])
            else:
                ignored.append("conventions")
        for key, control, callback in (("punctuation", w.punctuation, self.punctuationChanged),
                                       ("figures", w.figures, self.figuresChanged),
                                       ("smallCaps", w.smallCaps, self.smallCapsChanged),
                                       ("replaceKerning", w.replace, self.replaceChanged),
                                       ("alignWords", w.alignWords, self.alignWordsChanged)):
            v = flag(key)
            if v is not None:
                control.set(v)
                callback(None)
        v = number("maxPairs", 0, 1.0e9)
        if v is not None:
            w.maxPairs.set(str(int(v)) if v >= 1 else "")
            self.maxPairsChanged(None)
        v = number("threads", 0, self.engine.cpu_count)
        if v is not None:
            w.threads.set(int(v))
            self.threadsChanged(None)
        v = choice("applyTo", SCOPE_NAMES)
        if v is not None:
            w.scope.set(v)
            self.scopeChanged(None)
        v = number("textSize", MIN_SIZE, MAX_SIZE)
        if v is not None:
            self._set_size(v)
        style, view = choice("overlay", OVERLAY_NAMES), choice("view", VIEW_NAMES)
        if style is not None:
            w.overlayStyle.set(style)
            self._save("overlayStyle", style)
        if view is not None:
            w.viewMode.set(view)
            self._save("view", view)
        if style is not None or view is not None:
            self._apply_view()
        if "categories" in d:
            if isinstance(d["categories"], dict) and all(
                    k in kg.CATEGORY_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool)
                    for k, v in d["categories"].items()):
                applied.append("categories")
                self.set_category_offsets(d["categories"])
            else:
                ignored.append("categories")
        v = flag("keepFit")
        if v is not None:
            self.set_keep_fit(v)
        v = number("stayClose", 0.0, 100.0)
        if v is not None:
            self.set_stay_close(v)
        if self.categories_window is not None:
            self.categories_window.refresh()
        # the ones that start the engine again, only when they change
        connected, mode = flag("connected"), choice("joinMode", JOIN_NAMES)
        restart = False
        if connected is not None and connected != bool(w.connected.get()):
            w.connected.set(connected)
            restart = True
        if mode is not None and mode != int(w.joinMode.get()):
            w.joinMode.set(mode)
            self._save("joinMode", mode)
            restart = True
        if restart:
            self.connectedChanged(None)
        v = flag("alongSlant")
        if v is not None and v != bool(w.alongSlant.get()):
            w.alongSlant.set(v)
            self.alongSlantChanged(None)
        return applied, ignored

    def settings_text(self, indent=None):
        return json.dumps(self.settings(), indent=indent)

    def load_settings_text(self, text, source):
        """Applies settings given as JSON text; the status line says what happened. True when they applied."""
        try:
            applied, ignored = self.apply_settings(json.loads(text or ""))
        except Exception as e:
            self._set_status("No Kinetikern3 settings in %s: %s" % (source, e))
            return False
        note = "Settings from %s: %d applied" % (source, len(applied))
        if ignored:
            note += "; left out (unknown, or not a value it can take): %s" % ", ".join(sorted(set(ignored)))
        self._set_status(note + ".")
        return True

    def _pasteboard(self):
        """The general pasteboard; in a test instance a private one (a test never touches the clipboard)."""
        if self._unattended:
            if self._test_pasteboard is None:
                self._test_pasteboard = NSPasteboard.pasteboardWithUniqueName()
            return self._test_pasteboard
        return NSPasteboard.generalPasteboard()

    def copySettings(self, sender):
        pb = self._pasteboard()
        pb.clearContents()
        pb.setString_forType_(self.settings_text(), NSPasteboardTypeString)
        self._set_status("Settings copied as text (JSON): Paste Settings in any Kinetikern3 window takes them.")

    def pasteSettings(self, sender):
        self.load_settings_text(self._pasteboard().stringForType_(NSPasteboardTypeString), "the clipboard")

    def saveSettings(self, sender):
        vanilla.dialogs.putFile(messageText="Save Kinetikern3's settings", fileName="Kinetikern3 settings.json",
                                fileTypes=["json"], parentWindow=self.w, resultCallback=self.save_settings_to)

    def save_settings_to(self, path):
        path = path[0] if isinstance(path, (list, tuple)) and path else path
        if not path:
            return False
        try:
            with open(path, "w") as f:
                f.write(self.settings_text(indent=2) + "\n")
        except Exception as e:
            self._set_status("The settings could not be saved: %s" % e)
            return False
        self._set_status("Settings saved to %s." % os.path.basename(path))
        return True

    def loadSettings(self, sender):
        vanilla.dialogs.getFile(messageText="Load Kinetikern3's settings", fileTypes=["json"], parentWindow=self.w,
                                resultCallback=self.load_settings_from)

    def load_settings_from(self, path):
        path = path[0] if isinstance(path, (list, tuple)) and path else path
        if not path:
            return False
        try:
            with open(path) as f:
                text = f.read()
        except Exception as e:
            self._set_status("The settings could not be read: %s" % e)
            return False
        return self.load_settings_text(text, os.path.basename(path))

    def resetSettings(self, sender):
        self.apply_settings(self.default_settings())
        self._set_status("Settings back to Kinetikern3's defaults (Paste Settings brings back settings you copied).")

    # ----------------------------------------------------------- callbacks
    def physicsChanged(self, sender):
        self._save("tightness", float(self.w.tightness.get()))
        self._save("intensity", float(self.w.intensity.get()))
        self._show_slider_values()
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
        fit = self._fitted
        if fit is None and self._keep_fit_on():
            fit = self._category_fits.get(kg.FIT_GROUPS["lowercase"])  # the lowercase's fit, as the letters'
        looseness = float(self.w.tightness.get()) + (fit or 0.0)
        opts = self._glyph_opts()
        frozen = frozenset(i for i, o in enumerate(opts or ()) if o[0])
        kept = self._kept_sides()
        style = self._style()
        punct = self._punctuation_on()
        key = (id(snap), self._generation, round(looseness, 4), round(s, 4), self.groups.key(), style, punct,
               id(self.context) if kept else None)
        if self._harness_cache is not None and self._harness_cache[0] == key:
            return self._harness_cache[1]
        try:
            plan = kh.Plan(snap, looseness, s, frozen=frozen, style=style, kept=kept, punctuation=punct)
        except Exception:
            print(traceback.format_exc())
            plan = None
        self._harness_cache = (key, plan)
        return plan

    def model_pair(self, a, b):
        """Kinetikern3's spacing of glyph pair (a, b) without the harness, from
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

    def punctuation_available(self):
        return bool(self.engine.features & kb.FEATURE_PUNCTUATION) and kpu.available()

    def _punctuation_on(self):
        return bool(self.w is not None and self.w.punctuation.get() and self.punctuation_available())

    def _style(self):
        """The conventions the solves follow — the punctuation's, the
        harness's and Pass 2's (how far a capital reaches over its neighbour):
        the Conventions menu's, or for Automatic handwriting's when the master
        is a connected script (its letters join; a line drawn through every
        glyph does not count) or its stems lean without an italic angle, else
        text faces'."""
        if self.harness_style != kh.AUTO:
            return self.harness_style
        script = self.joins is not None and not self.join_decorated
        try:
            leans = self.font is not None and self._leans_undeclared()
        except Exception:
            leans = False
        return "handwriting" if script or leans else "text"

    def _figures_on(self):
        return bool(self.w is not None and getattr(self.w, "figures", None) is not None and self.w.figures.get()
                    and self.punctuation_available() and kpu.has_figures())

    def _small_caps_on(self):
        return bool(self.w is not None and getattr(self.w, "smallCaps", None) is not None and self.w.smallCaps.get()
                    and self.punctuation_available() and kpu.has_small_caps())

    def _punctuation_arg(self, snap=None):
        """Punctuation, figures and small caps by convention for the solves (kk3_punctuation.engine_arg), or None
        when all three are off."""
        snap = self.snapshot if snap is None else snap
        if not (self._punctuation_on() or self._figures_on() or self._small_caps_on()) or snap is None:
            return None
        try:
            return kpu.engine_arg(snap, self._style(), punctuation=self._punctuation_on(), figures=self._figures_on(),
                                  small_caps=self._small_caps_on())
        except Exception:
            print(traceback.format_exc())
            return None

    def punctuationChanged(self, sender):
        self._save("punctuation", bool(self.w.punctuation.get()))
        self._update_labels()
        self._request_preview()
        if self.harness_window is not None:
            self.harness_window.settings_changed()

    def figuresChanged(self, sender):
        self._save("figures", bool(self.w.figures.get()))
        self._update_labels()
        self._request_preview()

    def smallCapsChanged(self, sender):
        self._save("smallCaps", bool(self.w.smallCaps.get()))
        self._update_labels()
        self._request_preview()

    def conventionsChanged(self, sender):
        self.set_harness_style(self.styles[max(0, self.w.conventions.get())][0])

    def kerningChanged(self, sender):
        self._save("kerning", bool(self.w.kerning.get()))
        self._update_controls()
        self._update_labels()
        self._request_preview()

    def tightFieldChanged(self, sender):
        v = _number(self.w.tightField.get())
        if v is None:
            self._show_slider_values()  # not a number: the slider's value again
            return
        self.w.tightness.set(min(LOOSE_MAX, max(TIGHT_MIN, v)))
        self.physicsChanged(sender)

    def intensityFieldChanged(self, sender):
        v = _number(self.w.intensityField.get())
        if v is None:
            self._show_slider_values()
            return
        self.w.intensity.set(min(MAX_INTENSITY, max(0.0, v)))
        self.physicsChanged(sender)

    def harnessStrengthFieldChanged(self, sender):
        v = _number(self.w.harnessStrengthField.get())
        if v is None:
            self._show_slider_values()
            return
        self.w.harnessStrength.set(min(100.0, max(0.0, v)))
        self.harnessChanged(sender)

    def harnessChanged(self, sender):
        self._save("harness", bool(self.w.harness.get()))
        self._save("harnessStrength", float(self.w.harnessStrength.get()))
        self._show_slider_values()
        self._update_labels()
        self._request_preview()
        if self.harness_window is not None:
            self.harness_window.settings_changed()

    def set_harness_style(self, style):
        """Follow the conventions of `style` ("auto", "text", "display",
        "handwriting"): the spacing of that category (_style)."""
        if style == self.harness_style or style not in [k for k, _label in kh.style_choices()]:
            return
        self.harness_style = style
        self._save("harnessStyle", style)
        keys = [k for k, _label in self.styles]
        if self.w is not None and style in keys:
            self.w.conventions.set(keys.index(style))
        self._update_labels()
        self._request_preview()
        if self.harness_window is not None:
            self.harness_window.settings_changed()

    def openHarness(self, sender=None):
        if self.harness_window is not None:
            self.harness_window.w.getNSWindow().makeKeyAndOrderFront_(None)
            return self.harness_window
        import kk3_harness_window
        self.harness_window = kk3_harness_window.HarnessWindow(self)
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

    def _set_size(self, size):
        size = min(MAX_SIZE, max(MIN_SIZE, float(size)))
        if abs(size - self._size) < 1.0e-6:
            self._show_slider_values()
            return
        self._size = size
        self.w.size.set(math.log(size))
        self._show_slider_values()
        self._save("size", "%g" % round(size, 1))
        self._panes_due(left=True, right=True)

    def sizeChanged(self, sender):
        self._set_size(round(math.exp(float(self.w.size.get()))))  # whole points from the slider

    def sizeFieldChanged(self, sender):
        v = _number(self.w.sizeField.get())
        if v is None:
            self._show_slider_values()
            return
        self._set_size(v)

    def _magnify(self, magnification):
        """A pinch on a preview pane."""
        if self.w is not None:
            self._set_size(round(self._size * (1.0 + magnification), 1))

    # ---------------------------------------------------------------- view
    def _view(self):
        if self.w is None or getattr(self.w, "viewMode", None) is None:
            return VIEW_SIDE
        mode = int(self.w.viewMode.get())
        return mode if 0 <= mode < len(VIEWS) else VIEW_SIDE

    def _apply_view(self):
        """Shows the panes the View asks for and lays out what that needs."""
        w = self.w
        mode = self._view()
        toggle, over = mode == VIEW_TOGGLE, mode == VIEW_OVERLAY
        self._show_font_pane(mode == VIEW_SIDE)
        w.showing.show(toggle)
        w.showing.set(0 if self._showing_font else 1)
        w.overlayStyle.show(over)
        w.alignWords.show(over)
        covering = toggle and self._showing_font
        view = self.overlay.view
        view.setFrame_(self.right.view.bounds())
        view.setDrawsBackground_(covering)  # Toggle: the font as it is covers Kinetikern3's pane
        view.setHidden_(not (over or covering))
        click = self.flip if toggle else None
        for pane in (self.right, self.overlay):
            pane.view.kk3_on_click = click
        w.viewHint.set("Click the preview or press the space bar to switch." if toggle else
                       "Where a glyph shows twice, the two spacings differ." if over else "")
        w.viewHint.setPosSize((630 if over else 576, 163, -330, 14))
        self.right_group.caption.set(self._right_caption(self.result))
        if mode == VIEW_SIDE:
            if self._left_stale:
                self._panes_due(left=True)
        else:
            self._panes_due(right=True)  # its colour, then the overlay with its line breaks

    def _show_font_pane(self, show):
        """Shows or hides the left pane, giving its width to Kinetikern3's. vanilla's showPane has its on
        and off the other way round in the Glyphs repositories' vanilla (it reads a view's isHidden as
        "visible"): whichever sense it takes, the pane ends up as asked."""
        view = self.font_pane_view()
        for on_off in (show, not show):
            if bool(view.isHidden()) != bool(show):
                return
            self.proofs.showPane("font", on_off)

    def font_pane_view(self):
        """The split view's subview that holds the left pane (vanilla hides that one, not the group)."""
        inner = self.left_group.getNSView()
        for sub in self.proofs.getNSSplitView().subviews():
            if inner.isDescendantOf_(sub):
                return sub
        return inner

    def viewModeChanged(self, sender):
        self._save("view", self._view())
        self._apply_view()

    def overlayStyleChanged(self, sender):
        self._save("overlayStyle", int(self.w.overlayStyle.get()))
        self._apply_view()

    def alignWordsChanged(self, sender):
        self._save("alignWords", bool(self.w.alignWords.get()))
        self._overlay_due = True
        self._panes_due()

    def showingChanged(self, sender):
        self._showing_font = int(self.w.showing.get()) == 0
        self._apply_view_toggle()

    def flip(self):
        """Toggle: the font as it is, or Kinetikern3 (a click on the preview, the space bar)."""
        self._showing_font = not self._showing_font
        self._apply_view_toggle()

    def _apply_view_toggle(self):
        """What Toggle shows changed: nothing is laid out again, the font's pane covers Kinetikern3's or not."""
        if self.w is None:
            return
        self.w.showing.set(0 if self._showing_font else 1)
        view = self.overlay.view
        view.setDrawsBackground_(self._showing_font)
        view.setHidden_(not self._showing_font)
        self.right_group.caption.set(self._right_caption(self.result))

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
        import kk3_groups_window
        self.groups_window = kk3_groups_window.GroupsWindow(self)
        return self.groups_window

    def openJoins(self, sender=None):
        import kk3_joins_window as kj
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
        import kk3_pairs_window
        self.pairs_window = kk3_pairs_window.PairsWindow(self)
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
            return ("\n%s are spaced at Looseness %+.2f of Kinetikern3's: new glyphs follow them "
                    "(plus the main slider)." % (what[0].upper() + what[1:], self._fitted))
        return ""

    def windowClosed(self, sender):
        for sub in (self.groups_window, self.pairs_window, self.harness_window, self.joins_window,
                    self.categories_window):
            if sub is not None:
                sub.close()
        self.groups_window = self.pairs_window = self.harness_window = self.joins_window = None
        self.categories_window = None
        self._stop_timer()
        if self._timer_target is not None:
            self._timer_target.kk3_callback = None
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
        for pane in (self.right, self.overlay):
            pane.view.kk3_on_click = None
        for pane in (self.left, self.right, self.overlay):
            pane.view.kk3_on_magnify = None
        for pane in (self.left, self.right, self.overlay):
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
    win = KK3Window(resources)
    if getattr(win, "w", None) is not None:
        _open_windows.append(win)
    return win
