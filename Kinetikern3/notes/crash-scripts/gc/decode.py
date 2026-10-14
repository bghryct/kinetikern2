import struct, sys, re
data = open(sys.argv[1], "rb").read()
ncmds = struct.unpack_from("<I", data, 16)[0]; off = 32; secs = {}
for _ in range(ncmds):
    cmd, size = struct.unpack_from("<II", data, off)
    if cmd == 0x19:
        nsects = struct.unpack_from("<I", data, off + 64)[0]
        for k in range(nsects):
            s = off + 72 + 80 * k
            sect = data[s:s+16].rstrip(b"\0").decode(); seg = data[s+16:s+32].rstrip(b"\0").decode()
            addr, sz, foff = struct.unpack_from("<QQI", data, s + 32)
            secs[(seg, sect)] = (addr, sz, foff)
    off += size
def at(v):
    for (seg, sect), (addr, sz, foff) in secs.items():
        if addr <= v < addr + sz:
            return foff + (v - addr), sect
    return None, None
def cstr(v):
    o, _ = at(v); e = data.index(b"\0", o); return data[o:e].decode("utf-8", "replace")
def name(v):
    o, sect = at(v)
    if o is None: return None
    try:
        if sect == "__objc_stubs":
            i1, i2 = struct.unpack_from("<II", data, o)
            immlo = (i1 >> 29) & 3; immhi = (i1 >> 5) & 0x7FFFF
            imm = ((immhi << 2) | immlo) << 12
            if imm & (1 << 32): imm -= 1 << 33
            selref = (v & ~0xFFF) + imm + ((i2 >> 10) & 0xFFF) * 8
            so, _ = at(selref)
            return "msgSend$" + cstr(struct.unpack_from("<Q", data, so)[0] & 0xFFFFFFFFFFFF)
        if sect == "__cfstring":
            p = struct.unpack_from("<Q", data, o + 16)[0] & 0xFFFFFFFFFFFF; n = struct.unpack_from("<Q", data, o + 24)[0]
            po, _ = at(p); return "@" + repr(data[po:po+n].decode("utf-8", "replace")[:70])
        if sect == "__cstring":
            return repr(cstr(v)[:70])
    except Exception:
        return None
    return None
lo, hi = int(sys.argv[3], 16), int(sys.argv[4], 16)
regs = {}
for line in open(sys.argv[2]):
    m = re.match(r'^([0-9a-f]{16})\s+(\S+)\s*(.*)$', line.strip())
    if not m: continue
    addr, op, args = int(m.group(1), 16), m.group(2), m.group(3)
    if not (lo <= addr < hi): continue
    a = re.match(r'(x\d+),\s*(\d+)\s*;\s*0x([0-9a-f]+)', args)
    if op == 'adrp' and a:
        regs[a.group(1)] = int(a.group(3), 16); continue
    note = None
    c = re.match(r'(x\d+),\s*(x\d+),\s*#0x([0-9a-f]+)', args)
    if op == 'add' and c and c.group(2) in regs:
        note = name(regs[c.group(2)] + int(c.group(3), 16))
    if op == 'bl':
        t = re.match(r'0x([0-9a-f]+)', args)
        if t:
            note = name(int(t.group(1), 16)) or (args.split(';')[1].strip().replace("symbol stub for: ", "") if ';' in args else "call " + t.group(0))
    if op in ('cmp', 'b.eq', 'b.ne', 'cbz', 'cbnz', 'ret') and lo <= addr < hi:
        note = note or (op + " " + args[:30])
    if note and 'objc_re' not in note and 'objc_retain' not in note:
        print("  %x %s" % (addr & 0xFFFFFFF, note))
