# devScript: the self-test with the scanner (scanwrap.py), plus background
# Python threads doing what Font Proofer Companion's debounce timers do
# (iterate Glyphs.fonts and read document/font properties) in a loop.
import os, threading, time, random, traceback
from GlyphsApp import Glyphs
import kk2_args
_OUT = kk2_args.text("selfTestOut")
_N = int(kk2_args.number("ampThreads", 2))
_flog = open(os.path.join(_OUT, "fpcamp.txt"), "w", buffering=1)
_count = [0, 0]

def _amp(k):
    while True:
        try:
            for f in Glyphs.fonts:
                _ = (f.familyName, f.filepath, len(f.masters), f.parent)
            _count[0] += 1
        except Exception:
            _count[1] += 1
            if _count[1] < 5:
                _flog.write(traceback.format_exc())
        time.sleep(random.uniform(0.0005, 0.005))

for _k in range(_N):
    threading.Thread(target=_amp, args=(_k,), name="kk2-fpc-amp-%d" % _k, daemon=True).start()
_flog.write("%d amplifier threads started\n" % _N)

def _report():
    while True:
        time.sleep(10)
        _flog.write("%.0f iterations %d errors %d\n" % (time.time(), _count[0], _count[1]))
threading.Thread(target=_report, daemon=True).start()

exec(compile(open("/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad/crash/scanwrap.py").read(), "scanwrap.py", "exec"))
