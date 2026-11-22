# encoding: utf-8
"""
kk3_features — the sample text set with OpenType features.

The panes set the sample text glyph by glyph: each character by the font's
cmap, each /name escape as that glyph (kk3_snapshot.tokenize). With features
switched on (the Features window), each paragraph is then set again the way
Glyphs sets an Edit tab with those features selected: Glyphs' own feature
composer (GSFeatureComposer, which the Edit view uses) runs the font's
compiled features over it. The panes then show the glyphs a reader gets
(small caps, old-style figures, case forms, fractions, stylistic sets,
ligatures), the font as it is on the left and Kinetikern3 on the right, and
the preview spaces and kerns those glyphs as it does the rest of the sample.

How a paragraph goes through the composer:

* each glyph token becomes the character Glyphs gives that glyph in an Edit
  view (font.characterForGlyph_: its Unicode, else a private-use character
  of Glyphs' own), each space a space;
* the composer returns the text it sets, one character per glyph;
* each character becomes its glyph again (font.glyphForCharacter_), the
  font's space glyph a space.

A paragraph is cut at characters the font does not have (they stay missing)
and at line breaks; the pieces are set one by one. Features only substitute
here: positioning features (kern, mark, cpsp…) are left out, since the panes
show the font's own kerning on the left and Kinetikern3's on the right.

Everything here runs on the main thread (GSFont is not thread-safe). A
paragraph takes about a millisecond; results are kept per paragraph and
features, so typing only sets the paragraph that changed. With no feature on,
the tokens are passed through untouched: the panes are exactly as before.
"""

from __future__ import division, print_function, unicode_literals

import traceback

try:
    from GlyphsApp import GSLTR, GSRTL
except ImportError:
    try:  # Glyphs 3 before 3.2
        from GlyphsApp import LTR as GSLTR, RTL as GSRTL
    except ImportError:  # outside Glyphs (tests)
        GSLTR, GSRTL = 0, 2

COMPOSER_CLASS = "GSFeatureComposer"
COMPOSE = "composeString_forFont_featureTags_script_language_writingDirection_characterIndexesByLayerIndex_error_"

# positioning features: the panes kern with the font's kerning or Kinetikern3's
POSITIONING = frozenset((
    "kern", "dist", "mark", "mkmk", "abvm", "blwm", "curs", "cpsp", "size", "opbd", "lfbd", "rtbd", "palt",
    "halt", "vkrn", "vpal", "vhal", "valt"))
# substitution features no reader applies to running text
HIDDEN = frozenset(("aalt",))

# the registered names of the features fonts usually have
NAMES = {
    "afrc": "Alternative fractions", "c2pc": "Petite capitals from capitals",
    "c2sc": "Small capitals from capitals", "calt": "Contextual alternates", "case": "Case-sensitive forms",
    "ccmp": "Glyph composition", "clig": "Contextual ligatures", "cswh": "Contextual swash",
    "dlig": "Discretionary ligatures", "dnom": "Denominators", "expt": "Expert forms", "falt": "Final glyph on line",
    "fina": "Terminal forms", "frac": "Fractions", "fwid": "Full widths", "half": "Half forms",
    "hist": "Historical forms", "hlig": "Historical ligatures", "hwid": "Half widths", "init": "Initial forms",
    "isol": "Isolated forms", "ital": "Italics", "jalt": "Justification alternates", "lnum": "Lining figures",
    "locl": "Localized forms", "medi": "Medial forms", "mgrk": "Mathematical Greek", "nalt": "Alternate annotation",
    "numr": "Numerators", "onum": "Oldstyle figures", "ordn": "Ordinals", "ornm": "Ornaments",
    "pcap": "Petite capitals", "pnum": "Proportional figures", "rand": "Randomize", "rclt": "Required contextual",
    "rlig": "Required ligatures", "rvrn": "Required variation alternates", "salt": "Stylistic alternates",
    "sinf": "Scientific inferiors", "smcp": "Small capitals", "subs": "Subscript", "sups": "Superscript",
    "swsh": "Swash", "titl": "Titling", "tnum": "Tabular figures", "unic": "Unicase", "zero": "Slashed zero",
    "liga": "Standard ligatures",
}
# what a browser or a layout application applies by default: the Features
# window's "Text defaults" switches these on (those the font has)
TEXT_DEFAULTS = ("ccmp", "locl", "rlig", "rclt", "calt", "clig", "liga", "rvrn")
# the features that substitute what Kinetikern3 spaces by convention or by
# class (small caps, figures, case forms): the window lists these first
SPACING_RELEVANT = ("smcp", "c2sc", "pcap", "c2pc", "case", "onum", "lnum", "pnum", "tnum", "frac", "numr", "dnom",
                    "sups", "subs", "sinf", "ordn", "zero", "titl", "unic")


