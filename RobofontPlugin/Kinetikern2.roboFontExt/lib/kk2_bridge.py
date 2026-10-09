# encoding: utf-8
"""
kk2_bridge — ctypes bridge to libkinetikern2.dylib.

The engine runs everything as jobs on threads it owns. Python starts a job,
polls it (a few atomic loads, never blocking), and takes the output when it is
done. Inputs are packed into flat buffers once; results are read in place
(zero-copy views over the library's arrays) and never turned into per-pair
Python objects unless asked for.

No GlyphsApp import here: the same module drives the engine from tools.
"""

from __future__ import division, print_function, unicode_literals

import ctypes
import os
import struct
from ctypes import (POINTER, Structure, byref, c_char_p, c_double, c_float, c_uint8, c_uint32, c_uint64, c_void_p,
                    sizeof)

ABI_VERSION = 1
DYLIB_NAME = "libkinetikern2.dylib"
NONE = 0xFFFFFFFF
EXISTING = 1 << 31
# class origin of a frozen glyph without a kerning group: written with the
# glyph's name, never given a group (low bits: the glyph index)
GLYPH_KEYED = 1 << 30

NODE_KINDS = {"line": 0, "curve": 1, "offcurve": 2, "qcurve": 3}

GROUP_OTHER, GROUP_UPPERCASE, GROUP_LOWERCASE, GROUP_FIGURES, GROUP_SMALLCAPS = 0, 1, 2, 3, 4

GLYPH_FIXED_ADVANCE = 1
GLYPH_KERN = 2
GLYPH_RTL = 4
GLYPH_LEFT_KEY = 8
GLYPH_RIGHT_KEY = 16

RULE_FREE, RULE_FIXED, RULE_FOLLOW_SAME, RULE_FOLLOW_OPPOSITE = 0, 1, 2, 3

PARAM_SKIP_PASS2 = 1
PARAM_CLASSES = 2
PARAM_WINDOW = 4
PARAM_SCOPE_SCRIPTS = 8
PARAM_FIT_FROZEN = 16

GLYPHOPT_FROZEN = 1

# kk2_features() bits
FEATURE_GLYPH_OPTS = 1
FEATURE_MEASURE = 2
FEATURE_HARNESS = 4
FEATURE_JOINS = 8

# kk2_detect_joins: what a glyph is to the joins (only letters join; the
# lowercase letters are the partners whose overlaps are counted) and what
# makes a join
JOINKIND_OTHER, JOINKIND_UPPER, JOINKIND_LOWER = 0, 1, 2
JOINRULE_BOTH, JOINRULE_OVERHANG, JOINRULE_OVERLAPS = 0, 1, 2

STATE_RUNNING, STATE_DONE, STATE_FAILED, STATE_CANCELLED = 0, 1, 2, 3
PHASE_NAMES = {1: "Analyzing SDFs", 2: "Evaluating pairs", 3: "Grouping & pruning"}

ENTRY_GLYPH_GLYPH, ENTRY_CLASS_CLASS, ENTRY_GLYPH_CLASS, ENTRY_CLASS_GLYPH = 0, 1, 2, 3


class KK2Point(Structure):
    _fields_ = [("x", c_double), ("y", c_double), ("kind", c_uint32), ("reserved", c_uint32)]


class KK2Glyph(Structure):
    _fields_ = [
        ("points", c_void_p), ("contour_lengths", c_void_p),
        ("point_count", c_uint32), ("contour_count", c_uint32),
        ("advance", c_double),
        ("rhythm_group", c_uint32), ("flags", c_uint32),
        ("script", c_uint32), ("base", c_uint32),
        ("left_group", c_uint32), ("right_group", c_uint32),
        ("lsb_rule", c_uint32), ("rsb_rule", c_uint32),
        ("lsb_glyph", c_uint32), ("rsb_glyph", c_uint32),
        ("lsb_value", c_double), ("rsb_value", c_double),
        ("cur_lsb", c_double), ("cur_rsb", c_double),
    ]


class KK2Params(Structure):
    _fields_ = [("struct_size", c_uint32), ("flags", c_uint32)] + [(n, c_double) for n in (
        "spring", "repulsion", "coupling", "white_credit", "depth_ratio", "rhythm_weight", "width_coupling",
        "min_clearance", "crevice_pressure", "max_negative_kern", "max_positive_kern", "min_kern", "field_cap",
        "field_decay", "field_core", "threshold", "radius_ratio")] + [("budget", c_uint32), ("threads", c_uint32)]


