/* Heap-consistency scanner for CPython 3.11 (diagnostics only).
 * scan(objs) checks every value pointer held by the dicts, lists and tuples
 * in objs and returns [(container, index, key, address, reason, hexdump)]
 * for pointers that do not lead to a live object with a valid type. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <stdint.h>
#include <string.h>

typedef struct { Py_hash_t me_hash; PyObject *me_key; PyObject *me_value; } GenEntry;
typedef struct { PyObject *me_key; PyObject *me_value; } UniEntry;
typedef struct {
    Py_ssize_t dk_refcnt; uint8_t dk_log2_size; uint8_t dk_log2_index_bytes; uint8_t dk_kind;
    uint32_t dk_version; Py_ssize_t dk_usable; Py_ssize_t dk_nentries; char dk_indices[];
} Keys;
typedef struct { PyObject_HEAD Py_ssize_t ma_used; uint64_t ma_version_tag; Keys *ma_keys; PyObject **ma_values; } Dict;

#define LO 0x100000000ULL
#define HI 0x800000000000ULL
#define CACHE 65536
static uintptr_t good_types[CACHE];
#define PCACHE (1 << 18)
static uintptr_t good_pages[PCACHE];   /* 16 KiB pages known to be mapped */

static int page_ok(uintptr_t p, size_t n) {
    uintptr_t a = p >> 14, b = (p + n - 1) >> 14;
    for (uintptr_t pg = a; pg <= b; pg++) {
        size_t h = (pg * 0x9E3779B97F4A7C15ULL) >> 46;
        if (good_pages[h] == pg) continue;
        uint64_t tmp; mach_vm_size_t got = 0;
        if (mach_vm_read_overwrite(mach_task_self(), (mach_vm_address_t)(pg << 14), 8, (mach_vm_address_t)&tmp, &got) != KERN_SUCCESS)
            return 0;
        good_pages[h] = pg;
    }
    return 1;
}

static int safe_read(uintptr_t addr, void *out, size_t n) {
    mach_vm_size_t got = 0;
    kern_return_t kr = mach_vm_read_overwrite(mach_task_self(), (mach_vm_address_t)addr, n,
                                              (mach_vm_address_t)out, &got);
    return kr == KERN_SUCCESS && got == n;
}

static int type_ok(uintptr_t t) {
    if (t < LO || t >= HI || (t & 7)) return 0;
    size_t h = (t >> 4) & (CACHE - 1);
    if (good_types[h] == t) return 1;
    uint64_t hdr[2]; uint64_t flags;
    if (!safe_read(t, hdr, 16)) return 0;
    uintptr_t mt = (uintptr_t)hdr[1];
    if (mt < LO || mt >= HI || (mt & 7)) return 0;
    if (!safe_read(mt + 0xA8, &flags, 8)) return 0;
    if (!(flags & Py_TPFLAGS_TYPE_SUBCLASS)) return 0;
    if (!safe_read(t + 0xA8, &flags, 8)) return 0;   /* the type's own flags readable */
    good_types[h] = t;
    return 1;
}

/* 0: fine; else a reason code */
static int check(PyObject *v, char *dump) {
    uintptr_t p = (uintptr_t)v;
    if (v == NULL) return 0;
    if (p < LO || p >= HI || (p & 7)) return 1;          /* not a heap pointer */
    uint64_t hdr[4];
    if (!page_ok(p, 32)) return 2;                        /* unmapped */
    memcpy(hdr, (void *)p, 32);
    int r = 0;
    if (hdr[0] == 0xDDDDDDDDDDDDDDDDULL) r = 3;           /* freed (debug allocator) */
    else if ((int64_t)hdr[0] <= 0 || hdr[0] > (1ULL << 40)) r = 4;   /* refcount */
    else if (!type_ok((uintptr_t)hdr[1])) r = 5;           /* type */
    if (r && dump) snprintf(dump, 120, "%016llx %016llx %016llx %016llx", hdr[0], hdr[1], hdr[2], hdr[3]);
    return r;
}

