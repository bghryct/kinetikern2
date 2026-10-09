# encoding: utf-8
"""
kk2_groups — spacing groups.

Glyphs are painted into colour-coded groups. Each group is either spaced with
its own Looseness and kerning force (figures looser, Cyrillic a touch tighter,
fractions with gentler kerning…) or frozen: its glyphs keep their sidebearings
and the kerning between them stays as it is, and only the rest of the font is
spaced around them. Glyphs in no group follow the main window's settings.

With "Match the frozen spacing" on, the main Looseness is first moved to the
frozen glyphs' own tightness (the engine fits it on their current
sidebearings), so new glyphs come out as tight or loose as the spacing that is
already there; the Looseness slider is then an offset from it.

The groups belong to the font: they are kept in its userData (one key, saved
with the .glyphs file) and only written when the groups change.

No AppKit here except in `load` / `save` (font userData): the model is plain
Python, shared by the window, the selftest and tools.
"""

from __future__ import division, print_function, unicode_literals

import uuid

USERDATA_KEY = "com.mirkovelimirovic.Kinetikern2.spacingGroups"

MODE_SPACE = "space"
MODE_FREEZE = "freeze"

# distinct, readable on white and dark backgrounds (sRGB hex)
PALETTE = ("#2a78d6", "#eb6834", "#1baf7a", "#a35ce0", "#e0b11b", "#d64570", "#2bb5c9", "#8a6d3b",
           "#6e7a8a", "#57a83a")


class SpacingGroup(object):
    __slots__ = ("gid", "name", "color", "mode", "looseness", "force")

    def __init__(self, name, color, mode=MODE_SPACE, looseness=0.0, force=100.0, gid=None):
        self.gid = gid or uuid.uuid4().hex[:10]
        self.name = name
        self.color = color
        self.mode = mode
        self.looseness = float(looseness)  # offset from the main Looseness, slider units
        self.force = float(force)  # kerning intensity, percent of the main one

    @property
    def frozen(self):
        return self.mode == MODE_FREEZE

    def describe(self):
        if self.frozen:
            return "frozen"
        parts = []
        if abs(self.looseness) > 1e-6:
            parts.append("Looseness %+.2f" % self.looseness)
        if abs(self.force - 100.0) > 1e-6:
            parts.append("force %d%%" % round(self.force))
        return ", ".join(parts) or "main settings"

    def to_data(self):
        return {"id": self.gid, "name": self.name, "color": self.color, "mode": self.mode,
                "looseness": round(self.looseness, 4), "force": round(self.force, 2)}

    @classmethod
    def from_data(cls, d):
        mode = d.get("mode") if d.get("mode") in (MODE_SPACE, MODE_FREEZE) else MODE_SPACE
        return cls(str(d.get("name") or "Group"), str(d.get("color") or PALETTE[0]), mode,
                   float(d.get("looseness") or 0.0), float(d.get("force") if d.get("force") is not None else 100.0),
                   str(d.get("id") or "") or None)