class KK2Progress(Structure):
    _fields_ = [("state", c_uint32), ("phase", c_uint32), ("phases", c_uint32), ("reserved", c_uint32),
                ("done", c_uint64), ("total", c_uint64), ("elapsed", c_double)]


class KK2Metrics(Structure):
    _fields_ = [(n, c_double) for n in ("lsb", "rsb", "advance", "x_min", "x_max", "y_min", "y_max")] + [
        ("valid", c_uint32), ("flags", c_uint32)]


class KK2Entry(Structure):
    _fields_ = [("left", c_uint32), ("right", c_uint32), ("value", c_float), ("importance", c_float),
                ("kind", c_uint32), ("reserved", c_uint32)]


STAT_COUNTERS = ("kern_glyphs", "probe_glyphs", "pairs_in_scope", "class_pairs", "member_pairs", "inherited",
                 "verified", "verified_within", "solved", "force_evaluations", "merged_rays", "active_rays",
                 "clearance_hits", "crevice_hits", "saturated", "window_hits", "bound_hits", "fallbacks", "entries_before_budget",
                 "class_entries", "exception_entries", "dropped_by_budget")


class KK2Stats(Structure):
    _fields_ = [(n, c_double) for n in ("prep_ms", "pass1_ms", "pass2_ms", "prune_ms", "pass1_residual")] + [
        ("pass1_iterations", c_uint32), ("threads", c_uint32)] + [(n, c_uint64) for n in STAT_COUNTERS] + [
        ("rest_gap", c_double * 8), ("rhythm_scale", c_double * 8)]


class KK2Result(Structure):
    _fields_ = [(n, c_uint32) for n in ("abi_version", "glyph_count", "entry_count", "mode", "right_class_count",
                                        "left_class_count", "reserved0", "reserved1")] + [
        ("metrics", c_void_p), ("entries", c_void_p), ("glyph_right_class", c_void_p),
        ("glyph_left_class", c_void_p), ("right_class_origin", c_void_p), ("left_class_origin", c_void_p),
        ("right_class_rep", c_void_p), ("left_class_rep", c_void_p), ("kern_mask", c_void_p),
        ("reserved_ptr", c_void_p), ("stats", KK2Stats)]


class KK2Ray(Structure):
    _fields_ = [("y", c_double), ("x", c_double), ("tier", c_uint32), ("reserved", c_uint32)]


class KK2Harness(Structure):
    _fields_ = [("struct_size", c_uint32), ("glyph_count", c_uint32), ("sides", c_void_p), ("pairs", c_void_p),
                ("pair_count", c_uint32), ("reserved", c_uint32)]


class KK2HarnessPair(Structure):
    _fields_ = [("left", c_uint32), ("right", c_uint32), ("value", c_double)]


class KK2GlyphOpt(Structure):
    """Per-solve options of one glyph: its spacing group."""
    _fields_ = [("flags", c_uint32), ("reserved", c_uint32), ("looseness", c_double), ("intensity", c_double)]


class KK2KernIn(Structure):
    """One entry of the font's current kerning (class sides are group ids)."""
    _fields_ = [("kind", c_uint32), ("left", c_uint32), ("right", c_uint32), ("value", c_float)]


class KK2Joins(Structure):
    """A glyph's join bands in a connected script (font units; NaN = none)."""
    _fields_ = [(n, c_double) for n in ("left_y0", "left_y1", "right_y0", "right_y1")]


class KK2PairOut(Structure):
    _fields_ = [("left", c_uint32), ("right", c_uint32), ("current", c_float), ("model", c_float),
                ("residual", c_float), ("reserved", c_uint32)]


class KK2MeasureStats(Structure):
    _fields_ = [("pairs", c_uint64), ("offset", c_double), ("mae", c_double), ("rms", c_double),
                ("loosest_count", c_uint32), ("tightest_count", c_uint32)]


_SIZES = {KK2Point: 24, KK2Glyph: 104, KK2Params: 152, KK2Progress: 40, KK2Metrics: 64, KK2Entry: 24,
          KK2Stats: 352, KK2Result: 464, KK2Ray: 24, KK2GlyphOpt: 24, KK2KernIn: 16, KK2PairOut: 24,
          KK2MeasureStats: 40, KK2Harness: 32, KK2HarnessPair: 16, KK2Joins: 32}
