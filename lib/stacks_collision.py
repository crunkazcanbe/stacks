#!/usr/bin/env python3
"""
stacks_collision.py — IP and port collision detection + assignment
═══════════════════════════════════════════════════════════════════
PUBLIC FUNCTIONS (importable):
  ✔ load_conf()                    — load stacks.conf settings
  ✔ scan_all_ips()                 — {ip: [(stack,container)]}
  ✔ scan_all_ports()               — {port: [(stack,container)]}
  ✔ get_collisions()               — (ip_collisions, port_collisions)
  ✔ get_next_available_ip()        — next free IP in range
  ✔ get_next_available_port(ip)    — next free port for a given IP
  ✔ get_image_default_port(image)  — inspect image for ExposedPorts
  ✔ is_locked_container(name)      — check if container is locked
  ✔ is_network_mode_host(fpath,svc)— check if service uses network_mode: host
  ✔ validate_ip(ip)                — check IP against range/blacklist/whitelist
  ✔ validate_port(port)            — check port against range/blacklist
  ✔ add_ip_blacklist(ip)           — add IP to blacklist in stacks.conf
  ✔ add_ip_whitelist(ip)           — add IP to whitelist in stacks.conf
  ✔ add_port_blacklist(port)       — add port to blacklist in stacks.conf
  ✔ add_locked_container(name)     — add container to locked list
"""
import os, re, glob, json, subprocess

STACKS_DIR = "/srv/stacks/Stacks"
CONF_FILE  = os.path.expanduser("~/.config/stacks/stacks.conf")

