# encoding: utf-8
"""
kk3_ui — closing Kinetikern3's windows in Glyphs 3 and Glyphs 4 alike.

A vanilla window tells its "close" callbacks from windowWillClose_, which
NSWindow.close calls through NSWindowWillCloseNotification. In Glyphs 4 a
window's close does not always post that notification: the Kinetikern3
window was hidden and never told (Glyphs 4.2, macOS 26), so it kept its
engine result and context and stayed in kk3_window's list of open windows.
Glyphs 3 always posts it.

So each window runs its own close handler exactly once, whichever comes
first:

* "should close": the close button and Command-W (performClose:) ask the
  window before closing it;
* "close": the notification, when it comes;
* close(w) below, when Kinetikern3 closes a window itself: the handler
  first, then the window is closed and ordered out, whatever NSWindow.close
  does.
"""

from __future__ import division, print_function, unicode_literals

import traceback

_HANDLER = "_kk3_close_handler"


def on_close(w, callback):
    """Calls callback(sender) once when vanilla window `w` closes, by any way."""
    done = []

    def fire(sender=None):
        if done:
            return True
        done.append(True)
        try:
            callback(sender)
        except Exception:
            print(traceback.format_exc())
        return True  # "should close": let it close

    w.bind("should close", fire)
    w.bind("close", fire)
    setattr(w, _HANDLER, fire)


def close(w):
    """Closes vanilla window `w`: its close handler (on_close) runs first, then
    the window closes and is ordered out."""
    if w is None:
        return
    fire = getattr(w, _HANDLER, None)
    nsw = None
    try:
        nsw = w.getNSWindow()
    except Exception:
        nsw = None
    if fire is not None:
        fire(w)
    if nsw is None:
        return  # closed already
    try:
        w.close()
    except Exception:
        print(traceback.format_exc())
    try:
        if nsw.isVisible():
            nsw.orderOut_(None)
    except Exception:
        pass
