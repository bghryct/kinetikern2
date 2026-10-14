# Glyphs 3 crash during the Kinetikern2 self-test: investigation notes

Status: both crashes are explained. The crash at font close is mitigated in the self-test (see *13 October*). The GC crash comes from another plugin's outdated loader (see *13 October, evening* at the end). First paused on 2026-10-06, about 21:45. I made no changes to the Kinetikern2 code, the engine or build.sh, so nothing needs reverting. All instrumentation lives in the session scratchpad (see "Tooling"). The temporary font copy has been deleted. No temporary Glyphs instance is running. The user's Glyphs 3 (pid 87931) was never touched.

## TL;DR

* There are **two different crash bugs**. Neither has been traced to Kinetikern2 code.
  1. **GC crash** (the two crash reports from earlier today): `visit_decref ← dict_traverse ← gc_collect_main`, during a **full (generation-2) collection**. A str-keyed dict holds a pointer to a freed object. **Not reproduced** in 25 runs here, and the root cause is still open. The first of the two reports came from **v1 (Kinetic SDF Kerning)**, not Kinetikern2. *Solved on 13 October:* the dict is `sys.modules`, and the plugin "Show Distance And Angle Of Nodes In Corner" frees its own module at every launch (see *13 October, evening*).
  2. **PyObjC 10.2 nil-argument bug** (new, reproduced once, root cause proven): a crash in `method_stub → PyObjCMethodSignature_WithMetaData → PyObjCRT_SkipTypeSpec(0x388)` when the self-test closes the font. Glyphs calls each Python palette's `setWindowController_(None)` (inherited from GlyphsApp's `PalettePlugin`; not Waterfall, as first thought: see *13 October* below). PyObjC 10.2 then wrongly treats `None` as a block. Fixed upstream in PyObjC 11.0. This is not Kinetikern2.
* Font Proofer Companion (a compiled Cython plugin) runs Python on **background `threading.Timer` threads**. Those threads call `Glyphs.fonts` / `doc.font` whenever a font opens or closes. They are the only background Python threads in the test instance. This is the leading suspect for the GC crash, but it is **unproven**: the experiment for it is written and has not been run yet. *(Superseded on 13 October: FPC is not involved.)*

## Environment

macOS 14.8.7, iMac21,1 (arm64). Glyphs 3.5.1 (3534) with its bundled Python 3.11.9 and **PyObjC 10.2**, plus about 90 plugins. The v1 plugin "Kinetic SDF Kerning" is also installed and loads in every instance.

## Crash reports

| Report | Plugin that ran | Signature | Key facts |
|---|---|---|---|
| `Glyphs 3-2026-10-06-125530.ips` | **v1** (only `libkinetic_kerning.dylib` loaded; no `libkinetikern2`) | GC: `visit_decref+16 ← dict_traverse+100 ← gc_collect_main+244 ← _PyObject_GC_Link ← PyObjC_CreateRegisteredStruct ← call_NSBezierPath_elementAtIndex_associatedPoints_` | Reached from `__NSFireDelayedPerform` → v1 `FontSnapshot.__init__`. v1's self-test had just applied, closed and reopened its window. |
| `Glyphs 3-2026-10-06-204008.ips` | v2 (Kinetikern2) | GC: same frames, then `← PyList_New ← generator (gen_send_ex2) ← builtin_next ← NSTimer` | Happened about 70 s into the test, during the Apply planner or stepper. No Python thread was alive at crash time. |
| `Glyphs 3-2026-10-06-211637.ips` (my run P1_5; copy in `runs/P1_5_lato/`) | v2 | `PyObjCRT_SkipTypeSpec+48 ← new_methodsignature ← PyObjCMethodSignature_WithMetaData ← method_stub ← Glyphs+0x870ec ← -[NSViewController _sendViewWillDisappear] ← … -[NSDocument close] ← GSFont.close ← kk2_selftest.finish` | EXC_BAD_ACCESS at 0x388. faulthandler shows `kk2_selftest.py:1109 finish → font.close`. A Python `threading.Timer` thread was waiting at the time. |

