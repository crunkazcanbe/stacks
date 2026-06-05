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
# Infrastructure containers that should never be family heads
INFRA_SKIP = {'traefik','sablier','crowdsec-bouncer','error-pages'}
DB_WORDS = {'db','redis','cache','postgres','mysql','mongo','mariadb',
            'worker','celery','cron','realtime','beat','scheduler',
            'daemon','rabbitmq','memcached','valkey','indexer'}

def is_support(name):
    parts = name.replace('_','-').split('-')
    return parts[-1] in DB_WORDS or any(w in name for w in
           {'postgres','mysql','mongo','redis','rabbitmq','memcached'})

def root(name):
    """Get first meaningful segment: authentik-server -> authentik, wazuh.manager -> wazuh"""
    return name.replace('_','-').replace('.','-').split('-')[0]

# Generic prefixes that must NOT trigger family grouping. These are words
# shared by unrelated apps (open-webui vs open-notebook), or infra names.
NON_FAMILY_ROOTS = {'open', 'agent', 'cloudflared', 'minecraft', 'pritunl',
                    'tailscale', 'provisioner'}

def related(a, b):
    """True if a and b are likely in the same family."""
    ra, rb = root(a), root(b)
    # never group on a generic/non-family root
    if ra in NON_FAMILY_ROOTS or rb in NON_FAMILY_ROOTS:
        return False
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
        if not apps: continue
        if not supports:
            roots = set(root(m) for m in members)
            if len(roots) > 1: continue
        # Prefer names ending in common "main app" patterns
        def head_score(n):
            parts = n.replace(".","-").split("-")
            last = parts[-1]
            # Penalize support-like suffixes even if not in DB_WORDS
            penalty = {"indexer":3,"dashboard":2,"generator":4,
                       "certs":4,"cert":4,"worker":3,"web":1}.get(last,0)
            return (len(n) + penalty, n)
        new_head = min(apps, key=head_score)
        if new_head in INFRA_SKIP: continue
        result[new_head] = members
    return result

def _load_family_whitelist():
    """Read ~/.config/stacks/families.conf — lines of form  member=head
    Returns {member: head}. Used only to gap-fill when auto-detection
    leaves a container with no real (2+) family."""
    import os as _os
    wl = {}
    # prefer families.yaml (clean YAML master); fall back to families.conf
    try:
        import yaml as _y
        _yp = _os.path.expanduser("~/.config/stacks/families.yaml")
        if _os.path.exists(_yp):
            for m, h in (_y.safe_load(open(_yp)) or {}).items():
                h = str(h)
                if h.endswith('_net'): h = h[:-4]
                if m and h: wl[str(m)] = h
            if wl: return wl
    except Exception:
        pass
    cp = _os.path.expanduser("~/.config/stacks/families.conf")
    try:
        for line in open(cp):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            m, h = line.split('=', 1)
            m = m.strip(); h = h.strip()
            # tolerate "pangolin_net" or "pangolin" on the right side
            if h.endswith('_net'):
                h = h[:-4]
            if m and h:
                wl[m] = h
    except OSError:
        pass
    return wl


def get_families(stacks_dir=None):
    """Main callable. Returns {head: set(members)}"""
    global STACKS_DIR
    if stacks_dir: STACKS_DIR = stacks_dir
    fams = build_families(load_all())
    # Gap-fill from manual whitelist: only attach a member if it is NOT
    # already in a REAL family (2+ members). Lone/auto-missed = gap.
    wl = _load_family_whitelist()
    if wl:
        # which members are already in a real family?
        in_real = set()
        for _h, _mem in fams.items():
            if len(_mem) >= 2:
                in_real |= set(_mem)
        for member, head in wl.items():
            if member in in_real:
                continue  # auto already grouped it — don't override
            # remove member from any lone family it formed
            for _h in list(fams.keys()):
                if member in fams[_h] and len(fams[_h]) == 1:
                    del fams[_h]
            fams.setdefault(head, set())
            fams[head].add(head)
            fams[head].add(member)
    return fams

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
