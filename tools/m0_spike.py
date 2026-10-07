# encoding: utf-8
"""
M0 — facts about the Glyphs 3 API that Kinetikern2's Apply and snapshot rely on.

Runs inside a temporary Glyphs 3 instance (never the user's):

    open -n -a "Glyphs 3" --args -ApplePersistenceIgnoreState YES \
        -com.mirkovelimirovic.Kinetikern2.devScript  $PWD/Kinetikern2/tools/m0_spike.py \
        -com.mirkovelimirovic.Kinetikern2.devOut     /tmp/m0.json \
        -com.mirkovelimirovic.Kinetikern2.devFont    /System/Library/Fonts/Supplemental/Arial.ttf

Imports the font without a window, measures outline reading, kerning writes
(Python wrapper, direct Objective-C, bulk dictionary), group writes, undo and
metrics propagation, writes the facts as JSON and quits.
"""

import json
import random
import time
import traceback

from AppKit import NSApp, NSMutableDictionary
from GlyphsApp import LTR, RTL, Glyphs

PREFIX = "com.mirkovelimirovic.Kinetikern2."
out_path = Glyphs.defaults[PREFIX + "devOut"]
font_path = Glyphs.defaults[PREFIX + "devFont"]
facts = {"errors": []}


def step(name):
    def wrap(fn):
        t = time.time()
        try:
            facts[name] = fn()
        except Exception:
            facts["errors"].append("%s: %s" % (name, traceback.format_exc()))
        facts.setdefault("seconds", {})[name] = round(time.time() - t, 4)
        return fn
    return wrap


font = Glyphs.open(font_path, showInterface=False)
master = font.masters[0]
mid = master.id
glyphs = [g for g in font.glyphs if g.export]
facts["font"] = {"glyphs": len(font.glyphs), "export": len(glyphs), "upm": font.upm}


@step("read_bezierpath")
def _():
    t = time.time()
    n_el = 0
    for g in glyphs:
        layer = g.layers[mid]
        path = layer.completeBezierPath
        if path is None:
            continue
        for i in range(path.elementCount()):
            el, pts = path.elementAtIndex_associatedPoints_(i)
            n_el += 1
    return {"elements": n_el, "s": round(time.time() - t, 4)}


@step("read_decomposed_nodes")
def _():
    t = time.time()
    n = 0
    for g in glyphs:
        layer = g.layers[mid]
        dl = layer.copyDecomposedLayer() if layer.components else layer
        for p in dl.paths:
            for node in p.nodes:
                pos = node.position
                n += 1
                _k = node.type
    return {"nodes": n, "s": round(time.time() - t, 4)}


@step("read_properties")
def _():
    t = time.time()
    rows = 0
    for g in glyphs:
        layer = g.layers[mid]
        _ = (g.name, g.id, g.unicode, g.category, g.subCategory, g.script, g.leftKerningGroup, g.rightKerningGroup,
             g.leftMetricsKey, g.rightMetricsKey, layer.leftMetricsKey, layer.rightMetricsKey, layer.isAligned,
             layer.LSB, layer.RSB, layer.width, len(layer.components), len(layer.paths))
        rows += 1
    sample = {}
    for name in ("A", "Aacute", "alpha", "afii10017", "alef-ar", "Alpha", "a", "period", "zero"):
        g = font.glyphs[name]
        if g is None:
            continue
        layer = g.layers[mid]
        sample[name] = {"script": g.script, "category": g.category, "sub": g.subCategory,
                        "direction": str(getattr(g, "direction", None)), "lastChange": str(getattr(g, "lastChange", None)),
                        "components": [c.componentName for c in layer.components], "aligned": layer.isAligned,
                        "groups": [g.leftKerningGroup, g.rightKerningGroup], "id": g.id}
    return {"s": round(time.time() - t, 4), "rows": rows, "sample": sample}


@step("keys")
def _():
    a, v, t_, o = font.glyphs["A"], font.glyphs["V"], font.glyphs["T"], font.glyphs["o"]
    for g, name in ((a, "A"), (v, "V")):
        g.leftKerningGroup = name
        g.rightKerningGroup = name
    font.setKerningForPair(mid, "@MMK_L_A", "@MMK_R_V", -50)
    font.setKerningForPair(mid, "T", "o", -80)
    font.setKerningForFontMasterID_leftKey_rightKey_value_direction_(mid, "@MMK_L_V", "@MMK_R_A", -55, LTR)
    font.setKerningForFontMasterID_leftKey_rightKey_value_direction_(mid, t_.id, a.id, -66, LTR)
    stored = font.kerningLTR[mid]
    keys = {}
    for lk in stored.keys():
        for rk in stored[lk].keys():
            keys["%s | %s" % (lk, rk)] = float(stored[lk][rk])
    reads = {
        "wrapper @MMK A V": font.kerningForPair(mid, "@MMK_L_A", "@MMK_R_V"),
        "wrapper glyph A V (group pair)": font.kerningForPair(mid, "A", "V"),
        "wrapper T o": font.kerningForPair(mid, "T", "o"),
        "objc T A": font.kerningForFontMasterID_leftKey_rightKey_direction_(mid, t_.id, a.id, LTR),
    }
    rtl = {}
    alef, beh = font.glyphs["alef-ar"], font.glyphs["beh-ar"]
    if alef is not None and beh is not None:
        alef.leftKerningGroup = "alef"
        alef.rightKerningGroup = "alef"
        beh.leftKerningGroup = "beh"
        beh.rightKerningGroup = "beh"
        font.setKerningForPair(mid, "@MMK_R_alef", "@MMK_L_beh", -30, direction=RTL)
        font.setKerningForPair(mid, "alef-ar", "beh-ar", -31, direction=RTL)
        st = font.kerningRTL[mid] if font.kerningRTL else None
        if st is not None:
            for lk in st.keys():
                for rk in st[lk].keys():
                    rtl["%s | %s" % (lk, rk)] = float(st[lk][rk])
    return {"ltr": keys, "reads": {k: (None if v is None else float(v)) for k, v in reads.items()}, "rtl": rtl,
            "ids": {"A": a.id, "T": t_.id, "o": o.id}}