### Decoding the GC crash (reports 1 and 2)

* `dict_traverse+100` is the return address in the branch for **unicode-keyed, combined-table dicts** (it visits `entries[i].me_value`). I confirmed this by disassembling the bundled `Python` binary.
* Register x22 (entries still to visit) was **64 in report 1 and 82 in report 2**. The dict therefore had at least that many entries: a large dict with str keys, such as a module, class or registry dict (`sys.modules` has about 900 entries) or a large data dict.
* Register x23 (`generation`, kept from `gc_collect_main`) was **2 in both reports**. Both crashes happened in a **full collection**. A full collection runs only about once per self-test run, during Apply planning ("plan: existing/kerning"). The corruption could therefore have happened at any earlier point after launch.
* The faulting address equals `ob_type + 0xA9`, i.e. the `HAVE_GC` byte of `tp_flags`, and in both reports `ob_type` looks like a random 64-bit hash: `0x5F1E549B175E8EF8` and `0xF28C3C85B3E5A3BB`. The likeliest reading is that a **GC-tracked object was freed while the dict still referenced it**, and its pymalloc block was then reused by a str whose cached hash sits where `ob_type` used to be (a GC object starts 16 bytes into its block). This is a use-after-free from a refcount underflow or a double free, not a wild write.
* The dict object addresses were low (`0x107F8D140` and `0x10FC94640`) and the dangling values high. That fits an **old, long-lived dict** that received a newer value.

### Decoding crash 3: a PyObjC 10.2 bug

* In `libffi_support.m` of PyObjC 10.2 (I checked tag `v10.2` in a clone of the upstream repo), `method_stub` runs this for **every** argument that `pythonify_c_value` converts:
  `if (PyObjCObject_IsBlock(v) && PyObjCObject_GetBlock(v) == NULL) … PyObjCBlock_GetSignature(v)`.
  It does this without checking `PyObjCObject_Check(v)`, and it passes the Python object rather than the block. PyObjC 11.0 fixed both: `PyObjCObject_Check(v) && …` and `PyObjCBlock_GetSignature(PyObjCObject_OBJECT(v))`.
* For `v = None`, the static memory layout always passes the first two tests. Bit 0x40 is set at `None+24`, which holds the address `&PyType_Type` (…d68). `None+32`, which is `_PyNone_Type.ob_size`, is 0.
* The rest depends on two things. **None's refcount must be even**: `PyObjCBlock_GetSignature` returns early when the "isa" value is odd. And **bits 30 and 25 must both be set in the low word of `&_PyNone_Type`**, which depends on where libpython was loaded (ASLR). When both hold, the code reads `PyType_Type.tp_basicsize` (0x388 = 904) as a type-string pointer and crashes. In report 3, `x0 = 0x388` and `x27 = &_Py_NoneStruct`.
* I computed the bits per launch from the Python base address in each report. In report 3, the bits were set for every builtin type. In reports 1 and 2 they were **clear**. So this bug does not explain the GC crashes: with the bits clear, every static-typed argument returns early.
* The caller: `Glyphs 3 +0x87054..0x870e8` loops over plugin objects. If an object responds to `setWindowController:`, Glyphs leaves a Bugsnag breadcrumb and then sends `setWindowController:nil`. The only Python implementation among the installed plugins is in `Plugins/Waterfall.glyphsPlugin/Contents/Resources/plugin.py:315` (a GeneralPlugin). Any document close in an "unlucky-slide" launch can hit this, including in the user's own Glyphs.

## Runs (all in a temporary second instance; argument-domain keys as in build.sh)