_POINT = struct.Struct("<ddII")
_ENTRY = struct.Struct("<IIffII")


class EngineError(RuntimeError):
    pass


def script_code(script):
    """ISO 15924 code (or a Glyphs script name) → packed u32; 0 for Common."""
    if not script:
        return 0
    s = SCRIPT_NAMES.get(str(script).lower(), str(script))
    s = (s + "    ")[:4]
    if s.lower() in ("zyyy", "zinh", "zzzz"):
        return 0
    b = s.encode("ascii", "replace")
    return (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]


# Glyphs' script names → ISO 15924
SCRIPT_NAMES = {
    "latin": "Latn", "greek": "Grek", "cyrillic": "Cyrl", "arabic": "Arab", "hebrew": "Hebr", "armenian": "Armn",
    "georgian": "Geor", "thai": "Thai", "devanagari": "Deva", "bengali": "Beng", "coptic": "Copt",
    "syriac": "Syrc", "thaana": "Thaa", "nko": "Nkoo", "ethiopic": "Ethi", "cherokee": "Cher", "khmer": "Khmr",
    "lao": "Laoo", "tibetan": "Tibt", "gujarati": "Gujr", "gurmukhi": "Guru", "tamil": "Taml", "telugu": "Telu",
    "kannada": "Knda", "malayalam": "Mlym", "sinhala": "Sinh", "myanmar": "Mymr", "hangul": "Hang",
    "han": "Hani", "kana": "Kana", "bopomofo": "Bopo", "mongolian": "Mong", "tifinagh": "Tfng",
}
RTL_SCRIPTS = {"Arab", "Hebr", "Syrc", "Thaa", "Nkoo", "Samr", "Mand", "Adlm", "Rohg"}


class GlyphSpec(object):
    """One glyph's engine input."""
    __slots__ = ("name", "contours", "advance", "group", "flags", "script", "base", "left_group", "right_group",
                 "lsb_rule", "rsb_rule", "lsb_glyph", "rsb_glyph", "lsb_value", "rsb_value", "cur_lsb", "cur_rsb")

    def __init__(self, name, contours, advance, group=GROUP_OTHER, flags=GLYPH_KERN, script=0, base=NONE,
                 left_group=NONE, right_group=NONE):
        self.name = name
        self.contours = contours  # [[(x, y, kind), ...], ...]
        self.advance = float(advance)
        self.group = int(group)
        self.flags = int(flags)
        self.script = int(script)
        self.base = int(base)
        self.left_group = int(left_group)
        self.right_group = int(right_group)
        self.lsb_rule = self.rsb_rule = RULE_FREE
        self.lsb_glyph = self.rsb_glyph = NONE
        self.lsb_value = self.rsb_value = float("nan")
        self.cur_lsb = self.cur_rsb = float("nan")


class InputPacker(object):
    """Packs glyphs into the flat buffers the engine copies at job start.
    Glyphs can be added a few at a time (the plugin packs while it reads)."""

    def __init__(self):
        self.points = bytearray()
        self.lengths = []
        self.glyphs = []  # (point offset, point count, length offset, contour count, spec)

    def add(self, spec):
        p0 = len(self.points) // _POINT.size
        l0 = len(self.lengths)
        n = 0
        for contour in spec.contours:
            for (x, y, kind) in contour:
                x, y = float(x), float(y)
                if x != x or y != y or abs(x) == float("inf") or abs(y) == float("inf"):
                    x = y = float("nan")
                self.points += _POINT.pack(x, y, int(kind), 0)
            self.lengths.append(len(contour))
            n += len(contour)
        self.glyphs.append((p0, n, l0, len(spec.contours), spec))
        return len(self.glyphs) - 1

    def __len__(self):
        return len(self.glyphs)

    def build(self):
        """ctypes arrays (kept alive by the returned tuple)."""
        npts = len(self.points) // _POINT.size
        pts = (KK2Point * max(npts, 1)).from_buffer_copy(bytes(self.points) or bytes(_POINT.size))
        lens = (c_uint32 * max(len(self.lengths), 1))(*self.lengths) if self.lengths else (c_uint32 * 1)()
        arr = (KK2Glyph * max(len(self.glyphs), 1))()
        base_p = ctypes.addressof(pts)
        base_l = ctypes.addressof(lens)
        for i, (p0, n, l0, nc, s) in enumerate(self.glyphs):
            g = arr[i]
            g.points = base_p + p0 * _POINT.size if n else None
            g.contour_lengths = base_l + l0 * 4 if nc else None
            g.point_count = n
            g.contour_count = nc
            g.advance = s.advance
            g.rhythm_group = s.group
            g.flags = s.flags
            g.script = s.script
            g.base = s.base
            g.left_group = s.left_group
            g.right_group = s.right_group
            g.lsb_rule = s.lsb_rule
            g.rsb_rule = s.rsb_rule
            g.lsb_glyph = s.lsb_glyph
            g.rsb_glyph = s.rsb_glyph
            g.lsb_value = s.lsb_value
            g.rsb_value = s.rsb_value
            g.cur_lsb = s.cur_lsb
            g.cur_rsb = s.cur_rsb
        return arr, len(self.glyphs), (pts, lens)