pairs = []
random.seed(7)
names = [g.name for g in glyphs[:700]]
ids = dict((g.name, g.id) for g in glyphs)
for _i in range(5000):
    pairs.append((random.choice(names), random.choice(names), random.randint(-90, 60) or 1))


def count_undo():
    um = font.undoManager()
    if um is None:
        return None
    return {"canUndo": bool(um.canUndo()), "groupsByEvent": bool(um.groupsByEvent()), "levels": int(um.levelsOfUndo()),
            "name": str(um.undoActionName())}


@step("write_wrapper")
def _():
    t = time.time()
    font.disableUpdateInterface()
    for a, b, v in pairs[:2000]:
        font.setKerningForPair(mid, a, b, v)
    font.enableUpdateInterface()
    return {"pairs": 2000, "s": round(time.time() - t, 4), "undo": count_undo()}


@step("write_objc")
def _():
    t = time.time()
    font.disableUpdateInterface()
    for a, b, v in pairs[2000:4000]:
        font.setKerningForFontMasterID_leftKey_rightKey_value_direction_(mid, ids[a], ids[b], v, LTR)
    font.enableUpdateInterface()
    return {"pairs": 2000, "s": round(time.time() - t, 4), "undo": count_undo()}


@step("write_objc_no_undo")
def _():
    um = font.undoManager()
    t = time.time()
    font.disableUpdateInterface()
    if um is not None:
        um.disableUndoRegistration()
    for a, b, v in pairs[4000:5000]:
        font.setKerningForFontMasterID_leftKey_rightKey_value_direction_(mid, ids[a], ids[b], v, LTR)
    if um is not None:
        um.enableUndoRegistration()
    font.enableUpdateInterface()
    return {"pairs": 1000, "s": round(time.time() - t, 4)}


@step("write_bulk")
def _():
    """Swap the master's whole kerning dictionary in one call."""
    t0 = time.time()
    current = font.kerningLTR
    whole = NSMutableDictionary.alloc().init()
    for m in current.keys():
        inner = NSMutableDictionary.alloc().init()
        for lk, row in current[m].items():
            inner[lk] = NSMutableDictionary.dictionaryWithDictionary_(row)
        whole[m] = inner
    t1 = time.time()
    md = whole[mid]
    n = 0
    for i in range(300):
        for j in range(70):
            lk, rk = ids[names[i]], ids[names[(i * 7 + j) % len(names)]]
            row = md.get(lk)
            if row is None:
                row = NSMutableDictionary.alloc().init()
                md[lk] = row
            row[rk] = -(j % 40) - 5
            n += 1
    t2 = time.time()
    font.setKerningLTR_(whole)
    t3 = time.time()
    check = font.kerningForFontMasterID_leftKey_rightKey_direction_(mid, ids[names[5]], ids[names[(5 * 7 + 3) % len(names)]], LTR)
    return {"entries": n, "copy_s": round(t1 - t0, 4), "build_s": round(t2 - t1, 4), "set_s": round(t3 - t2, 4),
            "readback": None if check is None else float(check), "expected": -8, "undo": count_undo()}


@step("groups_write")
def _():
    t = time.time()
    k = 0
    for g in glyphs[:2000]:
        if not g.leftKerningGroup:
            g.leftKerningGroup = g.name
            k += 1
        if not g.rightKerningGroup:
            g.rightKerningGroup = g.name
    return {"glyphs": k, "s": round(time.time() - t, 4)}


@step("metrics")
def _():
    out = {}
    a = font.glyphs["A"].layers[mid]
    aa = font.glyphs["Aacute"].layers[mid]
    out["Aacute aligned"] = aa.isAligned
    before = (a.LSB, a.RSB, aa.LSB, aa.RSB, aa.width, a.width)
    a.LSB = a.LSB + 40
    a.RSB = a.RSB + 20
    after = (a.LSB, a.RSB, aa.LSB, aa.RSB, aa.width, a.width)
    out["A then Aacute (LSB, RSB, Aacute LSB, RSB, width, A width) before/after"] = [before, after]
    h = font.glyphs["H"].layers[mid]
    og = font.glyphs["O"]
    o_layer = og.layers[mid]
    og.leftMetricsKey = "=H"
    b2 = o_layer.LSB
    h.LSB = h.LSB + 33
    mid_val = o_layer.LSB
    o_layer.syncMetrics()
    out["O (=H) LSB: before H change, after H change, after syncMetrics; H LSB"] = [b2, mid_val, o_layer.LSB, h.LSB]
    return out


@step("undo_replay")
def _():
    um = font.undoManager()
    if um is None:
        return None
    n = 0
    t = time.time()
    while um.canUndo() and n < 20000:
        um.undo()
        n += 1
    return {"undo_steps": n, "s": round(time.time() - t, 3),
            "T o after undo": font.kerningForPair(mid, "T", "o")}


facts["defaults_seen"] = {"devScript": Glyphs.defaults[PREFIX + "devScript"] is not None}
with open(out_path, "w") as f:
    json.dump(facts, f, indent=1, default=str)
try:
    font.close(ignoreChanges=True)
except Exception:
    pass
NSApp().terminate_(None)