| Set | Configuration | Runs | Result |
|---|---|---|---|
| devcheck | Environment check only. `PYTHONMALLOC=debug` and `PYTHONFAULTHANDLER=1` do reach the embedded Python (`pymalloc_debug`, faulthandler on). About 331k GC-tracked objects and 908 modules at startup. | 1 | ok |
| d1 | Self-test, Lato, debug allocator + faulthandler | 1 | PASS |
| g1, L1_1–8 | Self-test with **`gc.collect()` every 0.25 s** (gcstress.py), debug allocator, Lato/Arial alternating | 9 | 9 PASS (about 300 full collections per run, no crash) |
| P1_1–5 | Plain self-test (like build.sh --verify) + faulthandler, Lato/Arial alternating | 5 | 4 PASS, **1 crash (P1_5, Lato) = crash 3** |
| s0 | Self-test with heap scanner every 0.5 s + per-tick scans | 1 | FAIL by timeout. The instrumentation was too heavy; this is not a crash. |
| t1 | Thread probe: open and close Lato, sample other threads' Python stacks every 2 ms | 1 | Found `Thread-1`/`Thread-3` (threading.Timer) running `GlyphsApp … AppFontProxy.values` → `doc.font` and `Proxy.__len__/__iter__`, i.e. `Glyphs.fonts`, about 2 s after open and about 0.3 s after close |
| S1_1–5 | Plain self-test + **heap scanner** (scanwrap.py) at every gen-1/gen-2 GC start and a full scan every 5 s | 5 | 5 PASS, every scan clean |

Totals: 21 completed self-tests (plus one run that timed out because of instrumentation), with 1 crash (crash 3, not Kinetikern2) and **0 GC crashes**. Before this session, the GC crash rate was about 1 in 6 (Arial 4 passes; Lato 1 crash, 1 pass).

## Hypotheses

Ruled out or very unlikely:
* **Kinetikern2 engine or ctypes misuse.** The engine copies its inputs synchronously (`read_inputs`, the mask `to_vec`, `read_params`). It writes into Python memory only synchronously, into buffers Python owns and sizes correctly (`kk2_job_poll`, `kk2_context_rays`, `kk2_result_values`). `ResultBox` is `#[repr(C)]` with `c` as its first field. Every `free` is guarded by `ptr = None`. The Python side only ever reads the zero-copy views. A view read after free would give garbage values, not heap corruption. And report 1 came from **v1**, which does not load this engine.
* **Self-test gc instrumentation** (`gc.callbacks`, `len(gc.get_objects())`): nothing is mid-deallocation when they run, and v1, which has no such code, crashed the same way.
* **Quadratic NSBezierPath elements**: ruled out before this session. Note, though, that PyObjC 10.2 returns only one point for `NSBezierPathElementQuadraticCurveTo`, so `kk2_snapshot.bezier_contours` would raise IndexError at `points[1]` if a quadratic ever appeared.
* **A PyObjC proxy's GC object being visible during `object_dealloc`**: disproved by experiment. PyObjC proxy types use `subtype_dealloc`, and `objc_object` is not a GC type, so the proxy is already untracked when `CFRelease` runs.
* **NSCell copies sharing `__dict__`**: PyObjC adds `copyWithZone:` and deep-copies `__dict__` (`object_method_copyWithZone_`).
* **The `objc.super` dealloc in 10.2 that does not untrack** (removed in 10.2.1): benign without `tp_clear`.
* **PyObjC 10.2's `forwardInvocation:` return-buffer overflow** (fixed in 10.2.1): it hides inside pymalloc's 16-byte granularity, and the debug allocator never reported a bad pad byte.
* **The PyObjC nil-argument bug as the cause of the GC crashes**: the address bits were clear in reports 1 and 2 (see above).