# Glyphs' script names (GSGlyph.script) → OpenType script tags: the composer looks a run's features up
# under its script (an Arabic run set without "arab" keeps its isolated forms)
SCRIPT_TAGS = {
    "latin": "latn", "cyrillic": "cyrl", "greek": "grek", "coptic": "copt", "armenian": "armn", "georgian": "geor",
    "hebrew": "hebr", "arabic": "arab", "syriac": "syrc", "thaana": "thaa", "nko": "nko ", "devanagari": "dev2",
    "bengali": "bng2", "gurmukhi": "gur2", "gujarati": "gjr2", "oriya": "ory2", "tamil": "tml2", "telugu": "tel2",
    "kannada": "knd2", "malayalam": "mlm2", "sinhala": "sinh", "thai": "thai", "lao": "lao ", "tibetan": "tibt",
    "myanmar": "mym2", "khmer": "khmr", "ethiopic": "ethi", "cherokee": "cher", "tifinagh": "tfng",
    "mongolian": "mong", "han": "hani", "hangul": "hang", "kana": "kana", "hiragana": "kana", "katakana": "kana",
    "bopomofo": "bopo", "yi": "yi  ", "vai": "vai ", "adlam": "adlm", "canadian": "cans", "runic": "runr",
    "ogham": "ogam", "gothic": "goth", "javanese": "java", "balinese": "bali", "sundanese": "sund",
}


def script_tag(script):
    """The OpenType script tag of a Glyphs script name, or None (no script, or one not listed: the font's
    default script)."""
    if not script:
        return None
    return SCRIPT_TAGS.get(str(script).strip().lower())


def feature_name(tag, label=None):
    """A line for the window: "smcp — Small capitals", a stylistic set by its name in the font."""
    if label:
        return "%s — %s" % (tag, label)
    if len(tag) == 4 and tag[:2] == "ss" and tag[2:].isdigit():
        return "%s — Stylistic set %d" % (tag, int(tag[2:]))
    if len(tag) == 4 and tag[:2] == "cv" and tag[2:].isdigit():
        return "%s — Character variant %d" % (tag, int(tag[2:]))
    name = NAMES.get(tag)
    return "%s — %s" % (tag, name) if name else tag


def _label(feature):
    """A stylistic set's name in the font (its first label), or None."""
    try:
        labels = feature.labels
        if not labels:
            return None
        for label in labels:
            value = getattr(label, "value", None)
            if value:
                return str(value)
    except Exception:
        pass
    return None


def font_features(font):
    """[(tag, label or None)] of the font's substitution features that text can switch on, in the font's
    order: no positioning feature, no aalt, no inactive feature, each tag once."""
    out, seen = [], set()
    try:
        features = list(font.features)
    except Exception:
        return out
    for feature in features:
        try:
            tag = str(feature.name or "").strip()
            active = getattr(feature, "active", True)
            if callable(active):
                active = active()
        except Exception:
            continue
        if len(tag) != 4 or tag in POSITIONING or tag in HIDDEN or tag in seen or active is False:
            continue
        seen.add(tag)
        out.append((tag, _label(feature)))
    return out


def ordered(tags):
    """The tags in the window's order: the spacing-relevant ones first, then the rest as given."""
    first = [t for t in SPACING_RELEVANT if t in tags]
    return first + [t for t in tags if t not in first]


