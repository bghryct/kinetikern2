# encoding: utf-8
"""
kk2_args — the parameters of unattended runs (build.sh --verify, tools).

A test instance of Glyphs is started with its parameters on the command line
(`open -n -a "Glyphs 3" --args -com.mirkovelimirovic.Kinetikern2.<key> <value>`),
which puts them into the argument domain of the user defaults. They are read
from that domain only: Glyphs.defaults also sees the persistent preferences,
and a key that ended up there would turn every normal launch of the user's
Glyphs into a test run. plugin.py, kk2_window and kk2_selftest all ask here,
so they agree on what an unattended launch is.
"""

from __future__ import division, print_function, unicode_literals

from Foundation import NSArgumentDomain, NSUserDefaults

PREFIX = "com.mirkovelimirovic.Kinetikern2."
UNATTENDED_KEYS = ("selfTestFont", "devScript")


def arguments():
    """The argument domain (`--args -key value`) as a dictionary, or None."""
    try:
        return NSUserDefaults.standardUserDefaults().volatileDomainForName_(NSArgumentDomain)
    except Exception:
        return None


def argument(key):
    """The value of PREFIX + key on the command line, or None."""
    args = arguments()
    if not args:
        return None
    return args.get(PREFIX + key)


def text(key):
    """An argument as str; None when missing or empty."""
    value = argument(key)
    return str(value) if value else None


def flag(key):
    """A boolean argument: `-key YES` arrives as the string "YES"."""
    value = argument(key)
    if value is None:
        return False
    if isinstance(value, (bool, int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "yes", "true", "y", "on")


def number(key, default):
    value = argument(key)
    try:
        return float(value) if value is not None and str(value).strip() else default
    except ValueError:
        return default


def unattended():
    """True in a test instance started with selfTestFont / devScript arguments."""
    return any(argument(key) for key in UNATTENDED_KEYS)