static int report(PyObject *out, PyObject *c, Py_ssize_t i, PyObject *key, PyObject *v, int r, const char *dump) {
    PyObject *k;
    if (key != NULL && check(key, NULL) == 0 && PyUnicode_Check(key)) { k = key; Py_INCREF(k); }
    else if (key != NULL && check(key, NULL) == 0 && PyLong_Check(key)) { k = key; Py_INCREF(k); }
    else if (key != NULL) k = PyUnicode_FromFormat("<key %p>", key);
    else { k = Py_None; Py_INCREF(k); }
    PyObject *t = Py_BuildValue("(OnNKis)", c, i, k, (unsigned long long)(uintptr_t)v, r, dump ? dump : "");
    if (t == NULL) return -1;
    int e = PyList_Append(out, t);
    Py_DECREF(t);
    return e;
}

static PyObject *scan(PyObject *self, PyObject *arg) {
    if (!PyList_Check(arg)) { PyErr_SetString(PyExc_TypeError, "list expected"); return NULL; }
    PyObject *out = PyList_New(0);
    if (!out) return NULL;
    char dump[128];
    Py_ssize_t n = PyList_GET_SIZE(arg), checked = 0;
    for (Py_ssize_t j = 0; j < n; j++) {
        PyObject *o = PyList_GET_ITEM(arg, j);
        PyTypeObject *tp = Py_TYPE(o);
        if (tp->tp_flags & Py_TPFLAGS_DICT_SUBCLASS) {
            Dict *d = (Dict *)o;
            Keys *k = d->ma_keys;
            if (k == NULL) continue;
            Py_ssize_t ne = k->dk_nentries;
            char *entries = (char *)k->dk_indices + ((size_t)1 << k->dk_log2_index_bytes);
            if (k->dk_kind != 0) {                        /* unicode keys */
                UniEntry *e = (UniEntry *)entries;
                for (Py_ssize_t i = 0; i < ne; i++) {
                    PyObject *v = d->ma_values ? d->ma_values[i] : e[i].me_value;
                    int r = check(v, dump); checked++;
                    if (r && report(out, o, i, e[i].me_key, v, r, dump) < 0) goto fail;
                }
            } else {
                GenEntry *e = (GenEntry *)entries;
                for (Py_ssize_t i = 0; i < ne; i++) {
                    if (e[i].me_value == NULL) continue;
                    int r = check(e[i].me_value, dump); checked++;
                    if (r && report(out, o, i, e[i].me_key, e[i].me_value, r, dump) < 0) goto fail;
                    r = check(e[i].me_key, dump);
                    if (r && report(out, o, -1 - i, NULL, e[i].me_key, r, dump) < 0) goto fail;
                }
            }
        } else if (tp->tp_flags & Py_TPFLAGS_LIST_SUBCLASS) {
            PyListObject *l = (PyListObject *)o;
            for (Py_ssize_t i = 0; i < Py_SIZE(l); i++) {
                int r = check(l->ob_item[i], dump); checked++;
                if (r && report(out, o, i, NULL, l->ob_item[i], r, dump) < 0) goto fail;
            }
        } else if (tp->tp_flags & Py_TPFLAGS_TUPLE_SUBCLASS) {
            PyTupleObject *t = (PyTupleObject *)o;
            for (Py_ssize_t i = 0; i < Py_SIZE(t); i++) {
                int r = check(t->ob_item[i], dump); checked++;
                if (r && report(out, o, i, NULL, t->ob_item[i], r, dump) < 0) goto fail;
            }
        }
    }
    return Py_BuildValue("(Nn)", out, checked);
fail:
    Py_DECREF(out);
    return NULL;
}

static PyObject *addr_info(PyObject *self, PyObject *arg) {
    unsigned long long a = PyLong_AsUnsignedLongLong(arg);
    if (PyErr_Occurred()) return NULL;
    char dump[128] = "";
    int r = check((PyObject *)(uintptr_t)a, dump);
    return Py_BuildValue("(is)", r, dump);
}

static PyMethodDef methods[] = {
    {"scan", scan, METH_O, "scan(list) -> ([(container, index, key, address, reason, dump)], values checked)"},
    {"addr_info", addr_info, METH_O, "addr_info(address) -> (reason, dump)"},
    {NULL, NULL, 0, NULL}};
static struct PyModuleDef mod = {PyModuleDef_HEAD_INIT, "kk2scan", NULL, -1, methods};
PyMODINIT_FUNC PyInit_kk2scan(void) { return PyModule_Create(&mod); }
