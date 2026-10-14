import json, sys
p = sys.argv[1]
txt = open(p).read()
first, rest = txt.split('\n', 1)
hdr = json.loads(first)
d = json.loads(rest)
print(hdr.get('app_version'), hdr.get('timestamp'))
print('exception', d.get('exception'))
print('termination', d.get('termination'))
print('vmregion', d.get('vmRegionInfo'))
print('faultingThread', d.get('faultingThread'))
imgs = d['usedImages']
def fr(f):
    im = imgs[f['imageIndex']]
    name = im.get('name') or im.get('path','?')
    sym = f.get('symbol', '')
    return f"{name:40s} {sym}+{f.get('symbolLocation','')}  off=0x{f['imageOffset']:x}"
for i, t in enumerate(d['threads']):
    print(f"--- thread {i} {t.get('name','')} {t.get('queue','')} {'TRIGGERED' if t.get('triggered') else ''}")
    if t.get('triggered'):
        print('state', json.dumps(t.get('threadState',{}))[:2000])
    for f in t['frames']:
        print('   ', fr(f))