Still open (all three superseded on 13 October by the loader finding at the end):
* **H1 (leading): a cross-thread race involving Font Proofer Companion.** Its debounce `threading.Timer` threads touch `NSDocumentController`, `GSDocument` and `GSFont` from a background thread while the main thread opens or closes documents. PyObjC releases the GIL around every ObjC call, so interleaving is real. Under this hypothesis, a race in PyObjC 10.2 (for example upstream issue #619, a race in class-proxy creation, fixed in 10.3.1) or an ObjC over-release of objects that hold Python references would leave a dangling Python reference, and the next full GC would crash. This fits: report 1 (v1) and report 2 (v2) both loaded fpcompanion, and the mock smoke test (no Glyphs, no FPC) never crashed. Weakness: the rate under my conditions is low or zero, and the corrupting event itself has not been observed.
* **H2: a rare write through the PyObjC 10.2 block-check path** (`PyObjCObject_SET_BLOCK(v, sig)` into a non-PyObjC object at `v+32`). It needs an even refcount, the address bits set, and garbage that happens to parse as a type string. Unlikely.
* **H3: something specific to timing or CPU load during the user's earlier runs** (for example builds or smoke tests running in parallel), which would make races more likely.

## Fix candidates and mitigations (none applied)

1. **Root fix for crash 3:** PyObjC ≥ 11.0 in Glyphs' Python. This is Glyphs' bundle: report it to Glyphs (Georg Seifert) together with the `method_stub` analysis above. A workaround on the Waterfall side is also possible, but Waterfall is a third-party plugin.
2. **kk2_selftest hardening (optional, low risk):** write `selftest.json` *before* `font.close()` (and log "closing font"), so a crash in another plugin at close cannot lose the verdict. Also record in the report whether the launch is "armed" for the nil bug (`id(type(None)) & 0xFFFFFFFF` has bits 30 and 25 set).
3. **build.sh --verify:** compare `~/Library/Logs/DiagnosticReports/Glyphs 3-*.ips` before and after the run, and print any new report together with its top frames, so a crash in the temporary instance is never silent.
4. If H1 is confirmed: report it to the Font Proofer Companion author (Peter Nowell). Mitigation for the self-test: stop FPC's companion in the test instance (`FontProoferCompanion.stop()` and cancelling its `threading.Timer`s) before opening the font. Kinetikern2 itself needs no change.

Because no code changed, the requested verification (py_compile, smoke test, `cargo test`, 5× `--verify`) was not run.

## Tooling (scratchpad; **/private/tmp, may be cleared on reboot**)

A copy of the scripts, the scanner source (`scan/kk2scan.c`) and the two crash reports (`ips1.txt`, `ips2.txt`) is kept in `Kinetikern2/notes/crash-scripts/`. The run outputs (`runs/`, about 18 MB) and the PyObjC clone were not copied. The scripts still contain the scratchpad paths, so point them at a new working folder before you rerun them.

`/private/tmp/claude-501/-Users-mirkovelimirovic-Desktop-KinetiKern/e47597b3-a4df-43ba-9d26-d6f84eb668cd/scratchpad/crash/`
* `run_verify.sh <label> <font> [open --env …] [-- extra args]`: build.sh's `open -n` command plus `--stdout`/`--stderr` capture, crash-report diff, and pgrep before and after. Modes: `DEVSCRIPT=…` (devScript only) and `WRAPSCRIPT=…` (devScript plus the self-test keys). Results go in `runs/<label>/`.
* `loop.sh <prefix> <n> plain|gc|wrap [open args]`: alternates Lato and Arial and stops at the first crash. `WRAP=<script>` selects the script for wrap mode.
* `scan/kk2scan.c` (+ the built `.so`): C extension for the 3.11 heap scanner. It checks every dict, list and tuple slot for freed memory, bad refcounts or a non-type `ob_type`.
* `scanwrap.py`: self-test + scanner (writes scan_report.txt with keys, referrers and stacks, then `os._exit(70)`).
* `gcstress.py`: self-test with `gc.collect()` every `-…gcInterval` seconds.
* `threadprobe.py`: samples background Python threads.
* `stress.py`: per-stage loops for bisection (`devMode snapshot|engine|window|apply|idle`); **not run yet**.
* `openclose.py`: open and close a font repeatedly without Kinetikern2 (`devMode fpc|nofpc`). It exits immediately in launches armed for the nil bug. **Not run yet.**
* `fpcamp.py`: self-test + scanner + 2 threads mimicking FPC's `Glyphs.fonts` reads in a tight loop. **Not run yet.**
* `parse_ips.py`: readable stacks from `.ips` files. `../pyobjc/` is a partial clone of upstream PyObjC (tags v10.2–v11.1).

## Separate finding: a hang (not a crash) opening a font through a relative symlink

2026-10-07: `build.sh --verify /tmp/…/Lato-Regular.ttf` never wrote selftest.json. A
`sample` of the temporary instance showed its document-opening thread looping in
`-[GSGlyphsInfo(InfoLoading) loadCustomIcons:]` → `-[NSString(SymlinksAndAliases)
stringByIterativelyResolvingSymlinkOrAlias]` (`/tmp` is the relative symlink
`private/tmp`), while the main thread waited for `Glyphs.open`. Fixed on our side
(`build.sh` passes `pwd -P`, the self-test opens `os.path.realpath`). Worth reporting
to Glyphs: opening any file under `/tmp/…` may hang Glyphs 3.

## Next steps to resume

1. Re-create the tooling if `/private/tmp` was cleared. The key pieces are `run_verify.sh` and `scan/kk2scan.c`. Build the scanner with:
   `clang -arch arm64 -O2 -shared -undefined dynamic_lookup -I"<GlyphsPythonPlugin>/Python.framework/Versions/3.11/include/python3.11" kk2scan.c -o kk2scan.cpython-311-darwin.so && codesign -f -s - kk2scan.cpython-311-darwin.so`
2. **H1 amplification:** `WRAPSCRIPT=$PWD/fpcamp.py ./run_verify.sh A1 <Lato> --env PYTHONFAULTHANDLER=1` (3–5 runs). A scan finding or GC crash means cross-thread Glyphs/PyObjC access corrupts the heap.
3. **H1 A/B without Kinetikern2:** `DEVSCRIPT=$PWD/openclose.py ./run_verify.sh OC_fpc <small TTF> -- -com.mirkovelimirovic.Kinetikern2.devMode fpc`, then the same with `nofpc`. Each run takes about 300 s, and openclose.txt logs which function each Timer thread runs.
4. Keep accumulating plain and scanner runs (`WRAP=$PWD/scanwrap.py ./loop.sh S2 10 wrap --env PYTHONFAULTHANDLER=1`), and also with CPU load in parallel (H3). With the scanner, the first GC-crash candidate will name the dict (its keys and owner).
5. Run the self-test on a `.glyphs` source. Copy one to scratch first, e.g. `~/Documents/Github/OswaldFont/sources/Oswald.glyphs`; the self-test then makes its own temp copy.
6. If H1 is confirmed, apply mitigation 4 to the self-test, and 2 and 3 regardless. Then verify: py_compile all modules, the smoke test (Arial default; Lato `--glyphs 3000 --max-pairs 30000`), and at least 5 clean `./build.sh --verify` runs across Lato and Arial with no new `.ips`.

## 13 October: the crash at font close, settled

Status: **explained; mitigated in the self-test.** The crash itself can only be fixed in Glyphs' Python.

* **What crashes.** PyObjC before 11.0 has this check in `method_stub` (`libffi_support.m`; the same code is in 9.0.1 and 10.2, fixed in 11.0):
  `PyObjCObject_IsBlock(v) && PyObjCObject_GetBlock(v) == NULL`. It runs on every argument of a method that a Python class implements, without checking that the argument is a PyObjC object. A nil argument arrives as `None`, so PyObjC reads the memory next to Python's None. Whether that crashes depends on two things:
  - **the launch**: bits 30 and 25 of the low word of `&_PyNone_Type`, which depend on where the Python framework was loaded;
  - **the moment**: None's reference count must be even.
  When both hold, PyObjC reads `PyType_Type.tp_basicsize` (0x388) as a pointer. Today's two reports (15:12 and 15:55) have x0 = 0x388, x27 = &_Py_NoneStruct and both bits set.
* **Who receives nil: the palettes, not Waterfall.** My conclusion of 6 October was wrong. A trace in a temporary instance (`scratchpad/crash/wctrace.py`: a profile hook logged every Python `setWindowController_` while a font opened and closed three times) shows the actual calls:
  - Waterfall's method was never called.
  - On open, Glyphs gives each Python palette of the window the window controller.
  - On close, it gives each palette `setWindowController_(None)`. Here that is GSDMATPalette, Distribute, SetPalette and TTControlInstructions: four calls per close.
  - They inherit the method from GlyphsApp's own `PalettePlugin` (`Scripts311/GlyphsApp/plugins.pyc`).
* **Rate.** Every crash report with this signature on this Mac (5: 6, 8, 12 and twice on 13 October) came from a temporary test instance. Each ran 1.5–2.5 min from launch to crash, and the user's own Glyphs has not crashed. The rate is about 1 test run in 8. The user's Glyphs is exposed all the same: in an unlucky launch, a font close can crash it.
* **Waterfall** (Toshi Omagari, installed build 15, upstream build 17 of July 2026) does implement `setWindowController_` in Python. The method stores the controller and never reads it again. Removing it would not help, since the palettes are the ones called.
* **RoboFont 4.4** bundles PyObjC 9.0.1, which has the same unguarded check. No RoboFont run has crashed this way: Python 3.9's memory next to None differs.
* **The fix: PyObjC 11 or later in Glyphs' Python.**
  - Glyphs 3.5.1 (3534) ships GlyphsApp for Python 3.8, 3.9, 3.11, 3.12 and 3.14 (`Contents/Scripts3xx`).
  - Its Plugin Manager lists only the Python 3.11 module (`py311`, PyObjC 10.2; `glyphs-packages` glyphs3 branch). The module's `py314` branch (Python 3.14.6, PyObjC 12.2.1, June 2026) exists upstream but is not listed for Glyphs 3.
  - Font Proofer Companion, the only plugin here with compiled modules, ships builds for 3.9–3.14.
  - Only standalone scripts would break on 3.14: four of Filipe Negrão's workflow scripts (`distutils`) and Wei Huang's html2text helper (`cgi`).
  - To report to Glyphs: update PyObjC in the `py311` module (PyObjC 11.1 supports 3.11), or list `py314` for Glyphs 3.
* **Mitigation in the self-test (both plugins).**
  - The test writes its verdict to `selftest-before-close.json` before it closes the font, then `selftest.json` as before.
  - `build.sh --verify` lists the crash reports from before the run. When the instance dies at the close, it reports the test from the earlier file.
  - `tools/kk2_crash_report.py` prints any new crash report's top frames and names this known cause; only this cause leaves the verdict standing, and any other crash fails the run.
  - `KK2_SETTINGS="selfTestCrashAtClose YES"` crashes the instance at the close on purpose, to test that path.

## 13 October, evening: the GC crash, solved

Status: **root cause proven.** It is not Kinetikern2, v1 or Font Proofer Companion. One installed plugin's outdated loader frees its own Python module while Glyphs starts. The fix belongs in that plugin.

* **The dict is `sys.modules`.** A C scanner thread (`kk2scan`, every 5 ms from launch) found one bad entry, always the same key: `__main__com_weiweihuanghuang_ShowDistanceAndAngleInCorner`. That is the module of "Show Distance And Angle Of Nodes In Corner" by Wei Huang (`ShowDistanceAndAngleOfNodesInCorner.glyphsReporter`, GitHub `weiweihuanghuang/Show-Distance-And-Angle-Of-Nodes-In-Corner`).
* **Who frees it: the plugin's loader binary.** Every Python plugin bundle runs `Contents/MacOS/plugin`, a small loader from the GlyphsSDK plugin templates (derived from py2app's bundle template). The loader calls `PyImport_AddModule("__main__" + bundle id with dots as underscores)`, which returns a **borrowed** reference, and runs `plugin.py` in that module's dict.
  - 63 of the 68 loader bundles here share one build, arm64 UUID `9f173642…`, byte-identical in every bundle. It calls `Py_IncRef(module)` right after `PyImport_AddModule` and never releases it: arm64 +0x2888 loads `_Py_IncRefPtr` (0x8238), and x0 is still the module.
  - This plugin is the only one with build `216be8e8…`. Its last commit, of 2020-11-20, is "New binary for M1 and Glyphs 3 compatibility", and the local clone equals upstream HEAD. The build imports no `Py_IncRef`. After `fclose` it calls `Py_DecRef(module)`: arm64 +0x2a90 `ldr x8, [x27, #0x220]` (x27 = page 0x8000, so `_Py_DecRefPtr`), +0x2a94 `mov x0, x21` (the module from `PyImport_AddModule`), +0x2a98 `blr x8`. That releases the one reference `sys.modules` owns. The module is freed while `sys.modules` still points to it, **on every launch**.
  - The other two builds are correct: `773c06a1` (Inverter, ShowBlackFill, ShowNodeIndexes) and `702bd09f` (MetricsSolver) neither keep nor release the module, so its refcount stays at 1.
  - Kinetikern2, its GitHub clone and v1 ship the standard build `9f173642`: not affected.
