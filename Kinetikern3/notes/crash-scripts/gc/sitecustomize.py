# Loaded by Python at startup in a temporary Glyphs instance only (open --env
# PYTHONPATH=<this folder>): a background thread that watches sys.modules from
# the first second of the launch.
#   KK2_SITE_HAMMER=1    also take and drop a reference to every module, again and again
#   KK2_SITE_ABORT=1     abort on the first bad entry (every thread's native stack in a crash report)
#   KK2_SITE_CONTINUE=1  log each bad entry once and keep watching (default: exit 70)
#   KK2_SITE_HOLD=a,b    create these modules in sys.modules now and keep a reference to each
#                        (a plugin loader's PyImport_AddModule then returns them), see HELD
#   KK2_SITE_CHECK_AT=s  at s seconds: alias check of the plugin modules (aliascheck.txt), the
#                        texts of a modal window, if any (alert.txt), then "done" and exit 0
# Every new "__main__..." key (a Python plugin's module) is logged with the time
# it appeared and the address it points to.
import os, sys, threading, time, traceback, types

OUT = os.environ.get("KK2_SITE_OUT", "/tmp")
HAMMER = os.environ.get("KK2_SITE_HAMMER") == "1"
CONTINUE = os.environ.get("KK2_SITE_CONTINUE") == "1"
CHECK_AT = float(os.environ.get("KK2_SITE_CHECK_AT") or 0)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scan"))
T0 = time.time()
HELD = {}
for _name in filter(None, os.environ.get("KK2_SITE_HOLD", "").split(",")):
    HELD[_name] = sys.modules[_name] = types.ModuleType(_name)


def _log(line):
    with open(os.path.join(OUT, "site.txt"), "a") as f:
        f.write("%.3f %s\n" % (time.time() - T0, line))


VICTIM = "__main__com_weiweihuanghuang_ShowDistanceAndAngleInCorner"


def _aliascheck(kk2scan):
    lines = []
    bad, n = kk2scan.scan([sys.modules])
    badkeys = {b[2] for b in bad}
    for (_c, i, key, addr, reason, dump) in bad:
        lines.append("BAD entry %d key %r -> 0x%x reason %d dump %s" % (i, key, addr, reason, dump))
    lines.append("%d entries, %d bad" % (n, len(bad)))
    by_id = {}
    for k in [k for k in list(sys.modules) if k.startswith("__main__") and k not in badkeys]:
        v = sys.modules[k]
        by_id.setdefault(id(v), []).append(k)
        if k == VICTIM:
            lines.append("victim key -> 0x%x type %s __name__ %r refs %d"
                         % (id(v), type(v).__name__, getattr(v, "__name__", None), sys.getrefcount(v) - 2))
        del v
    lines.append("plugin module keys: %d, objects: %d" % (sum(len(ks) for ks in by_id.values()), len(by_id)))
    for a, ks in by_id.items():
        if len(ks) > 1:
            o = sys.modules[ks[0]]
            lines.append("SHARED 0x%x (%s, __name__ %r): %r" % (a, type(o).__name__, getattr(o, "__name__", None), ks))
            del o
    refs = {}
    for a, ks in by_id.items():
        o = sys.modules[ks[0]]
        refs.setdefault(sys.getrefcount(o) - 2, []).append(ks[0])
        del o
    for r in sorted(refs):
        lines.append("refs %d: %d modules, e.g. %r" % (r, len(refs[r]), refs[r][:4]))
    for name in sorted(HELD):
        m = HELD[name]
        lines.append("HELD %s: refs %d (sys.modules + HELD = 2 before any loader), still sys.modules[name]: %s, has __file__: %s"
                     % (name, sys.getrefcount(m) - 2, sys.modules.get(name) is m, "__file__" in m.__dict__))
        del m
    with open(os.path.join(OUT, "aliascheck.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _read_modal():
    """Ask the main thread, in the common run-loop modes (they run during a modal
    loop), for the texts of the modal window."""
    import objc
    from Foundation import NSObject, NSRunLoopCommonModes
    from AppKit import NSApp, NSTextField

    class KK2SiteModalReader(NSObject):
        def read_(self, _):
            try:
                w = NSApp.modalWindow()
                texts = []
                def walk(v):
                    if isinstance(v, NSTextField) and v.stringValue():
                        texts.append(str(v.stringValue()))
                    for s in v.subviews():
                        walk(s)
                if w is not None:
                    walk(w.contentView())
                with open(os.path.join(OUT, "alert.txt"), "w") as f:
                    f.write("modal window: %r\n%s\n" % (w.title() if w is not None else None, "\n---\n".join(texts)))
            except Exception:
                with open(os.path.join(OUT, "alert.txt"), "w") as f:
                    f.write(traceback.format_exc())

    reader = KK2SiteModalReader.alloc().init()
    reader.performSelectorOnMainThread_withObject_waitUntilDone_modes_("read:", None, False, [NSRunLoopCommonModes])
    return reader


def _watch():
    try:
        import kk2scan
    except Exception as e:
        _log("no scanner: %r" % e)
        return
    main = threading.main_thread().ident
    n = 0
    seen = set(sys.modules)
    reported = set()
    _log("watching (hammer %s, continue %s, holding %s)" % (HAMMER, CONTINUE, sorted(HELD)))
    while True:
        n += 1
        bad, _k = kk2scan.scan([sys.modules])
        badkeys = {b[2] for b in bad}
        new = [k for k in list(sys.modules) if k not in seen]
        for k in new:
            seen.add(k)
            if k.startswith("__main__"):
                _log("new %s -> %s" % (k, "BAD" if k in badkeys else "0x%x" % id(sys.modules[k])))
        if bad and os.environ.get("KK2_SITE_ABORT") == "1":
            # the main thread's native stack, as it is now, into a crash report
            _log("FOUND, aborting: %r" % [(b[2], b[4]) for b in bad[:3]])
            os.abort()
        fresh = [b for b in bad if b[2] not in reported]
        if fresh:
            keys = list(sys.modules.keys())
            lines = ["FOUND at scan %d" % n]
            for (_c, i, key, addr, reason, dump) in fresh[:6]:
                reported.add(key)
                lines.append("entry %d (%d from the end) key %r -> 0x%x reason %d dump %s" % (i, len(keys) - i, key, addr, reason, dump))
            frame = sys._current_frames().get(main)
            lines.append("main thread now:\n" + ("".join(traceback.format_stack(frame)) if frame is not None else "(no Python frame: in C)"))
            _log("\n".join(lines))
            if not CONTINUE:
                os._exit(70)
        if CHECK_AT and time.time() - T0 > CHECK_AT:
            try:
                _aliascheck(kk2scan)
            except Exception:
                _log("aliascheck failed:\n" + traceback.format_exc())
            try:
                keep = _read_modal()
                time.sleep(2.0)
            except Exception:
                _log("modal reader failed:\n" + traceback.format_exc())
            open(os.path.join(OUT, "done"), "w").close()
            time.sleep(0.5)
            os._exit(0)
        if HAMMER:
            for m in list(sys.modules.values()):
                pass
        time.sleep(0.002 if HAMMER else 0.005)


threading.Thread(target=_watch, name="kk2-site-watch", daemon=True).start()