def pack_glyph_opts(opts):
    """[(frozen, looseness offset, intensity multiplier)] → a KK2GlyphOpt array."""
    arr = (KK2GlyphOpt * max(len(opts), 1))()
    for k, o in enumerate(opts):
        frozen, loose, inten = (tuple(o) + (False, 0.0, 1.0))[:3]
        arr[k].flags = GLYPHOPT_FROZEN if frozen else 0
        arr[k].looseness = float(loose or 0.0)
        arr[k].intensity = 1.0 if inten is None else float(inten)
    return arr


def pack_kern_in(current):
    """[(kind, left, right, value)] (group ids on class sides) → a KK2KernIn array."""
    arr = (KK2KernIn * max(len(current), 1))()
    for k, (kind, left, right, value) in enumerate(current):
        arr[k].kind, arr[k].left, arr[k].right, arr[k].value = int(kind), int(left), int(right), float(value)
    return arr


def pack_joins(joins, n):
    """[((left y0, y1) or None, (right y0, y1) or None)] → a KK2Joins array of n."""
    nan = float("nan")
    arr = (KK2Joins * max(n, 1))()
    for k in range(n):
        left, right = joins[k] if k < len(joins) and joins[k] else (None, None)
        arr[k].left_y0, arr[k].left_y1 = (float(left[0]), float(left[1])) if left else (nan, nan)
        arr[k].right_y0, arr[k].right_y1 = (float(right[0]), float(right[1])) if right else (nan, nan)
    return arr


def make_params(spring=1.0, repulsion=3.86, coupling=1.0, classes=True, window=True, scope_scripts=True,
                threshold=0.5, budget=0, threads=0, radius_ratio=-1.0, skip_pass2=False, fit_frozen=False, **tuning):
    p = KK2Params()
    p.struct_size = sizeof(KK2Params)
    p.flags = ((PARAM_CLASSES if classes else 0) | (PARAM_WINDOW if window else 0) |
               (PARAM_SCOPE_SCRIPTS if scope_scripts else 0) | (PARAM_SKIP_PASS2 if skip_pass2 else 0) |
               (PARAM_FIT_FROZEN if fit_frozen else 0))
    p.spring, p.repulsion, p.coupling = float(spring), float(repulsion), float(coupling)
    for name in ("white_credit", "depth_ratio", "rhythm_weight", "width_coupling", "min_clearance",
                 "crevice_pressure", "max_negative_kern", "max_positive_kern", "min_kern", "field_cap",
                 "field_decay", "field_core"):
        v = tuning.get(name)
        setattr(p, name, -1.0 if v is None else float(v))
    p.threshold = float(threshold)
    p.radius_ratio = float(radius_ratio)
    p.budget = int(budget)
    p.threads = int(threads)
    return p


