# encoding: utf-8
"""
Run by RoboFont at launch. It does nothing at all, unless this is a test
instance started by build.sh --verify (its parameters in the argument domain,
see kk2_args): then the self-test (or a developer's script) starts a few
seconds later, once RoboFont has finished launching.

Another extension may greet a launch with an alert, which waits for a click
that nobody gives in a test instance (and holds every ordinary timer while it
is up). The start timer therefore also fires in modal panels, and in a test
instance it dismisses such an alert (the self-test reports it).

Everything this extension puts into RoboFont's shared Python interpreter and
Objective-C runtime carries a name of its own (kk2_* modules, KK2* classes),
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


def _schedule(delay):
    from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer
    timer = NSTimer.timerWithTimeInterval_repeats_block_(delay, False, lambda t: _fire())
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    _timers.append(timer)


def _fire():
    del _timers[:]
    try:
        from AppKit import NSApp
        modal = NSApp().modalWindow()
        if modal is not None:
            DISMISSED.append(_alert_text(modal))
            NSApp().abortModal()
            _schedule(1.0)  # once the alert is gone
            return
    except Exception:
        print(traceback.format_exc())
    _run_unattended()


def _run_unattended():
    try:
        import kk2_args
        kk2_args.keep_path()  # outside the startup script: it stays
        resources = kk2_args.LIB
        script = kk2_args.text("devScript")
        if script:
            scope = {"__name__": "kk2_dev", "__file__": script, "RESOURCES": resources}
            with open(script) as f:
                exec(compile(f.read(), script, "exec"), scope)
            return
        import kk2_selftest
        kk2_selftest.run(resources, dismissed=list(DISMISSED))
    except Exception:
        print(traceback.format_exc())


try:
    import kk2_args  # keeps the extension's lib folder on sys.path
    if kk2_args.unattended():
        _schedule(START_DELAY_S)
except Exception:
    print(traceback.format_exc())
