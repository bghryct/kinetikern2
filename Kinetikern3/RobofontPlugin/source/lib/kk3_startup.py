# encoding: utf-8
"""
Run by RoboFont at launch. It does nothing at all, unless this is a test
instance started by build.sh --verify (its parameters in the argument domain,
see kk3_args): then the self-test (or a developer's script) starts a few
seconds later, once RoboFont has finished launching.

Another extension may greet a launch with an alert, which waits for a click
that nobody gives in a test instance (and holds every ordinary timer while it
is up): Mechanic's update check shows one at launch, or seconds later when the
network is slow. In a test instance a timer that also fires in modal panels
therefore dismisses any alert, before the test and while it runs (the
self-test reports each one).

Everything this extension puts into RoboFont's shared Python interpreter and
Objective-C runtime carries a name of its own (kk3_* modules, KK3* classes),
so it can run next to any other extension.
"""

from __future__ import division, print_function, unicode_literals

import traceback

START_DELAY_S = 5.0
DISMISSED = []  # the texts of alerts dismissed before the test started

_timers = []  # the start timer, kept alive until it fired


def _alert_text(window):
    from AppKit import NSTextField
    texts = []
    stack = [window.contentView()] if window is not None and window.contentView() is not None else []
    while stack:
        view = stack.pop()
        if isinstance(view, NSTextField):
            text = str(view.stringValue() or "").strip()
            if text:
                texts.append(text)
        stack.extend(view.subviews() or ())
    return " | ".join(texts)[:300] or "(no text)"


def _schedule(delay, fn=None):
    from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer
    fn = fn or _fire
    timer = NSTimer.timerWithTimeInterval_repeats_block_(delay, False, lambda t: fn())
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    _timers.append(timer)


def _dismiss_alert():
    """Dismisses an alert that is up (its text kept); True if there was one."""
    try:
        from AppKit import NSApp
        modal = NSApp().modalWindow()
        if modal is not None:
            DISMISSED.append(_alert_text(modal))
            NSApp().abortModal()
            return True
    except Exception:
        print(traceback.format_exc())
    return False


def _fire():
    del _timers[:]
    if _dismiss_alert():
        _schedule(1.0)  # once the alert is gone
        return
    # the watch first: an alert the test's own start raises (opening the font,
    # the window) would otherwise hold it before the watch begins
    _schedule(1.0, _watch)
    _run_unattended()


def _watch():
    """While the test runs: an alert that turns up later is dismissed too."""
    del _timers[:]
    _dismiss_alert()
    _schedule(1.0, _watch)


def _run_unattended():
    try:
        import kk3_args
        kk3_args.keep_path()  # outside the startup script: it stays
        resources = kk3_args.LIB
        script = kk3_args.text("devScript")
        if script:
            scope = {"__name__": "kk3_dev", "__file__": script, "RESOURCES": resources}
            with open(script) as f:
                exec(compile(f.read(), script, "exec"), scope)
            return
        import kk3_selftest
        kk3_selftest.run(resources, dismissed=DISMISSED)  # the list itself: it grows while the test runs
    except Exception:
        print(traceback.format_exc())


try:
    import kk3_args  # keeps the extension's lib folder on sys.path
    if kk3_args.unattended():
        _schedule(START_DELAY_S)
except Exception:
    print(traceback.format_exc())