# ── Host port scanner ────────────────────────────────────────────────────────
def scan_host_ports():
    """
    Scan ports actually listening on the host via ss.
    Returns {ip: set(ports)} for all listening ports.
    ✔ Used to avoid assigning ports already bound on host
    """
    host_ports = {}
    try:
        r = subprocess.run(['ss', '-tlnp'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            # Format: LISTEN 0 N IP:PORT 0.0.0.0:*
            m = re.search(r'([\d.]+):(\d+)\s+0\.0\.0\.0:\*', line)
            if m:
                ip, port = m.group(1), m.group(2)
                if ip not in host_ports: host_ports[ip] = set()
                host_ports[ip].add(port)
    except: pass
    return host_ports

def is_port_free_on_host(ip, port):
    """Check if ip:port is actually free on the host."""
    host_ports = scan_host_ports()
    port = str(port)
    if ip in host_ports and port in host_ports[ip]:
        return False
    return True

# ── Related container grouping ────────────────────────────────────────────────
def get_related_containers(fpath):
    """
    Group related containers using multiple detection methods.
    PRIMARY (most reliable):
      1. Shared private network - coolify_net shared by coolify+coolify-db+coolify-redis
      2. Name prefix - coolify matches coolify-db, coolify-redis, coolify-realtime
    SECONDARY (supporting signals):
      3. depends_on declarations
      4. Env vars pointing to other containers (DATABASE_URL, REDIS_HOST etc)
      5. URL references in env (http://container-name:port)
    traefik_net excluded as it is present on every container.
    Returns list of sets of related container names.
    """
    groups = []
    try:
        data = open(fpath).read()
        cnames = [c.strip('"').strip("'").strip("'") for c in re.findall(r'container_name:\s*(\S+)', data)]
        if not cnames: return []

        # Build info blocks per container
        info = {}
        for cname in cnames:
            idx = data.find('container_name: ' + cname)
            if idx < 0: continue
            info[cname] = data[idx:idx+3000]

        def find_g(name):
            for g in groups:
                if name in g: return g
            return None

        def merge(a, b):
            if a == b or a not in cnames or b not in cnames: return
            ga, gb = find_g(a), find_g(b)
            if ga is None and gb is None: groups.append({a, b})
            elif ga is None: gb.add(a)
            elif gb is None: ga.add(b)
            elif ga is not gb: ga.update(gb); groups.remove(gb)

        GLOBAL_NETS = {
            'traefik_net','apartment_net','bridge','host',
            'none','ingress','docker_gwbridge'
        }

        # ── PRIMARY 1: Shared private network ────────────────────────────────
        net_members = {}
        for cname, block in info.items():
            for net in re.findall(r'(\w+_net)\s*:', block):
                if net in GLOBAL_NETS: continue
                net_members.setdefault(net, [])
                if cname not in net_members[net]:
                    net_members[net].append(cname)
        for net, members in net_members.items():
            if len(members) > 1:
                for m in members[1:]:
                    merge(members[0], m)

        # ── PRIMARY 2: Name prefix ────────────────────────────────────────────
        for i, c1 in enumerate(cnames):
            for c2 in cnames[i+1:]:
                short, long = (c1,c2) if len(c1)<=len(c2) else (c2,c1)
                if long.startswith(short+'-') or long.startswith(short+'_'):
                    merge(c1, c2)

        # ── SECONDARY 3: depends_on
        for cname, block in info.items():
            if "depends_on" in block:
                for d in re.findall(r"-\s+[A-Za-z][\w-]+", block):
                    d = d.strip().lstrip("- ").strip("\"'")
                    if d in cnames and d != cname: merge(cname, d)

        # ── SECONDARY 4: Env var references ──────────────────────────────────
        ENV_KEYS = (
            'DB_HOST','DATABASE_HOST','POSTGRES_HOST','MYSQL_HOST',
            'MONGO_HOST','REDIS_HOST','REDIS_URL','DATABASE_URL',
            'MONGO_URL','ELASTICSEARCH_HOST','OPENSEARCH_HOST',
            'CELERY_BROKER_URL','AMQP_URL','RABBITMQ_HOST',
        )
        for cname, block in info.items():
            for key in ENV_KEYS:
                for val in re.findall(
                    rf'{key}=(?:https?://|redis://|amqp://|postgresql://|mysql://)?'
                    r'(?:[^@\s]*@)?([a-zA-Z][a-zA-Z0-9_-]+)', block
                ):
                    val = val.strip('"').strip("'").strip("'")
                    if val in cnames and val != cname:
                        merge(cname, val)

        # ── SECONDARY 5: URL references ───────────────────────────────────────
        for cname, block in info.items():
            for ref in re.findall(
                r'(?:https?|redis|postgres|mysql|mongo)://[^@\s]*@?([a-zA-Z][a-zA-Z0-9_-]+):\d+',
                block
            ):
                ref = ref.strip('"').strip("'")
                if ref in cnames and ref != cname:
                    merge(cname, ref)

        return [g for g in groups if len(g) > 1]
    except Exception as e:
        return []


# ── Config ────────────────────────────────────────────────────────────────────
def load_conf():
    """Load all relevant settings from stacks.conf."""
    cfg = {
        "IP_RANGE_START":            "192.168.1.153",
        "IP_RANGE_END":              "192.168.1.253",
        "PORT_RANGE_START":          "8080",
        "PORT_RANGE_END":            "8999",
        "IP_BLACKLIST":              "192.168.1.1,192.168.1.114,192.168.1.151",
        "IP_WHITELIST":              "",
        "PORT_BLACKLIST":            "22,80,443,3306,5432,6379,27017,2375,2376",
        "LOCKED_IPS":                "",
        "IP_PORT_LOCKED_CONTAINERS": "cloudflared,cloudflared_tunnel_core,cloudflared-doh,traefik,sablier",
        "NETWORK_MODE_SKIP":         "1",
        "IP_COLLISION_AUTOFIX":      "0",
    }
    try:
        for line in open(CONF_FILE):
            l = line.strip()
            if "=" in l and not l.startswith("#"):
                k, v = l.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"\'')
    except: pass
    return cfg

def _update_conf(key, value):
    """Update or add a key in stacks.conf."""
    try:
        lines = open(CONF_FILE).readlines()
        found = False
        for i, l in enumerate(lines):
            if re.match(rf'^{key}\s*=', l.strip()):
                lines[i] = f'{key}="{value}"\n'
                found = True
                break
        if not found:
            lines.append(f'{key}="{value}"\n')
        open(CONF_FILE, 'w').writelines(lines)
        return True
    except: return False

# ── Blacklist/Whitelist management ────────────────────────────────────────────
def add_ip_blacklist(ip):
    """Add an IP to IP_BLACKLIST in stacks.conf."""
    cfg = load_conf()
    bl = [x.strip() for x in cfg["IP_BLACKLIST"].split(",") if x.strip()]
    if ip not in bl:
        bl.append(ip)
        _update_conf("IP_BLACKLIST", ",".join(bl))
    return bl

def add_ip_whitelist(ip):
    """Add an IP to IP_WHITELIST in stacks.conf."""
    cfg = load_conf()
    wl = [x.strip() for x in cfg["IP_WHITELIST"].split(",") if x.strip()]
    if ip not in wl:
        wl.append(ip)
        _update_conf("IP_WHITELIST", ",".join(wl))
    return wl

def add_port_blacklist(port):
    """Add a port to PORT_BLACKLIST in stacks.conf."""
    cfg = load_conf()
    bl = [x.strip() for x in cfg["PORT_BLACKLIST"].split(",") if x.strip()]
    port = str(port)
    if port not in bl:
        bl.append(port)
        _update_conf("PORT_BLACKLIST", ",".join(bl))
    return bl

def add_locked_container(name):
    """Add a container name to IP_PORT_LOCKED_CONTAINERS."""
    cfg = load_conf()
    locked = [x.strip() for x in cfg["IP_PORT_LOCKED_CONTAINERS"].split(",") if x.strip()]
    if name not in locked:
        locked.append(name)
        _update_conf("IP_PORT_LOCKED_CONTAINERS", ",".join(locked))
    return locked

# ── Validation ────────────────────────────────────────────────────────────────
def validate_ip(ip):
    """
    Check IP against range, blacklist, whitelist.
    Returns (valid:bool, reason:str)
    """
    cfg = load_conf()
    blacklist = set(x.strip() for x in cfg["IP_BLACKLIST"].split(",") if x.strip())
    locked    = set(x.strip() for x in cfg["LOCKED_IPS"].split(",") if x.strip())
    whitelist = [x.strip() for x in cfg["IP_WHITELIST"].split(",") if x.strip()]

    if ip in blacklist: return False, "blacklisted"
    if ip in locked:    return False, "locked"

    if whitelist:
        if ip in whitelist: return True, "whitelisted"
        return False, "not in whitelist"

    # Check range
    try:
        start = int(cfg["IP_RANGE_START"].split(".")[-1])
        end   = int(cfg["IP_RANGE_END"].split(".")[-1])
        prefix = ".".join(cfg["IP_RANGE_START"].split(".")[:3])
        last = int(ip.split(".")[-1])
        ip_prefix = ".".join(ip.split(".")[:3])
        if ip_prefix != prefix:
            return False, f"wrong subnet (expected {prefix}.x)"
        if start <= last <= end:
            return True, "in range"
        return False, f"out of range ({cfg['IP_RANGE_START']}-{cfg['IP_RANGE_END']})"
    except: return False, "invalid format"

def validate_port(port):
    """
    Check port against blacklist and range.
    Returns (valid:bool, reason:str)
    """
    cfg = load_conf()
    port = str(port)
    blacklist = set(x.strip() for x in cfg["PORT_BLACKLIST"].split(",") if x.strip())
    if port in blacklist: return False, "blacklisted"
    try:
        p = int(port)
        start = int(cfg["PORT_RANGE_START"])
        end   = int(cfg["PORT_RANGE_END"])
        if start <= p <= end: return True, "in range"
        # Allow outside range if not blacklisted
        return True, "outside range but not blacklisted"
    except: return False, "invalid format"

# ── Container checks ──────────────────────────────────────────────────────────
def is_locked_container(name):
    """Return True if container name is in locked list."""
    cfg = load_conf()
    locked = set(x.strip() for x in cfg["IP_PORT_LOCKED_CONTAINERS"].split(",") if x.strip())
    # Check exact match or partial (e.g. 'cloudflared' matches 'cloudflared-doh')
    if name in locked: return True
    for l in locked:
        if l in name or name in l: return True
    return False

def is_network_mode_host(fpath, svc_name):
    """Return True if service uses network_mode: host."""
    try:
        content = open(fpath).read()
        # Find service block
        m = re.search(rf'container_name:\s*{re.escape(svc_name)}(.*?)(?=\n  [a-zA-Z]|\Z)', content, re.DOTALL)
        if m and 'network_mode' in m.group(1) and 'host' in m.group(1):
            return True
    except: pass
    return False

# ── Image inspection ──────────────────────────────────────────────────────────
def get_image_default_port(image):
    """
    Inspect Docker image for ExposedPorts.
    Returns list of port numbers (strings) or empty list.
    """
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.ExposedPorts}}", image],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0: return []
        data = json.loads(r.stdout.strip())
        if not data: return []
        ports = []
        for key in data.keys():
            # key format: "8080/tcp" or "8080/udp"
            port = key.split("/")[0]
            if port.isdigit():
                ports.append(port)
        return sorted(ports, key=int)
    except: return []