class GroupSet(object):
    """The spacing groups of one font and which glyph is in which."""

    def __init__(self):
        self.groups = []
        self.members = {}  # glyph name → group id
        self.match_frozen = True
        self.version = 0  # bumped on every change (solve keys, saving)

    # ---------------------------------------------------------- groups
    def _changed(self):
        self.version += 1

    def group(self, gid):
        for g in self.groups:
            if g.gid == gid:
                return g
        return None

    def add_group(self, name=None, mode=MODE_SPACE):
        used = set(g.color for g in self.groups)
        color = next((c for c in PALETTE if c not in used), PALETTE[len(self.groups) % len(PALETTE)])
        g = SpacingGroup(name or "Group %d" % (len(self.groups) + 1), color, mode)
        self.groups.append(g)
        self._changed()
        return g

    def remove_group(self, gid):
        self.groups = [g for g in self.groups if g.gid != gid]
        self.members = dict((n, k) for n, k in self.members.items() if k != gid)
        self._changed()

    def update(self, gid, **values):
        g = self.group(gid)
        if g is None:
            return
        for k, v in values.items():
            if k in ("name", "color", "mode") and v is not None:
                setattr(g, k, v)
            elif k in ("looseness", "force") and v is not None:
                setattr(g, k, float(v))
        self._changed()

    def assign(self, names, gid):
        """Puts `names` into group `gid` (None: back to the main settings)."""
        if gid is not None and self.group(gid) is None:
            return 0
        n = 0
        for name in names:
            if gid is None:
                if self.members.pop(name, None) is not None:
                    n += 1
            elif self.members.get(name) != gid:
                self.members[name] = gid
                n += 1
        if n:
            self._changed()
        return n

    def group_of(self, name):
        gid = self.members.get(name)
        return self.group(gid) if gid is not None else None

    def counts(self, names=None):
        out = dict((g.gid, 0) for g in self.groups)
        for name, gid in self.members.items():
            if names is None or name in names:
                if gid in out:
                    out[gid] += 1
        return out

    # ---------------------------------------------------------- engine
    def is_empty(self):
        return not self.groups or not self.members

    def frozen_names(self):
        frozen = set(g.gid for g in self.groups if g.frozen)
        return set(n for n, gid in self.members.items() if gid in frozen)

    def opts_for(self, names):
        """Per-glyph engine options (frozen, Looseness offset, force multiplier)
        for the snapshot's glyph order; None when no group changes anything."""
        if self.is_empty():
            return None
        by_id = dict((g.gid, g) for g in self.groups)
        out = []
        effective = False
        for name in names:
            g = by_id.get(self.members.get(name))
            if g is None:
                out.append((False, 0.0, 1.0))
                continue
            o = (g.frozen, 0.0 if g.frozen else g.looseness, 1.0 if g.frozen else max(0.0, g.force / 100.0))
            effective = effective or o != (False, 0.0, 1.0)
            out.append(o)
        return out if effective else None

    def key(self):
        """What a solve depends on (for reusing results)."""
        if self.is_empty():
            return ()
        return (self.version, self.match_frozen)

    def summary(self, names=None):
        if not self.groups:
            return "No spacing groups: every glyph follows the main settings."
        counts = self.counts(names)
        parts = []
        for g in self.groups:
            parts.append("%s %d (%s)" % (g.name, counts.get(g.gid, 0), g.describe()))
        return "; ".join(parts)

    # ---------------------------------------------------------- storage
    def to_data(self):
        return {"version": 1, "matchFrozen": bool(self.match_frozen),
                "groups": [g.to_data() for g in self.groups],
                "members": dict((gid, sorted(n for n, k in self.members.items() if k == gid))
                                for gid in [g.gid for g in self.groups])}

    @classmethod
    def from_data(cls, data):
        s = cls()
        if not data:
            return s
        try:
            s.match_frozen = bool(data.get("matchFrozen", True))
            for d in data.get("groups") or []:
                s.groups.append(SpacingGroup.from_data(dict(d)))
            ids = set(g.gid for g in s.groups)
            for gid, names in dict(data.get("members") or {}).items():
                gid = str(gid)
                if gid in ids:
                    for n in names or []:
                        s.members[str(n)] = gid
        except Exception:
            return cls()
        return s

    @classmethod
    def load(cls, font):
        try:
            data = font.userData[USERDATA_KEY]
        except Exception:
            data = None
        if data is None:
            return cls()
        return cls.from_data(_plain(data))

    def save(self, font):
        """Writes the groups into the font's userData (removes the key when empty)."""
        try:
            if not self.groups:
                if font.userData[USERDATA_KEY] is not None:
                    del font.userData[USERDATA_KEY]
                return True
            font.userData[USERDATA_KEY] = self.to_data()
            return True
        except Exception:
            return False


def _plain(v):
    """NSDictionary / NSArray / NSString → dict / list / str."""
    if hasattr(v, "keys") and callable(v.keys):
        return dict((str(k), _plain(v[k])) for k in v.keys())
    if isinstance(v, (list, tuple)) or (hasattr(v, "count") and hasattr(v, "objectAtIndex_")):
        return [_plain(x) for x in v]
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    try:
        f = float(v)
        if str(v).strip().lstrip("-").replace(".", "", 1).isdigit():
            return f
    except (TypeError, ValueError):
        pass
    return str(v)


# ------------------------------------------------------------ sections

FIGURE_SUFFIXES = (".tf", ".lf", ".osf", ".tosf", ".sups", ".subs", ".sinf", ".numr", ".dnom", ".tnum", ".pnum",
                   ".onum", ".lnum", ".dnom", ".numerator", ".denominator")
FRACTION_NAMES = ("fraction", "onehalf", "onequarter", "threequarters", "onethird", "twothirds", "oneeighth",
                  "threeeighths", "fiveeighths", "seveneighths", "percent", "perthousand")

CASE_NAMES = {1: "Uppercase", 2: "Lowercase", 3: "Small caps", 4: "Minor"}


def script_label(script):
    """A readable script name ("Latin", "Cyrillic") from a Glyphs script name
    ("cyrillic"), an ISO 15924 code ("Cyrl") or kk2_bridge's packed code;
    None for none or Common."""
    if not script:
        return None
    import kk2_bridge as kb
    if isinstance(script, int):
        iso = "".join(chr((script >> s) & 0xFF) for s in (24, 16, 8, 0)).strip()
    else:
        iso = str(script).strip()
    if not iso:
        return None
    names = dict((v.lower(), k) for k, v in kb.SCRIPT_NAMES.items())
    name = names.get(iso.lower(), iso if iso.lower() in kb.SCRIPT_NAMES else None)
    if name is None:
        return iso if len(iso) == 4 else iso.capitalize()
    return name.capitalize()