* **Evidence** (temporary instances only; a `sitecustomize.py` arrives through `open --env PYTHONPATH=…`).
  - **No race.** Without any extra thread, the victim's key is already freed (refcount 0, `ob_type` still `PyModule_Type`) the first time the watcher can see it, 5.96–6.67 s after Python started (runs A01, C01, E01). The free happens inside the loader, while it holds the GIL. My earlier "race" reading came from runs with a refcount-churning thread, and it was wrong.
  - **The moment.** The instance was aborted the instant the scanner saw the freed entry (HA01). The main thread was then starting the next plugin's loader (`plugin` +0x2458, `NSIsSymbolNameDefined`), inside `-[NSBundle loadAndReturnError:]` ← Glyphs +0x30028 ← +0x30a04 ← +0x2c70c ← +0x2842c ← main-menu nib loading.
  - **The count** (F01). The watcher created three modules before the plugins loaded and kept a reference to each, so `PyImport_AddModule` returned those. After loading:

    | Module | Loader | References after loading |
    |---|---|---|
    | ShowDistanceAndAngle (markfromberg) | `9f173642` | 3 = sys.modules + mine + the loader's |
    | ShowDistanceBetweenTwoPoints (mekkablue) | `9f173642` | 3 |
    | **ShowDistanceAndAngleInCorner** | `216be8e8` | **1** = sys.modules + mine **− 1** |

    The extra reference kept the module alive: that run had no freed entry.