# ── Scanning ──────────────────────────────────────────────────────────────────
def scan_all_ips():
    """Return {ip: [(stack, container)]} for all stacks."""
    ip_map = {}
    for fpath in sorted(glob.glob(f"{STACKS_DIR}/*.yml")):
        stack = os.path.basename(fpath).replace(".yml","")
        try:
            content = open(fpath).read()
            # Match ports like "192.168.1.x:port:port"
            for m in re.finditer(r'container_name:\s*(\S+)', content):
                cname = m.group(1)
                # Find IPs near this container
                block_start = m.start()
                # Get next 50 lines
                block = content[block_start:block_start+2000]
                for ip_m in re.finditer(r'(192\.168\.1\.\d+):\d+:\d+', block):
                    ip = ip_m.group(1)
                    if ip not in ip_map: ip_map[ip] = []
                    entry = (stack, cname)
                    if entry not in ip_map[ip]:
                        ip_map[ip].append(entry)
        except: pass
    return ip_map

def scan_all_ports():
    """Return {host_port: [(stack, container, ip)]} for all stacks."""
    port_map = {}
    for fpath in sorted(glob.glob(f"{STACKS_DIR}/*.yml")):
        stack = os.path.basename(fpath).replace(".yml","")
        try:
            content = open(fpath).read()
            for m in re.finditer(r'container_name:\s*(\S+)', content):
                cname = m.group(1)
                block_start = m.start()
                block = content[block_start:block_start+2000]
                for port_m in re.finditer(r'(192\.168\.1\.\d+):(\d+):\d+', block):
                    ip = port_m.group(1)
                    port = port_m.group(2)
                    key = f"{ip}:{port}"
                    if key not in port_map: port_map[key] = []
                    entry = (stack, cname)
                    if entry not in port_map[key]:
                        port_map[key].append(entry)
        except: pass
    return port_map

