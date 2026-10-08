import sys, os, faulthandler, json
out = {}
out["faulthandler"] = faulthandler.is_enabled()
try:
    import _testcapi
    out["allocator"] = _testcapi.pymem_getallocatorsname()
except Exception as e:
    out["allocator"] = repr(e)
out["flags"] = str(sys.flags)
out["env"] = {k: v for k, v in os.environ.items() if k.startswith(("PYTHON", "Malloc"))}
out["version"] = sys.version
import gc
out["gc_threshold"] = gc.get_threshold()
out["gc_count"] = gc.get_count()
out["n_objects"] = len(gc.get_objects())
out["modules"] = len(sys.modules)
import kk2_args
d = kk2_args.text("selfTestOut")
with open(os.path.join(d, "devcheck.json"), "w") as f:
    json.dump(out, f, indent=1)
open(os.path.join(d, "done"), "w").write("1")
print("devcheck", out, file=sys.stderr)
from AppKit import NSApp
from PyObjCTools import AppHelper
AppHelper.callLater(0.5, NSApp().terminate_, None)
