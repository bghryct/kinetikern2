# Dev script (DEVSCRIPT mode): right after Glyphs has started and loaded its
# plugins, and then every second for 20 s, sys.modules is scanned in C
# (kk2scan: an entry whose object is freed or has no valid type), touching no
# entry from Python. The first bad entry is written to startscan.txt and the
# instance exits; else "clean", and Glyphs quits.
import os, sys, time
from AppKit import NSApp
from Foundation import NSTimer
import kk2_args

S = "/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad"
sys.path.insert(0, S + "/crash/scan")
import kk2scan

OUT = kk2_args.text("selfTestOut")
T0 = time.time()
STATE = {"n": 0}
TIMERS = []


def write(lines, done=True):
    with open(os.path.join(OUT, "startscan.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    if done:
        open(os.path.join(OUT, "done"), "w").close()


def scan(timer=None):
    STATE["n"] += 1
    bad, n = kk2scan.scan([sys.modules])
    if bad:
        keys = list(sys.modules.keys())
        lines = ["FOUND at scan %d, %.1f s after the script started; %d entries" % (STATE["n"], time.time() - T0, len(keys))]
        for (_c, i, key, addr, reason, dump) in bad[:10]:
            lines.append("entry %d (%d from the end) key %r -> 0x%x reason %d dump %s"
                         % (i, len(keys) - i, key, addr, reason, dump))
        lines.append("neighbours: %r" % (keys[max(0, bad[0][1] - 4):bad[0][1] + 5],))
        write(lines)
        os._exit(70)
    if STATE["n"] >= 21:
        write(["clean: %d scans of %d entries" % (STATE["n"], n)])
        NSApp.terminate_(None)


scan()
TIMERS.append(NSTimer.scheduledTimerWithTimeInterval_repeats_block_(1.0, True, scan))