def get_collisions():
    """
    Scan all stacks for IP and port collisions.
    Returns (ip_collisions, port_collisions) lists of dicts.
    """
    cfg = load_conf()
    ip_map   = scan_all_ips()
    port_map = scan_all_ports()
    blacklist_ips   = set(x.strip() for x in cfg["IP_BLACKLIST"].split(",") if x.strip())
    blacklist_ports = set(x.strip() for x in cfg["PORT_BLACKLIST"].split(",") if x.strip())

    ip_collisions = []
    for ip, owners in ip_map.items():
        # IP sharing is OK - only flag blacklisted IPs
        if ip in blacklist_ips:
            ip_collisions.append({"ip": ip, "owners": owners, "type": "blacklisted"})

    port_collisions = []
    for key, owners in port_map.items():
        ip, port = key.split(":", 1)
        # Same IP:PORT used by multiple containers = real collision
        if len(owners) > 1:
            port_collisions.append({"ip": ip, "port": port, "owners": owners, "type": "duplicate"})
        elif port in blacklist_ports:
            port_collisions.append({"ip": ip, "port": port, "owners": owners, "type": "blacklisted"})

    return ip_collisions, port_collisions

# ── Assignment ────────────────────────────────────────────────────────────────
def get_next_available_ip():
    """
    Get next available IP respecting range, blacklist, whitelist, locked.
    Returns IP string or None.
    """
    cfg = load_conf()
    ip_map    = scan_all_ips()
    used      = set(ip_map.keys())
    blacklist = set(x.strip() for x in cfg["IP_BLACKLIST"].split(",") if x.strip())
    locked    = set(x.strip() for x in cfg["LOCKED_IPS"].split(",") if x.strip())
    whitelist = [x.strip() for x in cfg["IP_WHITELIST"].split(",") if x.strip()]
    blocked   = used | blacklist | locked

    if whitelist:
        for ip in whitelist:
            if ip not in blocked:
                return ip
        return None

    try:
        start  = int(cfg["IP_RANGE_START"].split(".")[-1])
        end    = int(cfg["IP_RANGE_END"].split(".")[-1])
        prefix = ".".join(cfg["IP_RANGE_START"].split(".")[:3])
        for i in range(start, end+1):
            ip = f"{prefix}.{i}"
            if ip not in blocked:
                return ip
    except: pass
    return None

