# encoding: utf-8
"""
kk2_args — the parameters of unattended runs (build.sh --verify, tools).

A test instance of RoboFont is started with its parameters on the command
line (`open -n -a RoboFont --args -com.mirkovelimirovic.Kinetikern2.<key>
<value>`), which puts them into the argument domain of the user defaults.
They are read from that domain only: the extension defaults are persistent
preferences, and a key that ended up there would turn every normal launch of
the user's RoboFont into a test run. kk2_startup, kk2_window and kk2_selftest
all ask here, so they agree on what an unattended launch is.
"""

from __future__ import division, print_function, unicode_literals

import os
import sys

from Foundation import NSArgumentDomain, NSUserDefaults

# RoboFont puts an extension's lib folder on sys.path while one of its
# scripts runs and restores sys.path afterwards. What is imported later must
# therefore be imported while a script runs (kk2_window imports its windows
# up front) or after keep_path() put the folder back outside a script (the
# self-test, started from a timer). Appended: it never shadows another
# extension's modules.
LIB = os.path.dirname(os.path.abspath(__file__))


def keep_path():
    """Puts the extension's lib folder on sys.path (to stay there, call it
    outside a script RoboFont runs: from a timer or a callback)."""
    if LIB not in sys.path:
        sys.path.append(LIB)


keep_path()

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