* **Why it crashes only sometimes.** The freed block (80 bytes, pymalloc) usually goes 1–8 ms later to the next plugin's module, ShowDistanceBetweenTwoPoints: the same size, and created next. Two keys then point to one live module (E01, E2_01: "SHARED"), with one reference too few. That is harmless as long as nobody removes those keys. If something else takes the block first (A01, C01), the key points to whatever lives there. Once that object dies, the next full collection walks `sys.modules` into freed memory: `visit_decref ← dict_traverse ← gc_collect_main`, generation 2. That is the signature of reports 1 and 2, and of the scanner's catch in B24 (1 launch in 24: the block reused by a str-like object, refcount 28, a hash where `ob_type` was). It fits the earlier decoding (a large str-keyed dict, an old dict holding a newer value, a str hash in `ob_type`). Any Glyphs launch with this plugin installed can crash this way, whether or not a test or Kinetikern2 runs; report 1 had only v1 loaded.
* **Side effect of my runs: two plugins flagged as crashed.**
  - Before it loads each plugin, Glyphs sets an extended attribute `CrashFlag` in two places:
    - on that plugin's entry in `Plugins/`, on the symlink itself (`XATTR_NOFOLLOW`, +0x309c8; read it with `xattr -s`);
    - on the `Plugins` folder (+0x261f4). The folder path is built from `applicationSupportPath` + "Plugins" and stored at +0x26298.

    After the plugin has loaded, Glyphs removes both (+0x30a6c, +0x2620c).
  - When the app dies while a plugin loads, both flags stay. The next launch reads the folder's flag (+0x2c614 `getxattr` → `setTempData:forKey:@"PluginHasCrashed"`). It skips every entry that still carries a flag (+0x308b4). It then shows "The last time Glyphs was started, the following plugins crashed", with the buttons OK, Remove and Ignore (error code 990, handler at +0x2f0b0). The next plugin that loads clears the folder's flag again; the entries' flags stay until someone answers the alert.
  - **OK** does nothing: the alert returns at every launch, and the plugins stay unloaded. **Remove** moves the plugins into `Plugins (Disabled)`. **Ignore** removes the flags (+0x2f6dc).
  - My runs HT01 and HA01 stopped the instance on purpose during plugin loading. `ShowHTLSPolygons.glyphsReporter` (CrashFlag "13") and `ShowHintsAlsoInOtherMasters.glyphsReporter` ("15") were therefore flagged. Both still loaded in B24, and every run from A01 to F01 skipped them. The cure is Ignore, or `xattr -s -d CrashFlag "…/Plugins/<name>"` for each.
  - A natural GC crash during plugin loading would flag an innocent plugin in the same way.
