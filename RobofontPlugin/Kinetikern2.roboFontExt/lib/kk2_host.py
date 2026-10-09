# encoding: utf-8
"""
kk2_host — what the Kinetikern2 extension asks of RoboFont, in one place.

The modules this extension shares with the Glyphs plugin (the engine bridge,
the proof panes, the designer harness, the Pairs, Harness and Spacing Groups
windows) know nothing of the app they run in. This module answers for
RoboFont: the fonts that are open, a font's name, the extension's
preferences, messages, the Output window, and a Space Center on a few pairs.

Fonts are fontParts fonts (CurrentFont(), AllFonts()); the work underneath
is done on their defcon objects (font.naked()), which RoboFont edits and
which tests outside RoboFont have too (fontParts.fontshell). Nothing here
imports mojo at module level, so the extension's model can be tested
without RoboFont.
"""

from __future__ import division, print_function, unicode_literals

import os
import traceback

PREFIX = "com.mirkovelimirovic.Kinetikern2."
OUTPUT_NAME = "the Output window"  # where tracebacks go (Glyphs: the Macro panel)
PAIR_VIEW_NAME = "Space Center"  # where a pair is opened to look at it (Glyphs: an Edit tab)


def naked(font):
    """The defcon font of a fontParts font (a defcon font stays as it is)."""
    if font is None:
        return None
    n = getattr(font, "naked", None)
    return n() if callable(n) else font


def same_font(a, b):
    """True when two font objects (fontParts or defcon) are one font."""
    if a is None or b is None:
        return False
    return naked(a) is naked(b)


def current_font():
    """The font of the frontmost font window, or None (outside RoboFont too)."""
    try:
        from mojo.roboFont import CurrentFont
    except ImportError:
        return None
    try:
        return CurrentFont()
    except Exception:
        return None


def all_fonts():
    """The open fonts, or None outside RoboFont (no list to check against)."""
    try:
        from mojo.roboFont import AllFonts
    except ImportError:
        return None
    try:
        return list(AllFonts())
    except Exception:
        return None


def font_is_open(font):
    """False once the user closed the font (checked where the user acts)."""
    fonts = all_fonts()
    if fonts is None:
        return True
    return any(same_font(f, font) for f in fonts)


def _info(font, attr):
    try:  # a font that was closed may refuse
        info = getattr(font, "info", None)
        value = getattr(info, attr, None) if info is not None else None
    except Exception:
        return None
    return str(value) if value else None


def _file_name(font):
    try:
        path = getattr(font, "path", None)
    except Exception:
        return None
    return os.path.splitext(os.path.basename(path))[0] if path else None


def family_name(font):
    """The font's family name (its file name, "Untitled" without either)."""
    if font is None:
        return ""
    return _info(font, "familyName") or _file_name(font) or "Untitled"


def units_per_em(font):
    """The font's units per em (1000 when it has none)."""
    try:
        value = float(host_info(font, "unitsPerEm") or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else 1000.0


def host_info(font, attr):
    info = getattr(font, "info", None)
    return getattr(info, attr, None) if info is not None else None


def style_name(font):
    return _info(font, "styleName") or ""


def font_title(font):
    """"Family Style" — what the window and its popup call a font."""
    if font is None:
        return ""
    family, style = family_name(font), style_name(font)
    return "%s %s" % (family, style) if style else family


def get_default(key, fallback=None):
    """A preference of the extension (RoboFont's extension defaults)."""
    try:
        from mojo.extensions import getExtensionDefault
    except ImportError:
        return fallback
    try:
        value = getExtensionDefault(PREFIX + key, None)
    except Exception:
        return fallback
    return fallback if value is None else value


def set_default(key, value):
    try:
        from mojo.extensions import setExtensionDefault
    except ImportError:
        return
    try:
        setExtensionDefault(PREFIX + key, value)
    except Exception:
        print(traceback.format_exc())


def message(text, title="Kinetikern2"):
    """A message to the user (a dialog in RoboFont, a print elsewhere)."""
    try:
        import vanilla.dialogs
        vanilla.dialogs.message(messageText=title, informativeText=text)
    except Exception:
        print("%s: %s" % (title, text))


def show_output():
    """Brings RoboFont's Output window (where tracebacks go) to the front."""
    try:
        from mojo.UI import OutputWindow
        window = OutputWindow()
        if window is not None:
            window.show()
    except Exception:
        pass


def open_text(font, text):
    """Opens a Space Center of `font` showing `text` ("/A/V /H/A/V/H …")."""
    from mojo.UI import OpenSpaceCenter
    space_center = OpenSpaceCenter(font)
    try:
        space_center.setRaw(text)
    except AttributeError:
        names = [n for n in text.replace(" ", "/space/").split("/") if n]
        space_center.set(names)
    return space_center


def pair_text(left, right):
    """The text a pair is opened with: the pair alone, between H, between o."""
    return "/%s/%s /H/%s/%s/H /o/%s/%s/o" % (left, right, left, right, left, right)