def get_next_available_port(ip, preferred_port=None):
    """
    Get next available port for a given IP.
    Tries preferred_port first, then scans PORT_RANGE_START to PORT_RANGE_END.
    Returns port string or None.
    """
    cfg = load_conf()
    port_map  = scan_all_ports()
    blacklist = set(x.strip() for x in cfg["PORT_BLACKLIST"].split(",") if x.strip())

    # Build set of used ports for this IP
    used_ports = set()
    for key, owners in port_map.items():
        k_ip, k_port = key.split(":", 1)
        if k_ip == ip:
            used_ports.add(k_port)

    # Try preferred port first
    if preferred_port:
        p = str(preferred_port)
        if p not in used_ports and p not in blacklist:
            return p

    # Scan range
    try:
        start = int(cfg["PORT_RANGE_START"])
        end   = int(cfg["PORT_RANGE_END"])
        for port in range(start, end+1):
            p = str(port)
            if p not in used_ports and p not in blacklist:
                return p
    except: pass
    return None

def find_ip_with_free_port(port):
    """
    Given a desired port, find an IP where that port is free.
    This is the core logic for the fix script:
    - Try each IP in range
    - Return first IP where port is not already used
    Returns (ip, port) or (None, None)
    """
    cfg = load_conf()
    port_map  = scan_all_ports()
    blacklist_ips   = set(x.strip() for x in cfg["IP_BLACKLIST"].split(",") if x.strip())
    blacklist_ports = set(x.strip() for x in cfg["PORT_BLACKLIST"].split(",") if x.strip())
    locked    = set(x.strip() for x in cfg["LOCKED_IPS"].split(",") if x.strip())
    whitelist = [x.strip() for x in cfg["IP_WHITELIST"].split(",") if x.strip()]
    port = str(port)

    if port in blacklist_ports:
        return None, None

    # Build used ip:port set
    used = set(port_map.keys())

    ips_to_try = []
    if whitelist:
        ips_to_try = whitelist
    else:
        try:
            start  = int(cfg["IP_RANGE_START"].split(".")[-1])
            end    = int(cfg["IP_RANGE_END"].split(".")[-1])
            prefix = ".".join(cfg["IP_RANGE_START"].split(".")[:3])
            ips_to_try = [f"{prefix}.{i}" for i in range(start, end+1)]
        except: pass

    host_ports = scan_host_ports()

    for ip in ips_to_try:
        if ip in blacklist_ips or ip in locked: continue
        if f"{ip}:{port}" not in used:
            # Also check host is not already using this ip:port
            if ip in host_ports and port in host_ports[ip]:
                continue
            return ip, port

    return None, None


# ── Sticky assignment ledger (stops IP rotation across runs) ──────────────────
LEDGER_FILE = os.path.expanduser("~/.config/stacks/ip_assignments.conf")

def load_ledger():
    """container_name -> 'ip:port' remembered assignments."""
    led = {}
    try:
        for line in open(LEDGER_FILE):
            l = line.strip()
            if "=" in l and not l.startswith("#"):
                k, v = l.split("=", 1)
                led[k.strip()] = v.strip()
    except OSError:
        pass
    return led

def save_ledger(led):
    try:
        os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)
        with open(LEDGER_FILE, "w") as f:
            f.write("# container_name=ip:port — remembered IP assignments (stops rotation)\n")
            for k in sorted(led):
                f.write("%s=%s\n" % (k, led[k]))
    except OSError:
        pass

