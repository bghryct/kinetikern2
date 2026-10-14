# encoding: utf-8
"""
Kinetikern3 — Glyphs 3 plugin shim.

Filter ▸ Kinetikern3… opens the spacing and kerning window. All heavy work
runs in libkinetikern3.dylib on threads the library owns; this file only
registers the menu item and the unattended test hooks.

Everything this bundle puts into the shared Python interpreter and Objective-C
runtime carries a name of its own (kk3_* modules, KK3* classes, the
Kinetikern3Plugin principal class) so it can run next to Kinetic SDF Kerning
(v1) in the same Glyphs process.
"""

from __future__ import division, print_function, unicode_literals

import os
import sys
import traceback

import objc
from Foundation import NSTimer
from GlyphsApp import FILTER_MENU, Glyphs, Message
from GlyphsApp.plugins import GeneralPlugin, NSMenuItem

RESOURCES = os.path.dirname(os.path.abspath(__file__))
if RESOURCES not in sys.path:
    sys.path.append(RESOURCES)  # appended: never shadows another plugin's modules



class Kinetikern3Plugin(GeneralPlugin):

    @objc.python_method
    def settings(self):
        self.name = Glyphs.localize({"en": "Kinetikern3…", "de": "Kinetikern3…"})

    @objc.python_method
    def start(self):
        try:
            item = NSMenuItem(self.name, self.showKinetikern3_)
            item.setTarget_(self)
            Glyphs.menu[FILTER_MENU].append(item)
        except Exception:
            Message(traceback.format_exc(), title="Kinetikern3 menu error")
        # Unattended runs (build.sh --verify, tools): parameters arrive in the
        # argument domain (`open -n -a "Glyphs 3" --args -<key> <value>`) and
        # are read from there only (kk3_args), so a key in the preferences
        # never turns a normal launch into a test run.
        try:
            import kk3_args
            if kk3_args.unattended():
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    4.0, self, "runKinetikern3Unattended:", None, False)
        except Exception:
            print(traceback.format_exc())

    def runKinetikern3Unattended_(self, timer):
        try:
            import kk3_args
            script = kk3_args.text("devScript")
            if script:
                scope = {"__name__": "kk3_dev", "__file__": script, "Glyphs": Glyphs, "PLUGIN": self,
                         "RESOURCES": RESOURCES}
                with open(script) as f:
                    exec(compile(f.read(), script, "exec"), scope)
                return
            import kk3_selftest
            kk3_selftest.run(RESOURCES, self)
        except Exception:
            print(traceback.format_exc())

    def showKinetikern3_(self, sender):
        try:
            import kk3_window
            self._window = kk3_window.open_window(RESOURCES)
        except Exception:
            print(traceback.format_exc())
            Glyphs.showMacroWindow()

    @objc.python_method
    def __file__(self):
        """Please leave this method unchanged"""
        return __file__