def section_of(name, category, subcategory, case, script):
    """A readable section for one glyph (script first for letters)."""
    base = name.split(".")[0]
    lower = name.lower()
    if category == "Number":
        if subcategory == "Fraction" or any(f in base for f in FRACTION_NAMES) or ".numr" in lower or ".dnom" in lower:
            return "Figures · Fractions"
        if any(s in lower for s in (".sups", ".subs", ".sinf", "superior", "inferior")):
            return "Figures · Superiors and inferiors"
        if any(s in lower for s in (".tf", ".tnum", ".tosf")):
            return "Figures · Tabular"
        if any(s in lower for s in (".osf", ".onum")):
            return "Figures · Old-style"
        return "Figures"
    if category == "Letter":
        sc = script_label(script) or "Other script"
        c = CASE_NAMES.get(case or 0, "")
        if ".sc" in lower or ".smcp" in lower or ".c2sc" in lower:
            c = "Small caps"
        return "%s · %s" % (sc, c) if c else sc
    if category == "Punctuation":
        if any(q in base for q in ("quote", "guil")):
            return "Punctuation · Quotes"
        if any(d in base for d in ("dash", "hyphen")):
            return "Punctuation · Dashes"
        if any(b in base for b in ("paren", "bracket", "brace")):
            return "Punctuation · Brackets"
        return "Punctuation"
    if category == "Symbol":
        if subcategory == "Currency":
            return "Symbols · Currency"
        if subcategory == "Math":
            return "Symbols · Math"
        return "Symbols"
    if category == "Mark":
        return "Marks"
    return category or "Other"


# The groups by_category makes, in this order, then one per script
CATEGORY_GROUPS = ("Figures", "Punctuation", "Symbols")


def by_category(group_set, entries):
    """Puts the glyphs of `entries` [(name, category, subcategory, case,
    script)] that are in no group yet into groups by kind, so each kind can
    be spaced on its own: Figures, Punctuation, Symbols, and the letters of
    every script but Latin (Cyrillic, Greek…: one group each; Latin letters
    keep the main settings). A group of that name that is there already is
    used, its settings kept; a new one starts at the main settings, so
    nothing changes until its Looseness or kerning force is set. Glyphs
    already in a group stay where they are. Returns [(group name, glyphs
    added)] in the order the groups are listed."""
    targets = {}
    for name, cat, sub, case, script in entries:
        if name in group_set.members:
            continue
        head = section_of(name, cat, sub, case, script).split(" \u00b7 ")[0]
        if head in CATEGORY_GROUPS or (cat == "Letter" and head not in ("Latin", "Other script")):
            targets.setdefault(head, []).append(name)
    order = [k for k in CATEGORY_GROUPS if k in targets] + sorted(k for k in targets if k not in CATEGORY_GROUPS)
    added = []
    for target in order:
        g = next((x for x in group_set.groups if x.name.strip().lower() == target.lower()), None)
        if g is None:
            g = group_set.add_group(target)
        added.append((target, group_set.assign(targets[target], g.gid)))
    return added


def snapshot_entries(snapshot):
    """[(name, category, subcategory, case, script)] of a snapshot's glyphs."""
    out = []
    for name in snapshot.names:
        info = snapshot.infos.get(name)
        out.append((name, getattr(info, "category", None), getattr(info, "subcategory", None),
                    getattr(info, "case", 0) or 0, getattr(info, "script", None)))
    return out


def snapshot_sections(snapshot):
    """sections() of a snapshot's glyphs, from what the snapshot read (no
    Glyphs calls: fast for fonts of thousands of glyphs)."""
    entries = []
    for name in snapshot.names:
        info = snapshot.infos.get(name)
        entries.append((name, getattr(info, "category", None), getattr(info, "subcategory", None),
                        getattr(info, "case", 0) or 0, getattr(info, "script", None)))
    return sections(entries)


def sections(entries):
    """[(name, category, subcategory, case, script)] → [(section, [names])],
    letters first by script and case, then figures, punctuation, symbols."""
    out = {}
    for name, cat, sub, case, script in entries:
        out.setdefault(section_of(name, cat, sub, case, script), []).append(name)

    def order(key):
        rank = 9
        if key.startswith("Latin"):
            rank = 0
        elif "·" in key and not key.startswith(("Figures", "Punctuation", "Symbols")):
            rank = 1
        elif key.startswith("Figures"):
            rank = 3
        elif key.startswith("Punctuation"):
            rank = 4
        elif key.startswith("Symbols"):
            rank = 5
        elif key == "Marks":
            rank = 6
        return (rank, key)

    return [(k, out[k]) for k in sorted(out, key=order)]