class Engine(object):
    """The loaded library."""

    def __init__(self, path):
        for cls, size in _SIZES.items():
            if sizeof(cls) != size:
                raise EngineError("struct %s is %d bytes, expected %d" % (cls.__name__, sizeof(cls), size))
        if not os.path.exists(path):
            raise EngineError("engine library not found: %s" % path)
        lib = ctypes.CDLL(path)
        sig = {
            "kk2_abi_version": ([], c_uint32),
            "kk2_engine_version": ([], c_char_p),
            "kk2_last_error": ([], c_char_p),
            "kk2_cpu_count": ([], c_uint32),
            "kk2_default_threads": ([], c_uint32),
            "kk2_prepare_start": ([c_void_p, c_uint32, c_double, c_uint32], c_void_p),
            "kk2_solve_start": ([c_void_p, POINTER(KK2Params), c_void_p, c_uint32], c_void_p),
            "kk2_job_poll": ([c_void_p, POINTER(KK2Progress)], c_uint32),
            "kk2_job_cancel": ([c_void_p], None),
            "kk2_job_wait": ([c_void_p, c_double], c_uint32),
            "kk2_job_error": ([c_void_p], c_char_p),
            "kk2_job_take_context": ([c_void_p], c_void_p),
            "kk2_job_take_result": ([c_void_p], c_void_p),
            "kk2_job_free": ([c_void_p], None),
            "kk2_context_free": ([c_void_p], None),
            "kk2_context_glyph_count": ([c_void_p], c_uint32),
            "kk2_context_prep_ms": ([c_void_p], c_double),
            "kk2_context_rays": ([c_void_p, c_uint32, c_uint32, POINTER(KK2Ray), c_uint32], c_uint32),
            "kk2_result_free": ([c_void_p], None),
            "kk2_result_value": ([c_void_p, c_uint32, c_uint32], c_float),
            "kk2_result_values": ([c_void_p, c_void_p, c_void_p, c_uint32, c_void_p], c_uint32),
        }
        for name, (args, res) in sig.items():
            fn = getattr(lib, name)
            fn.argtypes = args
            fn.restype = res
        # optional (newer engines): spacing groups, the frozen-glyph fit, measuring
        optional = {
            "kk2_features": ([], c_uint32),
            "kk2_solve_start2": ([c_void_p, POINTER(KK2Params), c_void_p, c_uint32, c_void_p, c_uint32], c_void_p),
            "kk2_solve_start3": ([c_void_p, POINTER(KK2Params), c_void_p, c_uint32, c_void_p, c_uint32,
                                  POINTER(KK2Harness)], c_void_p),
            "kk2_result_fitted_looseness": ([c_void_p], c_double),
            "kk2_fit_looseness": ([c_void_p, POINTER(KK2Params), c_void_p, c_uint32], c_double),
            "kk2_measure": ([c_void_p, c_void_p, c_void_p, c_uint32, c_void_p, c_uint32, c_uint32, c_void_p, c_void_p,
                             c_uint32, POINTER(KK2MeasureStats)], c_uint64),
            "kk2_prepare_start2": ([c_void_p, c_uint32, c_double, c_uint32, c_void_p, c_uint32], c_void_p),
            "kk2_detect_joins": ([c_void_p, c_uint32, c_double, c_double, c_void_p, c_void_p, c_uint32, c_uint32,
                                  c_void_p], c_uint32),
        }
        self.features = 0
        for name, (args, res) in optional.items():
            fn = getattr(lib, name, None)
            if fn is not None:
                fn.argtypes = args
                fn.restype = res
        if getattr(lib, "kk2_features", None) is not None:
            self.features = lib.kk2_features()
        if lib.kk2_abi_version() != ABI_VERSION:
            raise EngineError("engine ABI %d, plugin expects %d" % (lib.kk2_abi_version(), ABI_VERSION))
        self.lib = lib
        self.path = path
        self.version = lib.kk2_engine_version().decode("utf-8", "replace")
        self.cpu_count = lib.kk2_cpu_count()
        self.default_threads = lib.kk2_default_threads()

    def last_error(self):
        return (self.lib.kk2_last_error() or b"").decode("utf-8", "replace")

    def prepare(self, packer, units_per_em, threads=0, joins=None):
        """Starts Phase 1. Returns a Job whose output is a Context. `joins`:
        a connected script's join bands (detect_joins), or None."""
        arr, n, keep = packer.build()
        if joins is not None:
            if not self.features & FEATURE_JOINS:
                raise EngineError("this engine build has no connected-script mode (kk2_prepare_start2)")
            jarr = pack_joins(joins, n)
            ptr = self.lib.kk2_prepare_start2(ctypes.addressof(arr), n, float(units_per_em), int(threads),
                                              ctypes.addressof(jarr), sizeof(KK2Joins))
            del jarr
        else:
            ptr = self.lib.kk2_prepare_start(ctypes.addressof(arr), n, float(units_per_em), int(threads))
        del keep, arr  # the engine copied the input
        if not ptr:
            raise EngineError(self.last_error())
        return Job(self, ptr, "prepare", names=[g[4].name for g in packer.glyphs])

    def detect_joins(self, packer, units_per_em, x_height, kinds, current=(), rule=JOINRULE_BOTH):
        """A connected script's joins, found in the font's own spacing: one
        (left band, right band) per glyph, each (y0, y1) in font units or
        None — all None when the font is not a connected script (text faces
        never are). `kinds`: one JOINKIND_* per glyph; `current`: the font's
        kerning as measure() takes it (JOINRULE_OVERHANG does not use it).
        Synchronous (milliseconds for a script font)."""
        if not self.features & FEATURE_JOINS:
            raise EngineError("this engine build has no connected-script mode (kk2_detect_joins)")
        arr, n, keep = packer.build()
        kinds_buf = (c_uint8 * max(n, 1)).from_buffer_copy(bytes(bytearray(kinds[:n])).ljust(max(n, 1), b"\0"))
        cur = pack_kern_in(current)
        out = (KK2Joins * max(n, 1))()
        count = self.lib.kk2_detect_joins(ctypes.addressof(arr), n, float(units_per_em), float(x_height),
                                          ctypes.addressof(kinds_buf), ctypes.addressof(cur), len(current), int(rule),
                                          ctypes.addressof(out))
        del keep, arr
        if count == 0xFFFFFFFF:
            raise EngineError(self.last_error())
        band = lambda y0, y1: (y0, y1) if y0 == y0 and y1 == y1 else None
        return [(band(o.left_y0, o.left_y1), band(o.right_y0, o.right_y1)) for o in out[:n]]

    def solve(self, context, params, kern_mask=None, glyph_opts=None, harness=None):
        """Starts Phases 2–3. `kern_mask`: bytes (one per glyph) or None.
        `glyph_opts`: one (frozen, looseness offset, intensity multiplier)
        per glyph (spacing groups), or None. `harness`: (sides, pairs) of the
        designer harness (kk2_harness.Plan.engine_arg), or None."""
        if context.ptr is None:
            raise EngineError("context is closed")
        mask_ptr, mask_len, keep = None, 0, None
        if kern_mask is not None:
            keep = (c_uint8 * len(kern_mask)).from_buffer_copy(bytes(kern_mask))
            mask_ptr, mask_len = ctypes.addressof(keep), len(kern_mask)
        if harness is not None:
            if not self.features & FEATURE_HARNESS:
                raise EngineError("this engine build has no designer harness (kk2_solve_start3)")
            sides, pairs = harness
            side_arr = (c_double * max(2 * len(sides), 1))()
            for k, (dl, dr) in enumerate(sides):
                side_arr[2 * k] = float(dl)
                side_arr[2 * k + 1] = float(dr)
            pair_arr = (KK2HarnessPair * max(len(pairs), 1))()
            for k, (a, b, v) in enumerate(pairs):
                pair_arr[k].left, pair_arr[k].right, pair_arr[k].value = int(a), int(b), float(v)
            h = KK2Harness()
            h.struct_size = sizeof(KK2Harness)
            h.glyph_count = len(sides)
            h.sides = ctypes.addressof(side_arr) if sides else None
            h.pairs = ctypes.addressof(pair_arr) if pairs else None
            h.pair_count = len(pairs)
            opts = pack_glyph_opts(glyph_opts) if glyph_opts is not None else None
            ptr = self.lib.kk2_solve_start3(context.ptr, byref(params), mask_ptr, mask_len,
                                            ctypes.addressof(opts) if opts is not None else None,
                                            len(glyph_opts) if glyph_opts is not None else 0, byref(h))
            del side_arr, pair_arr, opts  # the engine copied them
        elif glyph_opts is not None:
            if not self.features & FEATURE_GLYPH_OPTS:
                raise EngineError("this engine build has no spacing groups (kk2_solve_start2)")
            opts = pack_glyph_opts(glyph_opts)
            ptr = self.lib.kk2_solve_start2(context.ptr, byref(params), mask_ptr, mask_len, ctypes.addressof(opts),
                                            len(glyph_opts))
        else:
            ptr = self.lib.kk2_solve_start(context.ptr, byref(params), mask_ptr, mask_len)
        del keep
        if not ptr:
            raise EngineError(self.last_error())
        return Job(self, ptr, "solve", names=context.names)

    def fit_looseness(self, context, params, which=None):
        """Looseness offset (slider units, relative to `params`) at which Pass 1
        gives the glyphs flagged in `which` (bytes/list of 0/1; None = all)
        the sidebearings they have now; None if it cannot fit. Synchronous
        (Pass 1 only: milliseconds)."""
        if not self.features & FEATURE_GLYPH_OPTS:
            return None
        if which is None:
            v = self.lib.kk2_fit_looseness(context.ptr, byref(params), None, 0)
        else:
            buf = (c_uint8 * len(which)).from_buffer_copy(bytes(bytearray(1 if x else 0 for x in which)))
            v = self.lib.kk2_fit_looseness(context.ptr, byref(params), ctypes.addressof(buf), len(which))
        return None if v != v else float(v)

    def measure(self, context, result, current, mask=None, scope_scripts=True, cap=100):
        """The font's spacing as it is against `result`: for every pair of
        glyphs in `mask` (None = the glyphs the solve kerned), the visible gap
        now (current sidebearings + `current` kerning: (kind, left, right,
        value) with group ids on class sides) and the model's. Returns
        (stats dict, loosest, tightest) with (left, right, current, model,
        residual) tuples, most extreme first; residual = current − model −
        offset (positive: looser than the font's own rhythm). Synchronous;
        call it off the main thread for whole fonts."""
        if not self.features & FEATURE_MEASURE:
            raise EngineError("this engine build cannot measure (kk2_measure)")
        n = len(current)
        cur = pack_kern_in(current)
        if mask is not None:
            mbuf = (c_uint8 * len(mask)).from_buffer_copy(bytes(bytearray(1 if x else 0 for x in mask)))
            mptr, mlen = ctypes.addressof(mbuf), len(mask)
        else:
            mptr, mlen = None, 0
        loose = (KK2PairOut * max(cap, 1))()
        tight = (KK2PairOut * max(cap, 1))()
        st = KK2MeasureStats()
        self.lib.kk2_measure(context.ptr, result.ptr, ctypes.addressof(cur), n, mptr, mlen, 1 if scope_scripts else 0,
                             ctypes.addressof(loose), ctypes.addressof(tight), int(cap), byref(st))
        row = lambda p: (p.left, p.right, p.current, p.model, p.residual)
        stats = {"pairs": st.pairs, "offset": st.offset, "mae": st.mae, "rms": st.rms}
        return (stats, [row(loose[k]) for k in range(st.loosest_count)],
                [row(tight[k]) for k in range(st.tightest_count)])


