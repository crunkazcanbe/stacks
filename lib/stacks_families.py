#!/usr/bin/env python3
"""
Container Family Detector
3 detection methods:
1. Common name root (authentik-server + authentik-db share root 'authentik')
2. Direct prefix (coolify + coolify-realtime)
3. Shared private network with at least one name match
"""
import re, glob
from collections import defaultdict

STACKS_DIR = "/srv/stacks/Stacks"
GLOBAL_NETS = {'traefik_net','apartment_net','bridge','host','none',
               'ingress','docker_gwbridge'}
SKIP_CONTAINERS = {'provisioner','adminer','surrealist','cloudbeaver'}
DB_WORDS = {'db','redis','cache','postgres','mysql','mongo','mariadb',
            'worker','celery','cron','realtime','beat','scheduler',
            'daemon','rabbitmq','memcached','valkey','indexer'}

def is_support(name):
    parts = name.replace('_','-').split('-')
    return parts[-1] in DB_WORDS or any(w in name for w in
           {'postgres','mysql','mongo','redis','rabbitmq','memcached'})

def root(name):
    """Get first meaningful segment: authentik-server -> authentik"""
    return name.replace('_','-').split('-')[0]

def related(a, b):
    """True if a and b are likely in the same family."""
    ra, rb = root(a), root(b)
    if ra == rb and len(ra) >= 3: return True
    s, lg = (a,b) if len(a)<=len(b) else (b,a)
    return lg.startswith(s+'-') or lg.startswith(s+'_')

def load_all():
    containers = {}
    for fpath in sorted(glob.glob(f"{STACKS_DIR}/*.yml")):
        try:
            data = open(fpath).read()
            fname = fpath.split('/')[-1]
            for cname in re.findall(r'container_name:\s*(\S+)', data):
                cname = cname.strip().strip('"\'')
                if not cname: continue
                idx = data.find(f'container_name: {cname}')
                if idx < 0: continue
                block = data[idx:idx+3000]
                nxt = re.search(r'\n  [a-zA-Z][a-zA-Z0-9]', block[10:])
                if nxt: block = block[:nxt.start()+10]
                nets = set(re.findall(r'(\w+_net)\s*:', block)) - GLOBAL_NETS
                port_ips = re.findall(r'(192\.168\.1\.\d+):(\d+):\d+', block)
                ip = port_ips[0][0] if port_ips else None
                containers[cname] = {'file': fname, 'nets': nets, 'ip': ip}
        except: pass
    return containers

def build_families(containers):
    cnames = list(containers.keys())
    parent = {c: c for c in cnames}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        pa, pb = find(a), find(b)
        if pa == pb: return
        if len(pa) <= len(pb): parent[pb] = pa
        else: parent[pa] = pb

    # Method 1: Name root match (PRIMARY)
    # authentik-server + authentik-db + authentik-redis all share root 'authentik'
    for i, c1 in enumerate(cnames):
        for c2 in cnames[i+1:]:
            if related(c1, c2):
                union(c1, c2)

    # Method 2: Shared private network + name confirmation
    net_members = defaultdict(list)
    for cname, info in containers.items():
        for net in info['nets']:
            net_members[net].append(cname)
    for net, members in net_members.items():
        if len(members) < 2: continue
        for i, c1 in enumerate(members):
            for c2 in members[i+1:]:
                if related(c1, c2):
                    union(c1, c2)

    # Build groups
    groups = defaultdict(set)
    for c in cnames:
        groups[find(c)].add(c)

    # Filter and elect proper head
    result = {}
    for head, members in groups.items():
        if len(members) < 2: continue
        if any(s in head for s in SKIP_CONTAINERS): continue
        apps = [m for m in members if not is_support(m)]
        supports = [m for m in members if is_support(m)]
        if not apps or not supports: continue
        new_head = min(apps, key=lambda x: (len(x), x))
        result[new_head] = members
    return result

def get_families(stacks_dir=None):
    """Main callable. Returns {head: set(members)}"""
    global STACKS_DIR
    if stacks_dir: STACKS_DIR = stacks_dir
    return build_families(load_all())

def get_family_of(cname, stacks_dir=None):
    for head, members in get_families(stacks_dir).items():
        if cname in members: return head, members
    return None, None

def get_family_head(cname, stacks_dir=None):
    head, _ = get_family_of(cname, stacks_dir)
    return head

def main():
    containers = load_all()
    families = build_families(containers)
    all_in = set()
    for m in families.values(): all_in |= m
    sorted_fams = sorted(families.items(), key=lambda x: (-len(x[1]), x[0]))
    print()
    print("=" * 65)
    print("  CONTAINER FAMILY REPORT")
    print("=" * 65)
    print(f"  Total containers:        {len(containers)}")
    print(f"  Total families:          {len(sorted_fams)}")
    print(f"  Containers in families:  {len(all_in)}")
    print(f"  Standalone containers:   {len(containers) - len(all_in)}")
    print("=" * 65)
    for head, members in sorted_fams:
        other = sorted(m for m in members if m != head)
        print(f"\n  {head} ({len(members)} containers)")
        for m in other:
            print(f"    └─ {m}")

if __name__ == "__main__":
    main()