def _code_points(text):
    """The code points of a string PyObjC hands over in UTF-16 halves (a
    character above U+FFFF comes as a surrogate pair)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = ord(text[i])
        if 0xD800 <= c < 0xDC00 and i + 1 < n and 0xDC00 <= ord(text[i + 1]) < 0xE000:
            c = 0x10000 + ((c - 0xD800) << 10) + (ord(text[i + 1]) - 0xDC00)
            i += 1
        out.append(c)
        i += 1
    return out


class Shaper(object):
    """Sets the sample's tokens with features, through Glyphs' composer.

    `space`, `newline`, `missing`: the tokens kk3_snapshot.tokenize makes
    besides glyph names."""

    CACHE_LIMIT = 512  # paragraphs kept per Shaper

    def __init__(self, font, space=" ", newline="\n", missing=None):
        self.font = font
        self.space, self.newline, self.missing = space, newline, missing
        self.compiled = False
        self.error = None  # why the last set could not be made (shown in the window)
        self._composer = None
        self._chars = {}  # glyph name → its character in an Edit view (None: none)
        self._names = {}  # code point → glyph name (None: no glyph)
        self._cache = {}  # (run, tags, rtl, script) → tokens
        try:
            from Foundation import NSClassFromString
            self._composer = NSClassFromString(COMPOSER_CLASS)
        except Exception:
            self._composer = None

    @property
    def available(self):
        """Glyphs has a feature composer this can call."""
        return self._composer is not None and hasattr(self._composer, COMPOSE)

    def forget(self):
        """The font's glyphs or features changed (read again): everything is looked up and compiled anew."""
        self.compiled = False
        self._chars = {}
        self._names = {}
        self._cache = {}

    def compile(self):
        """Compiles the font's features as Glyphs does for an Edit view (Font Info ▸ Features ▸ Compile): the
        composer sets text with what was compiled last. True when it compiled."""
        self.compiled = True
        try:
            result = self.font.compileFeatures()
        except Exception:
            self.error = "the font's features could not be compiled:\n" + traceback.format_exc()
            return False
        ok = result[0] if isinstance(result, tuple) else result
        if ok is False:
            err = result[1] if isinstance(result, tuple) and len(result) > 1 else None
            self.error = "the font's features do not compile%s" % (": %s" % err if err is not None else "")
            return False
        self.error = None
        return True

    def _char(self, name):
        ch = self._chars.get(name, False)
        if ch is False:
            ch = None
            try:
                glyph = self.font.glyphs[name]
                if glyph is not None:
                    ch = self.font.characterForGlyph_(glyph)
                    if isinstance(ch, int):  # Glyphs 4 gives the code point
                        ch = chr(ch) if 0 < ch <= 0x10FFFF else None
                    else:
                        ch = str(ch) if ch else None
            except Exception:
                ch = None
            self._chars[name] = ch
        return ch

    def _name(self, code):
        name = self._names.get(code, False)
        if name is False:
            name = None
            try:
                glyph = self.font.glyphForCharacter_(code)
                if glyph is not None:
                    name = str(glyph.name)
            except Exception:
                name = None
            self._names[code] = name
        return name

    def shape(self, tokens, tags, space_glyph=None, is_rtl=None, script_of=None):
        """`tokens` set with the features `tags` (a list of tags, in any order).

        `space_glyph`: the font's word space (a glyph the composer gives
        back for a space becomes a space token again); `is_rtl(name)`: whether
        a glyph is set right to left (a run that starts with one is set right
        to left); `script_of(name)`: a glyph's OpenType script tag or None. A
        piece is set in runs of one script each; glyphs without a script
        (figures, punctuation) and spaces go with the run they are in. With
        no tags, or without a composer, the tokens come back as they are."""
        tokens = list(tokens)
        tags = tuple(sorted(set(str(t) for t in tags or ())))
        if not tags or not self.available:
            return tokens
        if not self.compiled:
            self.compile()
        out = []
        segment = []
        for tok in tokens:
            if tok == self.newline or tok is self.missing:
                out.extend(self._runs(segment, tags, space_glyph, is_rtl, script_of))
                segment = []
                out.append(tok)
            else:
                segment.append(tok)
        out.extend(self._runs(segment, tags, space_glyph, is_rtl, script_of))
        return out

    def _runs(self, segment, tags, space_glyph, is_rtl, script_of):
        """A piece of a paragraph set run by run, one script per run."""
        if script_of is None:
            return self._segment(segment, tags, space_glyph, is_rtl, None)
        runs = []  # [(script, tokens)]
        pending = []  # spaces and glyphs without a script before the first glyph with one
        for tok in segment:
            script = None if tok == self.space else script_of(tok)
            if script is None:
                (runs[-1][1] if runs else pending).append(tok)
            elif runs and runs[-1][0] == script:
                runs[-1][1].append(tok)
            else:
                runs.append((script, pending + [tok]))
                pending = []
        if not runs:
            return self._segment(pending, tags, space_glyph, is_rtl, None)
        out = []
        for script, run in runs:
            out.extend(self._segment(run, tags, space_glyph, is_rtl, script))
        return out

    def _segment(self, segment, tags, space_glyph, is_rtl, script):
        """One run of a paragraph (glyphs and spaces) set with `tags` in `script` (an OpenType tag or None)."""
        if not any(tok != self.space for tok in segment):
            return segment
        first = next(tok for tok in segment if tok != self.space)
        rtl = bool(is_rtl(first)) if is_rtl is not None else False
        key = (tuple(segment), tags, rtl, script)
        cached = self._cache.get(key)
        if cached is not None:
            return list(cached)
        chars = []
        for tok in segment:
            ch = " " if tok == self.space else self._char(tok)
            if ch is None:
                return segment  # a glyph the composer cannot be given: the piece stays as typed
            chars.append(ch)
        try:
            from Foundation import NSAttributedString, NSMutableArray
            text = NSAttributedString.alloc().initWithString_("".join(chars))
            indexes = NSMutableArray.array()
            composed = getattr(self._composer, COMPOSE)(text, self.font, list(tags), script, None,
                                                        GSRTL if rtl else GSLTR, indexes, None)
            if isinstance(composed, tuple):
                composed = composed[0]
            if composed is None:
                self.error = "Glyphs' feature composer set nothing"
                return segment
            result = str(composed.string())
        except Exception:
            self.error = "Glyphs' feature composer failed:\n" + traceback.format_exc()
            return segment
        shaped = []
        for code in _code_points(result):
            if code == 0x20:
                shaped.append(self.space)
                continue
            name = self._name(code)
            if name is None:
                shaped.append(self.missing)
            elif name == space_glyph:
                shaped.append(self.space)
            else:
                shaped.append(name)
        self.error = None
        if len(self._cache) >= self.CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = tuple(shaped)
        return shaped