class Job(object):
    """A running engine job. Poll it; never blocks (except wait())."""

    def __init__(self, engine, ptr, kind, names=None):
        self.engine = engine
        self.ptr = ptr
        self.kind = kind
        self.names = names
        self._progress = KK2Progress()

    def poll(self):
        """(state, phase, phases, fraction, elapsed seconds)."""
        if not self.ptr:
            return (STATE_CANCELLED, 0, 0, 0.0, 0.0)
        p = self._progress
        state = self.engine.lib.kk2_job_poll(self.ptr, byref(p))
        frac = min(1.0, p.done / float(p.total)) if p.total else 0.0
        return (state, p.phase, p.phases, frac, p.elapsed)

    def cancel(self):
        if self.ptr:
            self.engine.lib.kk2_job_cancel(self.ptr)

    def wait(self, timeout=0.0):
        return self.engine.lib.kk2_job_wait(self.ptr, float(timeout)) if self.ptr else STATE_CANCELLED

    def error(self):
        if not self.ptr:
            return ""
        return (self.engine.lib.kk2_job_error(self.ptr) or b"").decode("utf-8", "replace")

    def take(self):
        """The Context (prepare) or Result (solve) of a finished job."""
        lib = self.engine.lib
        if self.kind == "prepare":
            ptr = lib.kk2_job_take_context(self.ptr)
            if not ptr:
                raise EngineError(self.error() or self.engine.last_error())
            return Context(self.engine, ptr, self.names)
        ptr = lib.kk2_job_take_result(self.ptr)
        if not ptr:
            raise EngineError(self.error() or self.engine.last_error())
        return Result(self.engine, ptr, self.names)

    def free(self):
        if self.ptr:
            self.engine.lib.kk2_job_free(self.ptr)  # cancels a running job; never blocks
            self.ptr = None

    def __del__(self):
        try:
            self.free()
        except Exception:
            pass