* **Fixes.**
  1. **The plugin.** Replace `Contents/MacOS/plugin` with the current loader. The standard one is generic (it reads `PyMainFileNames` from Info.plist) and ad-hoc signed, and neither bundle has sealed resources, so a copy from any other Python plugin works. Or uninstall the plugin. Either way, tell the author (the GitHub repo) to rebuild with the current GlyphsSDK loader.
  2. **Glyphs** (Georg Seifert), worth a report:
     - Glyphs could recognise this loader build (UUID `216be8e8`; its strings lack `Py_IncRef`) or check after each Python plugin loads that its `sys.modules` entry is alive.
     - `CrashFlag` blames whichever plugin was loading when Glyphs died. A heap corruption that crashes later blames an innocent plugin.
  3. **Our harness:** test instances must never be stopped during plugin loading. A check in `build.sh` could list new `CrashFlag`s after a run.
* **Done on this Mac** (user: "yes, clear the flags, fix the plugin and push").
  - Both `CrashFlag`s cleared (`xattr -s -d`). No entry in `Plugins/`, and not the folder, has one now.
  - The plugin's loader is replaced with the standard build: a byte-identical copy of `ShowDistanceAndAngleOfNodes`' loader, copied next to it and renamed into place. The original is in the plugin's git history: `git checkout -- ShowDistanceAndAngleOfNodesInCorner.glyphsReporter/Contents/MacOS/plugin` in its Repositories clone restores it. A Plugin Manager update that changes that file upstream would bring the old loader back.
  - Verified in temporary instances:
    - G01: the module is alive under its own name, with 2 references like the other standard-loader modules. 71 keys point to 71 objects, there are no bad entries and no alert, and ShowHTLSAreas and ShowHintsAlsoInOtherMasters load again.
    - H01, the hold test: 3 references, the same as the control (1 before the fix).
    - `./build.sh --verify` (Arial) passed. The instance then hit the known PyObjC close crash (`Glyphs 3-2026-10-13-194406.ips`). macOS wrote the report about 26 s after the instance died, after build.sh's 20 s wait, so build.sh warned "macOS wrote no crash report". A longer wait would close that gap.
* **Tooling** (scratchpad `crash/`, copied to `notes/crash-scripts/gc/`; the scratchpad paths are still inside):
  - `site/sitecustomize.py`: the startup watcher. Modes: `KK2_SITE_CONTINUE`, `KK2_SITE_HOLD=a,b`, `KK2_SITE_CHECK_AT=s` (alias check plus the texts of a modal window, then exit 0), `KK2_SITE_ABORT`, `KK2_SITE_HAMMER`.
  - `aliasloop.sh`: launches with the watcher.
  - `startscan.py`, `startloop.sh`: a scan after startup, as a dev script. A modal alert at startup blocks the plugin's dev-script timer, which is why the watcher does its own check.
  - `stub/annot3.py`: annotates a loader's disassembly with its `…Ptr` globals.
  - `decode.py`: names the `objc_msgSend` stubs, CFStrings and C strings in Glyphs' arm64 code.
