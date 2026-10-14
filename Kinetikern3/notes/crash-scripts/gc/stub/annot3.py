import re, sys
asm, nmf = sys.argv[1], sys.argv[2]
lo, hi = int(sys.argv[3], 16), int(sys.argv[4], 16)
syms = {}
for line in open(nmf):
    p = line.split()
    if len(p) == 3:
        syms[int(p[0], 16)] = p[2]
regs = {}
for line in open(asm):
    s = line.strip()
    m = re.match(r'^([0-9a-f]{16})\s+(\S+)\s*(.*)$', s)
    if not m:
        continue
    addr, op, args = int(m.group(1), 16), m.group(2), m.group(3)
    note = ''
    a = re.match(r'(x\d+),\s*(\d+)\s*;\s*0x([0-9a-f]+)', args)
    if op == 'adrp' and a:
        regs[a.group(1)] = int(a.group(3), 16)
    elif op == 'ldr' and re.match(r'x\d+,\s*#0x[0-9a-f]+$', args):
        t = addr + int(args.split('#')[1], 16)
        if t in syms:
            note = syms[t]
    else:
        b = re.match(r'(?:x|w|d|q)\d+,\s*\[(x\d+),\s*#0x([0-9a-f]+)\]', args)
        if b and b.group(1) in regs:
            t = regs[b.group(1)] + int(b.group(2), 16)
            note = syms.get(t, '0x%x' % t)
        c = re.match(r'(x\d+),\s*(x\d+),\s*#0x([0-9a-f]+)', args)
        if op == 'add' and c and c.group(2) in regs:
            t = regs[c.group(2)] + int(c.group(3), 16)
            note = syms.get(t, '0x%x' % t)
    if lo <= addr < hi:
        print('%05x  %-6s %-52s %s' % (addr, op, args[:52], ('; ' + note) if note else ''))