class Context(object):
    def __init__(self, engine, ptr, names):
        self.engine = engine
        self.ptr = ptr
        self.names = names or []
        self.index = dict((n, i) for i, n in enumerate(self.names))
        self.prep_ms = engine.lib.kk2_context_prep_ms(ptr)

    def rays(self, glyph, side):
        lib = self.engine.lib
        n = lib.kk2_context_rays(self.ptr, glyph, side, None, 0)
        buf = (KK2Ray * max(n, 1))()
        lib.kk2_context_rays(self.ptr, glyph, side, buf, n)
        return [(buf[k].y, buf[k].x, buf[k].tier) for k in range(n)]

    def close(self):
        if self.ptr:
            self.engine.lib.kk2_context_free(self.ptr)
            self.ptr = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _view(ptr, ctype, n):
    """A zero-copy ctypes array over library memory."""
    if not ptr or n <= 0:
        return (ctype * 0)()
    return (ctype * n).from_address(ptr)


class Result(object):
    """A solve's output, read in place. Keep the Result alive while any view
    is in use; close() frees the library memory."""

    def __init__(self, engine, ptr, names):
        self.engine = engine
        self.ptr = ptr
        self.names = names or []
        r = KK2Result.from_address(ptr)
        self._r = r
        self.glyph_count = r.glyph_count
        self.entry_count = r.entry_count
        self.classes = bool(r.mode)
        self.right_class_count = r.right_class_count
        self.left_class_count = r.left_class_count
        self.metrics = _view(r.metrics, KK2Metrics, r.glyph_count)
        self.entries_raw = _view(r.entries, KK2Entry, r.entry_count)
        self.glyph_right_class = _view(r.glyph_right_class, c_uint32, r.glyph_count)
        self.glyph_left_class = _view(r.glyph_left_class, c_uint32, r.glyph_count)
        self.right_class_origin = _view(r.right_class_origin, c_uint32, r.right_class_count)
        self.left_class_origin = _view(r.left_class_origin, c_uint32, r.left_class_count)
        self.right_class_rep = _view(r.right_class_rep, c_uint32, r.right_class_count)
        self.left_class_rep = _view(r.left_class_rep, c_uint32, r.left_class_count)
        self.kern_mask = _view(r.kern_mask, c_uint8, r.glyph_count)
        s = r.stats
        self.stats = dict((n, getattr(s, n)) for n, _ in KK2Stats._fields_ if n not in ("rest_gap", "rhythm_scale"))
        self.stats["rest_gap"] = list(s.rest_gap)
        self.stats["rhythm_scale"] = list(s.rhythm_scale)

    @property
    def fitted_looseness(self):
        """Looseness offset the solve moved to (PARAM_FIT_FROZEN), else None."""
        fn = getattr(self.engine.lib, "kk2_result_fitted_looseness", None)
        if fn is None or not self.ptr:
            return None
        v = fn(self.ptr)
        return None if v != v else float(v)

    def iter_entries(self):
        """(kind, left, right, value, importance) tuples, unpacked in C."""
        if not self.entry_count:
            return iter(())
        mv = memoryview(self.entries_raw).cast("B")
        return ((k, l, r, v, imp) for (l, r, v, imp, k, _x) in _ENTRY.iter_unpack(mv))

    def value(self, left, right):
        """Kerning of glyph indices (left, right) with entry precedence; NaN
        if either glyph was not kerned in this solve."""
        return self.engine.lib.kk2_result_value(self.ptr, left, right)

    def values(self, lefts, rights):
        """value() of many pairs in one call. A closed result raises (it
        must not read as "no kerning anywhere")."""
        if not self.ptr:
            raise EngineError("the result is closed")
        n = len(lefts)
        if len(rights) != n:
            raise ValueError("%d left glyphs, %d right glyphs" % (n, len(rights)))
        a = (c_uint32 * n)(*lefts)
        b = (c_uint32 * n)(*rights)
        out = (c_float * n)()
        if self.engine.lib.kk2_result_values(self.ptr, a, b, n, out) != n and n:
            raise EngineError("kk2_result_values answered fewer pairs than asked")
        return list(out)

    def close(self):
        if self.ptr:
            # drop every view into library memory before freeing it
            for name in ("metrics", "entries_raw", "glyph_right_class", "glyph_left_class", "right_class_origin",
                         "left_class_origin", "right_class_rep", "left_class_rep", "kern_mask", "_r"):
                setattr(self, name, None)
            self.engine.lib.kk2_result_free(self.ptr)
            self.ptr = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
