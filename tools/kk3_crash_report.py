#!/usr/bin/env python3
"""
kk3_crash_report — what crashed a temporary Glyphs or RoboFont during a
`build.sh --verify` run: the crash reports macOS wrote since the run began.

    kk3_crash_report.py BEFORE_LIST APP_PREFIX [--wait SECONDS] [--dir FOLDER]

BEFORE_LIST is a file with the names of the crash reports that were there
before the run, one per line; APP_PREFIX is "Glyphs 3" or "RoboFont". With
--wait the tool waits up to SECONDS for a new report (macOS writes it a few
seconds after the crash). For each new report it prints the time, the
exception and the crashing thread's top frames.

One cause is known to come from outside Kinetikern3. PyObjC before 11.0
checks whether an argument is a block before checking that it is a PyObjC
object (libffi_support.m, method_stub): when Objective-C passes nil to a
method that a Python class implements, it reads memory next to Python's
None and, in some launches (the address bits of NoneType, None's reference
count), dereferences 0x388. Glyphs 3.5's Python 3.11 module ships PyObjC 10.2,
and Glyphs sends setWindowController_(None) to each Python palette when a
font window closes, so closing a font can crash it. Kinetikern3 has no
palette. See notes/crash-investigation.md.

Exit status: 0 no new report, 3 only the known PyObjC crash, 4 another crash.
"""

import argparse
import json
import os
import sys
import time

REPORTS = os.path.expanduser("~/Library/Logs/DiagnosticReports")


def new_reports(before, prefix, folder=REPORTS):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(n for n in names if n.startswith(prefix + "-") and n.endswith(".ips") and n not in before)


def describe(path):
    """(lines, known) for one .ips report."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    head, _, body = text.partition("\n")
    try:
        info = json.loads(head)
        report = json.loads(body)
    except ValueError:
        return ["%s: not a JSON crash report" % os.path.basename(path)], False
    images = report.get("usedImages", [])
    threads = report.get("threads", [])
    index = report.get("faultingThread", 0)
    frames = threads[index].get("frames", []) if 0 <= index < len(threads) else []

    def frame(fr):
        i = fr.get("imageIndex")
        image = (images[i].get("name") if i is not None and 0 <= i < len(images) else None) or "?"
        symbol = fr.get("symbol")
        return "%s %s" % (image, symbol if symbol else "+0x%x" % fr.get("imageOffset", 0))

    exc = report.get("exception", {})
    symbols = [fr.get("symbol", "") for fr in frames[:8]]
    known = "PyObjCRT_SkipTypeSpec" in symbols and "method_stub" in symbols
    lines = ["%s: %s %s, %s" % (os.path.basename(path), exc.get("type", "?"), exc.get("subtype", ""),
                                info.get("timestamp", "")[:19])]
    lines += ["    " + frame(fr) for fr in frames[:8]]
    if known:
        lines.append("    known cause, outside Kinetikern3: PyObjC before 11.0 and a nil argument to a method a "
                     "Python class implements (closing a font: a palette's setWindowController_(None))")
    return lines, known


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("before")
    ap.add_argument("prefix")
    ap.add_argument("--wait", type=float, default=0.0)
    ap.add_argument("--dir", default=REPORTS, help="where the crash reports are (default: %(default)s)")
    a = ap.parse_args()
    try:
        with open(a.before, encoding="utf-8") as f:
            before = set(line.strip() for line in f if line.strip())
    except OSError:
        before = set()
    deadline = time.time() + a.wait
    found = new_reports(before, a.prefix, a.dir)
    while not found and time.time() < deadline:
        time.sleep(1.0)
        found = new_reports(before, a.prefix, a.dir)
    if not found:
        return 0
    all_known = True
    for name in found:
        lines, known = describe(os.path.join(a.dir, name))
        all_known = all_known and known
        print("crash report " + "\n".join(lines))
    return 3 if all_known else 4


if __name__ == "__main__":
    sys.exit(main())