def stable_assign(cname, port):
    """Assign an IP to a container KEEPING its (default) port — never changes the
    port, only the IP. First-fit packing: reuse the container's remembered IP if
    still free, otherwise the lowest IP in range where this port is free. This
    packs many containers onto one IP (distinct ports) and only spills the next
    same-port container onto the next IP.

    The PORT blacklist is intentionally ignored here: we keep the port and move
    only the IP, so standard ports (6379/5432/3306/27017/...) are fine to keep.
    IP blacklist, locked IPs, whitelist and live host bindings are still honored.
    A persistent ledger records the choice so it never rotates across runs.
    Returns (ip, port) or (None, None)."""
    port = str(port)
    cfg = load_conf()
    led = load_ledger()
    port_map      = scan_all_ports()
    host_ports    = scan_host_ports()
    blacklist_ips = set(x.strip() for x in cfg["IP_BLACKLIST"].split(",") if x.strip())
    locked        = set(x.strip() for x in cfg["LOCKED_IPS"].split(",") if x.strip())
    whitelist     = [x.strip() for x in cfg["IP_WHITELIST"].split(",") if x.strip()]
    blacklist_ports = set(x.strip() for x in cfg["PORT_BLACKLIST"].split(",") if x.strip())

    def _free(ip, prt):
        if not ip or ip in blacklist_ips or ip in locked: return False
        # another container already holding ip:prt blocks it (our own slot is OK)
        if any(c != cname for (_s, c) in port_map.get(f"{ip}:{prt}", [])): return False
        if ip in host_ports and prt in host_ports[ip]: return False
        return True

    if whitelist:
        ips = whitelist
    else:
        try:
            start  = int(cfg["IP_RANGE_START"].split(".")[-1])
            end    = int(cfg["IP_RANGE_END"].split(".")[-1])
            prefix = ".".join(cfg["IP_RANGE_START"].split(".")[:3])
            ips = [f"{prefix}.{i}" for i in range(start, end + 1)]
        except Exception:
            ips = []

    # 1) reuse remembered assignment — stops rotation across runs
    rec = led.get(cname, "")
    if rec.endswith(":" + port) and _free(rec.split(":", 1)[0], port):
        return rec.split(":", 1)[0], port

    # 2) first-fit on the DEFAULT port: lowest IP where this port is free
    for ip in ips:
        if _free(ip, port):
            led[cname] = f"{ip}:{port}"
            save_ledger(led)
            return ip, port

    # 3) LAST RESORT — default port is taken on every IP. Only now change the port:
    #    lowest IP, lowest free non-blacklisted port in the configured range.
    try:
        p_start = int(cfg["PORT_RANGE_START"]); p_end = int(cfg["PORT_RANGE_END"])
    except Exception:
        p_start, p_end = 8080, 8999
    for ip in ips:
        if ip in blacklist_ips or ip in locked: continue
        for np in range(p_start, p_end + 1):
            nps = str(np)
            if nps in blacklist_ports: continue
            if _free(ip, nps):
                led[cname] = f"{ip}:{nps}"
                save_ledger(led)
                return ip, nps
    return None, None


if __name__ == "__main__":
    import sys as _sys
    # --find-port PORT SVC FILE mode for use by repair command
    if len(_sys.argv) >= 4 and _sys.argv[1] == "--find-port":
        _port, _svc, _file = _sys.argv[2], _sys.argv[3], _sys.argv[4]
        if is_locked_container(_svc):
            _sys.exit(1)
        _new_ip, _new_port = find_ip_with_free_port(_port)
        if not _new_ip:
            try:
                _data = open(_file).read()
                _m = re.search(r"container_name:\s*" + re.escape(_svc) + r".*?image:\s*(\S+)", _data, re.DOTALL)
                if _m:
                    for _p in get_image_default_port(_m.group(1).strip()):
                        _new_ip, _new_port = find_ip_with_free_port(_p)
                        if _new_ip: break
            except: pass
        if _new_ip:
            print(f"{_new_ip}:{_new_port}")
            _sys.exit(0)
        _sys.exit(1)
    # normal mode below
    import sys
    cfg = load_conf()
    print(f"IP Range:  {cfg['IP_RANGE_START']} → {cfg['IP_RANGE_END']}")
    print(f"Port Range: {cfg['PORT_RANGE_START']} → {cfg['PORT_RANGE_END']}")
    print(f"Blacklist IPs:  {cfg['IP_BLACKLIST']}")
    print(f"Blacklist Ports:{cfg['PORT_BLACKLIST']}")
    print(f"Locked containers: {cfg['IP_PORT_LOCKED_CONTAINERS']}")
    print()
    ip_col, port_col = get_collisions()
    print(f"IP collisions:   {len(ip_col)}")
    for c in ip_col[:5]:
        print(f"  {c['type']:12} {c['ip']:18} {c['owners']}")
    print(f"Port collisions: {len(port_col)}")
    for c in port_col[:5]:
        print(f"  {c['type']:12} {c['ip']}:{c['port']:8} {c['owners']}")
    print()
    next_ip = get_next_available_ip()
    print(f"Next available IP: {next_ip}")
    next_port = get_next_available_port(next_ip) if next_ip else None
    print(f"Next available port on {next_ip}: {next_port}")
    print()
    # Test image port detection
    if len(sys.argv) > 1:
        img = sys.argv[1]
        ports = get_image_default_port(img)
        print(f"Default ports for {img}: {ports}")
    # Test find_ip_with_free_port
    print(f"IP with free port 8080: {find_ip_with_free_port('8080')}")
    print(f"IP with free port 8443: {find_ip_with_free_port('8443')}")
