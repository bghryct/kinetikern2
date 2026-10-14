import json, sys
p = sys.argv[1]
txt = open(p).read()
first, rest = txt.split('\n', 1)
d = json.loads(rest)
for im in d['usedImages']:
    path = im.get('path','')
    if '/System/' in path or '/usr/lib' in path: continue
    print(hex(im.get('base',0)), im.get('name'), path)
print({k: (v if len(str(v))<300 else str(v)[:300]) for k,v in d.items() if k not in ('threads','usedImages','legacyInfo','vmSummary')})
