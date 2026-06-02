#!/usr/bin/env python3
"""
stacks_fix.py — Automated compose file fixer for StacksServer

Fixes:
  1. Auto-defines missing networks/volumes into the smallest "creator" file
     (any compose file that defines named networks/volumes is a creator;
      not hard-coded to core_*). Names are taken VERBATIM from the service
      files — never spell-corrected. Networks get the full bridge template
      with a fresh, non-colliding 10.50.x subnet. Volumes get {external: true}.
      New defines are also synced into that file's provisioner_* service so
      they actually get created.
  2. Heals obvious typos in creator files (external: tfue -> true, etc.).
  3. Auto-injects smart healthchecks ONLY into services that have none.
     Existing healthchecks are NEVER touched. Deep-inspects the running
     container first, then falls back to the image-pattern table, then
     port-based, then a safe generic.

Usage:
  stacks_fix.py all
  stacks_fix.py <stackname>
  stacks_fix.py <stackname> <servicename>
  stacks_fix.py all --dry-run        # show what would change, write nothing

Config (optional): ~/.config/stacks/stacks.conf
  FIX_HEALTHCHECKS=1        # 0 disables all healthcheck injection
  FIX_DEFINE_NETVOL=1       # 0 disables network/volume auto-define
  FIX_HEAL_TYPOS=1          # 0 disables creator-file typo healing
  FIX_DEEP_INSPECT=1        # 0 skips docker inspect, uses pattern table only
  FIX_SUBNET_BASE=10.50     # the /16 prefix used for generated subnets
  FIX_HC_SKIP="svc1 svc2"   # space-separated service names to never add HC to
"""
import sys, os, re, subprocess, json, shutil, time

STACKS_DIR = "/srv/stacks/Stacks"
CONF_PATH  = "~/.config/stacks/stacks.conf"
BACKUP_DIR = "/srv/stacks/backups"

def _backup(p):
    try:
        import os, shutil, time
        cfg = load_conf()
        if not on(cfg.get("FIX_BACKUP", "1")):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        shutil.copy2(p, os.path.join(BACKUP_DIR, os.path.basename(p) + f".bak-{int(time.time())}"))
    except Exception:
        pass


G="\033[1;32m"; Y="\033[1;33m"; R="\033[1;31m"; C="\033[1;36m"; M="\033[1;35m"; X="\033[0m"

def pr(msg): print(msg, flush=True)

# ── Config loader ────────────────────────────────────────────────────────────
def load_conf():
    cfg = {
        "FIX_HEALTHCHECKS": "0",
        "FIX_DEFINE_NETVOL": "0",
        "FIX_HEAL_TYPOS": "0",
        "FIX_DEEP_INSPECT": "1",
        "FIX_SUBNET_BASE": "10.50",
        "FIX_BACKUP": "1",
        "FIX_VOLUME_BASE": "/srv/stacks/docker",  # base path for bind mounts
        "FIX_VOLUME_CONTAINER_PATH": "/config",          # default container-side path
        "FIX_AUTO_BIND_MOUNTS": "0",                     # auto-add bind mount if service has none
        "FIX_AUTO_NAMED_VOLUMES": "0",                   # auto-add named volume if service has none
        "FIX_CONVERT_NAMED_TO_BIND": "0",                # convert named volumes to bind mounts
        "FIX_CREATE_VOLUME_DIRS": "0",                   # auto-create host directories for bind mounts
        "FIX_AUTO_NETWORKS": "",                         # space-separated networks to add to every service
        "FIX_AUTO_LINK_NETWORKS": "0",                   # auto-gen stackname_net for stacks with 2+ services
        "FIX_REMOVE_GAPS": "0",  # set to 0 to disable blank line removal in service blocks
        "FIX_HC_IGNORE_STACKS": "",  # space-separated stack files to skip healthcheck changes
        "FIX_REPLACE_BROKEN_HC": "0",  # set to 1 to replace actively-failing healthchecks
        "FIX_FORCE_HC": "0",           # set to 1 to replace ALL healthchecks
        "FIX_FORCE_HC_CONTAINERS": "",  # comma-separated containers to always force-update HC
        "FIX_FORCE_NETWORKS": "0",     # 1 = re-inject networks even if already defined
        "FIX_FORCE_VOLUMES": "0",      # 1 = re-inject volumes even if already defined
        "FIX_EXTERNAL_NETWORKS": "1",  # 1 = generate external:true networks (default)
        "FIX_EXTERNAL_VOLUMES": "1",   # 1 = generate external:true volumes (default)
        "FIX_LOCAL_NETWORKS": "0",     # 1 = generate non-external local networks instead
        "FIX_LOCAL_VOLUMES": "0",      # 1 = generate non-external local volumes instead
        "FIX_INLINE_NETWORKS": "0",    # 1 = add networks directly in service file (not creator)
        "FIX_INLINE_VOLUMES": "0",     # 1 = add volumes directly in service file (not creator)
        "FIX_DEPENDS": "off",          # MASTER: on = inject depends_on (+includes for cross-stack); off = strip all depends_on
        "FIX_DEPENDS_INCLUDES": "1",   # when injecting, auto-add include: for cross-stack family members
        "FIX_AUTO_DEPENDS": "0",       # 1 = auto-inject depends_on for related containers (app->db->redis)
        "FIX_FORCE_DEPENDS": "0",      # 1 = re-inject depends_on even if already exists
        "FIX_STRIP_PROFILES": "0",  # set to 0 to disable auto-stripping of profiles: blocks
        "FIX_SKIP_FILES": "net_0-ext.yml",
        "FIX_HC_SKIP": "",
        "STACKS_DIR": STACKS_DIR,
    }
    if os.path.isfile(CONF_PATH):
        try:
            for line in open(CONF_PATH):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.split("#")[0].strip().strip('"').strip("'")
                cfg[k] = v  # accept all keys, not just pre-defined ones
        except Exception as e:
            pr(f"{Y}⚠ Could not fully read config ({e}); using defaults.{X}")
    # FIX_DEPENDS master switch maps to internal flags
    _fd = str(cfg.get("FIX_DEPENDS", "off")).strip().lower()
    if _fd in ("on", "1", "true", "yes"):
        cfg["FIX_AUTO_DEPENDS"] = "1"; cfg["FIX_REMOVE_DEPENDS"] = "0"
    elif _fd in ("off", "0", "false", "no"):
        cfg["FIX_AUTO_DEPENDS"] = "0"; cfg["FIX_REMOVE_DEPENDS"] = "1"
    return cfg

def on(v): return str(v).strip() not in ("0", "", "false", "False", "no")

# ── Healthcheck templates (image-name based) ───────────────────────────────────
HEALTHCHECKS = [
    (r'postgres|pgvecto|timescale',
     ['CMD-SHELL', 'pg_isready -U postgres || exit 1'],
     '10s','5s',10,'30s'),
    (r'mariadb|mysql',
     ['CMD-SHELL', 'healthcheck.sh --connect --innodb_initialized || exit 1'],
     '10s','5s',10,'30s'),
    (r'redis(?!.*insight)',
     ['CMD', 'redis-cli', 'ping'],
     '10s','3s',10,'10s'),
    (r'mongo(?!.*express|.*compass)',
     ['CMD', 'mongosh', '--quiet', '--eval', "db.adminCommand('ping').ok"],
     '10s','5s',10,'30s'),
    (r'elasticsearch|opensearch',
     ['CMD-SHELL', 'curl -sf http://localhost:9200/_cluster/health || exit 1'],
     '30s','10s',5,'60s'),
    (r'qdrant',
     ['CMD-SHELL', 'curl -sf http://localhost:6333/healthz || exit 1'],
     '10s','5s',10,'30s'),
    (r'neo4j',
     ['CMD-SHELL', 'curl -sf http://localhost:7474 || exit 1'],
     '15s','5s',10,'60s'),
    (r'influxdb',
     ['CMD-SHELL', 'curl -sf http://localhost:8086/health || exit 1'],
     '10s','5s',10,'30s'),
    (r'couchdb',
     ['CMD-SHELL', 'curl -sf http://localhost:5984/_up || exit 1'],
     '10s','5s',10,'30s'),
    (r'rabbitmq',
     ['CMD', 'rabbitmq-diagnostics', 'ping'],
     '15s','5s',10,'30s'),
    (r'minio',
     ['CMD-SHELL', 'curl -sf http://localhost:9000/minio/health/live || exit 1'],
     '10s','5s',10,'30s'),
    (r'surrealdb|surreal',
     ['CMD-SHELL', 'curl -sf http://localhost:8000/health || exit 1'],
     '10s','5s',10,'30s'),
    (r'traefik',
     ['CMD', 'traefik', 'healthcheck'],
     '10s','5s',5,'10s'),
    (r'nginx-proxy-manager|jc21/nginx',
     ['CMD-SHELL', 'curl -sf http://localhost:81/api || exit 1'],
     '15s','5s',10,'30s'),
    (r'nginx(?!.*proxy.*manager)|openresty',
     ['CMD-SHELL', 'curl -sf http://localhost/ || exit 1'],
     '10s','5s',5,'10s'),
    (r'caddy',
     ['CMD-SHELL', 'caddy validate --config /etc/caddy/Caddyfile || exit 1'],
     '10s','5s',5,'10s'),
    (r'authelia',
     ['CMD-SHELL', 'wget -qO- http://localhost:9091/api/health || exit 1'],
     '10s','5s',10,'30s'),
    (r'goauthentik.*server|authentik.*server',
     ['CMD-SHELL', 'ak healthcheck || exit 1'],
     '10s','5s',10,'60s'),
    (r'vaultwarden|bitwarden',
     ['CMD-SHELL', 'curl -sf http://localhost:80/alive || exit 1'],
     '10s','5s',10,'30s'),
    (r'crowdsec(?!.*bouncer)',
     ['CMD-SHELL', 'cscli version || exit 1'],
     '15s','5s',5,'30s'),
    (r'grafana',
     ['CMD-SHELL', 'curl -sf http://localhost:3000/api/health || exit 1'],
     '10s','5s',10,'30s'),
    (r'prometheus',
     ['CMD-SHELL', 'wget -qO- http://localhost:9090/-/healthy || exit 1'],
     '10s','5s',5,'30s'),
    (r'netdata',
     ['CMD-SHELL', 'curl -sf http://localhost:19999/api/v1/info || exit 1'],
     '15s','5s',5,'30s'),
    (r'uptime.kuma',
     ['CMD-SHELL', 'curl -sf http://localhost:3001 || exit 1'],
     '10s','5s',10,'30s'),
    (r'wazuh.*dashboard',
     ['CMD-SHELL', 'curl -skf https://localhost:5601/api/status || exit 1'],
     '30s','10s',10,'120s'),
    (r'wazuh.*manager',
     ['CMD-SHELL', '/var/ossec/bin/wazuh-control status | grep -q running || exit 1'],
     '15s','5s',10,'60s'),
    (r'jellyfin',
     ['CMD-SHELL', 'curl -sf http://localhost:8096/health || exit 1'],
     '15s','5s',10,'60s'),
    (r'immich.*server|immich.*microservices',
     ['CMD-SHELL', 'curl -sf http://localhost:3001/api/server-info/ping || exit 1'],
     '10s','5s',10,'60s'),
    (r'nextcloud',
     ['CMD-SHELL', 'curl -sf http://localhost/status.php | grep -q ok || exit 1'],
     '30s','10s',10,'120s'),
    (r'gitea',
     ['CMD-SHELL', 'curl -sf http://localhost:3000/api/v1/version || exit 1'],
     '10s','5s',10,'30s'),
    (r'portainer',
     ['CMD-SHELL', 'curl -sf https://localhost:9443/api/system/status || curl -sf http://localhost:9000/api/system/status || exit 1'],
     '10s','5s',10,'30s'),
    (r'ollama',
     ['CMD-SHELL', 'curl -sf http://localhost:11434/api/version || exit 1'],
     '10s','5s',10,'30s'),
    (r'open.webui|openwebui',
     ['CMD-SHELL', 'curl -sf http://localhost:8080/health || exit 1'],
     '10s','5s',10,'60s'),
    (r'searxng',
     ['CMD-SHELL', 'curl -sf http://localhost:8080/ || exit 1'],
     '10s','5s',10,'30s'),
    (r'litellm',
     ['CMD-SHELL', 'curl -sf http://localhost:4000/health || exit 1'],
     '10s','5s',10,'30s'),
    (r'n8n',
     ['CMD-SHELL', 'curl -sf http://localhost:5678/healthz || exit 1'],
     '10s','5s',10,'60s'),
    (r'netbird.*server',
     ['CMD-SHELL', 'curl -sf http://localhost:80/api/v1/setup-keys || exit 1'],
     '15s','5s',10,'30s'),
    (r'adguard',
     ['CMD-SHELL', 'curl -sf http://localhost:3000 || exit 1'],
     '10s','5s',10,'30s'),
    (r'pihole',
     ['CMD-SHELL', 'curl -sf http://localhost/admin/api.php || exit 1'],
     '10s','5s',10,'30s'),
    (r'technitium',
     ['CMD-SHELL', 'curl -sf http://localhost:5380 || exit 1'],
     '10s','5s',10,'30s'),
    (r'letta',
     ['CMD-SHELL', 'wget -qO- http://localhost:8283/v1/health || exit 1'],
     '10s','5s',10,'60s'),
    (r'speaches',
     ['CMD-SHELL', 'wget -qO- http://localhost:8000/health || exit 1'],
     '10s','5s',10,'30s'),
    (r'whisper|faster.whisper',
     ['CMD-SHELL', 'wget -qO- http://localhost:8000/health || exit 1'],
     '10s','5s',10,'30s'),
    (r'playwright',
     ['CMD-SHELL', 'wget -qO- http://localhost:3000 || exit 1'],
     '10s','5s',10,'30s'),
]

PORT_MAP = {
    '80':'http://localhost:80/',     '81':'http://localhost:81/',
    '3000':'http://localhost:3000/', '3001':'http://localhost:3001/',
    '4000':'http://localhost:4000/', '5000':'http://localhost:5000/',
    '7860':'http://localhost:7860/', '8000':'http://localhost:8000/',
    '8080':'http://localhost:8080/', '8096':'http://localhost:8096/',
    '9000':'http://localhost:9000/', '9090':'http://localhost:9090/',
}

def hc_from_pattern(image, ports):
    img = image.lower().split(':')[0]
    for pattern, cmd, interval, timeout, retries, start in HEALTHCHECKS:
        if re.search(pattern, img, re.I):
            return cmd, interval, timeout, retries, start, f"pattern:{pattern}"
    for p in ports:
        m = re.search(r':(\d+):\d+', p) or re.search(r'^(\d+):\d+', p)
        if m and m.group(1) in PORT_MAP:
            url = PORT_MAP[m.group(1)]
            return (['CMD-SHELL', f'wget -qO- {url} || exit 1'],
                    '30s','10s',5,'60s', f"port:{m.group(1)}")
    return (['CMD-SHELL', 'wget -qO- http://localhost:8080/ || exit 1'],
            '30s','10s',5,'60s', "generic")


# Creator file generator - to be injected into stacks_fix.py

CREATOR_TEMPLATE = '''name: {name}

services:
  {provisioner_name}:
    image: alpine:latest
    container_name: {provisioner_name}
    command: tail -f /dev/null
    restart: "no"
    cpuset: "4-5"
    cpu_shares: 256
    networks:
      - "traefik_net"

networks:
  traefik_net:
    external: true

volumes:
'''

def generate_creator_file(path, name, networks, volumes, subnet_base="10.50", used_subnets=None):
    """
    Generate a complete creator compose file with provisioner container.
    networks: list of network names
    volumes: list of volume names
    """
    if used_subnets is None:
        used_subnets = set()

    provisioner_name = f"provisioner_{name.replace('-','_')}"
    
    # Generate network definitions
    top_nets = []
    service_nets = []
    net_cmds = []
    
    for i, net in enumerate(networks):
        # Find next available subnet octet
        octet = 1
        while octet in used_subnets or octet > 254:
            octet += 1
        used_subnets.add(octet)
        
        base = net.replace('_net','')
        subnet = f"{subnet_base}.{octet}.0/24"
        gateway = f"{subnet_base}.{octet}.1"
        
        top_nets.append(
            f"  {net}: {{name: {net}, driver: bridge, attachable: true, "
            f"external: false, internal: false, enable_ipv6: false, "
            f'labels: ["com.stacks.network={base}", '
            f'"com.stacks.env=production"], '
            f"ipam: {{driver: default, config: [{{subnet: {subnet}, "
            f"gateway: {gateway}}}]}}}}"
        )
        service_nets.append(f"      {net}:")
        net_cmds.append(f"      docker network create {net} 2>/dev/null || true")
    
    # Generate volume definitions
    top_vols = []
    service_vols = []
    vol_cmds = []
    
    for vol in volumes:
        top_vols.append(f"  {vol}: {{name: {vol}, external: true}}")
        service_vols.append(f"      - /tmp:/tmp")  # dummy mount for provisioner
        vol_cmds.append(f"      docker volume create {vol} 2>/dev/null || true")
    
    if not top_nets:
        top_nets = ["  {}  # no networks yet"]
    if not top_vols:
        top_vols = ["  {}  # no volumes yet"]
    if not service_nets:
        service_nets = ["      traefik_net:"]
    if not service_vols:
        service_vols = ["      - /tmp:/tmp"]

    content = CREATOR_TEMPLATE.format(
        name=name,
        provisioner_name=provisioner_name,
        network_cmds='\n      '.join(net_cmds) if net_cmds else 'echo no networks',
        volume_cmds='\n      '.join(vol_cmds) if vol_cmds else 'echo no volumes',
        service_networks='\n'.join(service_nets),
        service_volumes='\n'.join(service_vols),
        top_networks='\n'.join(top_nets),
        top_volumes='\n'.join(top_vols),
    )
    
    with open(path, 'w') as f:
        f.write(content)
    return path


def find_or_create_creator(stacks_dir, cfg):
    """
    Find existing creator file or create a new one based on config.
    Returns path to creator file to use.
    """
    import os, glob
    
    # Respect explicit target from build wizard
    _target = cfg.get("FIX_CREATOR_TARGET")
    if _target:
        _tp = os.path.join(stacks_dir, _target + ".yml")
        if os.path.exists(_tp):
            return _tp
    
    # First try to find existing creator with provisioner
    best = None
    best_size = float('inf')
    for f in sorted(glob.glob(f"{stacks_dir}/*.yml")):
        try:
            content = open(f).read()
            import re
            if re.search(r'container_name:\s*provisioner', content):
                sz = os.path.getsize(f)
                # Check if under max limits
                net_count = len(re.findall(r'^\s{2}\w+_net:', content, re.MULTILINE))
                vol_count = len(re.findall(r'^\s{2}\w+:\s*\{.*external', content, re.MULTILINE))
                max_nets = int(cfg.get('FIX_CREATOR_MAX_NETWORKS', '20'))
                max_vols = int(cfg.get('FIX_CREATOR_MAX_VOLUMES', '20'))
                if net_count < max_nets and vol_count < max_vols:
                    if sz < best_size:
                        best_size = sz
                        best = f
        except: pass
    
    if best:
        return best
    
    # No suitable creator found - create one if config allows
    if cfg.get('FIX_AUTO_CREATE_CREATOR', '0') != '1':
        return None
    
    base_name = cfg.get('FIX_CREATOR_NAME', 'core')
    # Find next available number
    n = 0
    while os.path.exists(os.path.join(stacks_dir, f"{base_name}_{n}.yml")):
        n += 1
    
    new_path = os.path.join(stacks_dir, f"{base_name}_{n}.yml")
    generate_creator_file(new_path, f"{base_name}_{n}", [], [])
    return new_path



# ── Image healthcheck knowledge base ────────────────────────────────────────
IMAGE_HC_DB = {
    "cloudflare/cloudflared":               (["CMD", "cloudflared", "version"], "30s", "5s", 3, "10s"),
    "thespad/traefik-crowdsec-bouncer":      (["CMD-SHELL", "wget -qO- http://localhost:8080/api/v1/forwardAuth || exit 1"], "15s", "5s", 5, "30s"),
    "crowdsecurity/cs-traefik-bouncer":      (["CMD-SHELL", "wget -qO- http://localhost:8080/api/v1/forwardAuth || exit 1"], "15s", "5s", 5, "30s"),
    "crowdsecurity/crowdsec":               (["CMD-SHELL", "cscli version || exit 1"], "30s", "5s", 3, "30s"),
    "tailscale/tailscale":                  (["CMD-SHELL", "tailscale status || exit 1"], "30s", "5s", 3, "30s"),
    "pihole/pihole":                        (["CMD-SHELL", "dig +short +norecurse +retry=0 @127.0.0.1 pi.hole || exit 1"], "30s", "10s", 3, "30s"),
    "adguard/adguardhome":                  (["CMD-SHELL", "wget -qO- http://localhost:3000 || exit 1"], "30s", "5s", 3, "20s"),
    "nginxproxy/nginx-proxy-manager":       (["CMD-SHELL", "wget -qO- http://localhost:81/api || exit 1"], "30s", "5s", 3, "30s"),
    "jc21/nginx-proxy-manager":             (["CMD-SHELL", "wget -qO- http://localhost:81/api || exit 1"], "30s", "5s", 3, "30s"),
    "technitium/dns-server":                (["CMD-SHELL", "curl -sf http://localhost:5380/ || exit 1"], "30s", "5s", 3, "30s"),
    "fosrl/pangolin":                       (["CMD-SHELL", "wget -qO- http://localhost:3001/ || exit 1"], "30s", "5s", 3, "30s"),
    "fosrl/gerbil":                         (["CMD-SHELL", "wget -qO- http://localhost:3003/ || exit 1"], "30s", "5s", 3, "30s"),
    "netbirdio/management":                 (["CMD-SHELL", "wget -qO- http://localhost:80/ || exit 1"], "30s", "5s", 3, "30s"),
    "netbirdio/dashboard":                  (["CMD-SHELL", "wget -qO- http://localhost:80/ || exit 1"], "30s", "5s", 3, "20s"),
    "authelia/authelia":                    (["CMD-SHELL", "wget -qO- http://localhost:9091/api/health || exit 1"], "30s", "5s", 3, "30s"),
    "acouvreur/sablier":                    (["CMD-SHELL", "wget -qO- http://localhost:10000/health || exit 1"], "15s", "5s", 3, "10s"),
    "wazuh/wazuh-indexer":                  (["CMD-SHELL", "curl -sf http://localhost:9200/_cluster/health || exit 1"], "30s", "10s", 5, "60s"),
    "wazuh/wazuh-manager":                  (["CMD-SHELL", "/var/ossec/bin/wazuh-control status || exit 1"], "30s", "10s", 5, "60s"),
    "wazuh/wazuh-dashboard":                (["CMD-SHELL", "curl -sf http://localhost:5601/api/status || exit 1"], "30s", "10s", 5, "60s"),
    "portainer/portainer":                  (["CMD-SHELL", "wget -qO- http://localhost:9000/api/system/status || exit 1"], "30s", "5s", 3, "20s"),
    "traefik":                              (["CMD-SHELL", "traefik healthcheck || exit 1"], "10s", "5s", 3, "10s"),
    "dperson/openvpn-client":               (["CMD-SHELL", "ip addr show tun0 || exit 1"], "30s", "5s", 3, "30s"),
    "qmcgaw/gluetun":                       (["CMD-SHELL", "wget -qO- http://localhost:8000/v1/vpn/status || exit 1"], "30s", "5s", 3, "30s"),
    "dperson/torproxy":                     (["CMD-SHELL", "nc -z localhost 8118 || exit 1"], "30s", "5s", 3, "30s"),
    "ghcr.io/goauthentik/server":           (["CMD-SHELL", "ak healthcheck || exit 1"], "30s", "5s", 5, "30s"),
    "v2fly/v2fly-core":                     (["CMD-SHELL", "v2ray version || exit 1"], "30s", "5s", 3, "10s"),
    "nginx":                                (["CMD-SHELL", "nginx -t || exit 1"], "30s", "5s", 3, "10s"),
    "caddy":                                (["CMD-SHELL", "caddy validate --config /etc/caddy/Caddyfile || exit 1"], "30s", "5s", 3, "10s"),
    "headscale/headscale":                  (["CMD-SHELL", "wget -qO- http://localhost:8080/health || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/speedtest-tracker": (["CMD-SHELL", "curl -sf http://localhost:80/ || exit 1"], "60s", "10s", 3, "60s"),
    "jellyfin/jellyfin":                    (["CMD-SHELL", "curl -sf http://localhost:8096/health || exit 1"], "30s", "10s", 3, "60s"),
    "jlesage/jdownloader-2":                (["CMD-SHELL", "curl -sf http://localhost:5800/ || exit 1"], "30s", "5s", 3, "60s"),
    "juanfont/headscale":                   (["CMD-SHELL", "wget -qO- http://localhost:8080/health || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/jellyfin":         (["CMD-SHELL", "curl -sf http://localhost:8096/health || exit 1"], "30s", "10s", 3, "60s"),
    "lscr.io/linuxserver/bazarr":           (["CMD-SHELL", "curl -sf http://localhost:6767/api/ || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/readarr":          (["CMD-SHELL", "curl -sf http://localhost:8787/api/v1/system/status || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/lidarr":           (["CMD-SHELL", "curl -sf http://localhost:8686/api/v1/system/status || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/radarr":           (["CMD-SHELL", "curl -sf http://localhost:7878/api/v3/system/status || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/sonarr":           (["CMD-SHELL", "curl -sf http://localhost:8989/api/v3/system/status || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/prowlarr":         (["CMD-SHELL", "curl -sf http://localhost:9696/api/v1/system/status || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/jackett":          (["CMD-SHELL", "curl -sf http://localhost:9117/UI/Login || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/qbittorrent":      (["CMD-SHELL", "curl -sf http://localhost:8080/ || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/sabnzbd":          (["CMD-SHELL", "curl -sf http://localhost:8080/api?mode=version || exit 1"], "30s", "5s", 3, "30s"),
    "lscr.io/linuxserver/jdownloader-2":    (["CMD-SHELL", "curl -sf http://localhost:5800/ || exit 1"], "30s", "5s", 3, "60s"),
    "cauliflower/speedtest-tracker":        (["CMD-SHELL", "curl -sf http://localhost:80/ || exit 1"], "60s", "10s", 3, "60s"),
    "alexjustesen/speedtest-tracker":       (["CMD-SHELL", "curl -sf http://localhost:80/ || exit 1"], "60s", "10s", 3, "60s"),
    "henrywhitaker3/speedtest-tracker":     (["CMD-SHELL", "curl -sf http://localhost:80/ || exit 1"], "60s", "10s", 3, "60s"),
    "adguard/adguardhome":                  (["CMD-SHELL", "wget -qO- http://localhost:3000 || exit 1"], "30s", "5s", 3, "20s"),
    "containrrr/watchtower":                (["CMD-SHELL", "wget -qO- http://localhost:8080/ || exit 1"], "30s", "5s", 3, "30s"),
    "amir20/dozzle":                        (["CMD-SHELL", "wget -qO- http://localhost:8080/healthcheck || exit 1"], "30s", "5s", 3, "20s"),
}

def probe_container(name):
    """Exec into running container to find available tools and ports."""
    tools = {}
    for shell in ["/bin/sh", "/bin/bash", "/busybox/sh", "/usr/bin/sh"]:
        try:
            r = subprocess.run(["docker", "exec", name, shell, "-c", "echo ok"],
                               capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                tools["shell"] = shell
                break
        except:
            pass
    if "shell" in tools:
        sh = tools["shell"]
        for tool in ["curl", "wget", "nc", "netcat", "ping", "ss", "netstat"]:
            try:
                r = subprocess.run(["docker", "exec", name, sh, "-c", f"which {tool} 2>/dev/null"],
                                   capture_output=True, text=True, timeout=2)
                if r.returncode == 0 and r.stdout.strip():
                    tools[tool] = r.stdout.strip()
            except:
                pass
    ports = []
    if "shell" in tools:
        sh = tools["shell"]
        for cmd in ["ss -tlnp 2>/dev/null", "netstat -tlnp 2>/dev/null"]:
            try:
                r = subprocess.run(["docker", "exec", name, sh, "-c", cmd],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    for line in r.stdout.splitlines():
                        m = re.search(r":(\d+)\s", line)
                        if m:
                            p = int(m.group(1))
                            if 0 < p < 65536 and p not in ports:
                                ports.append(p)
                    if ports:
                        break
            except:
                pass
    main_bin = None
    try:
        r = subprocess.run(["docker", "inspect", name, "--format", "{{index .Config.Cmd 0}}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            main_bin = r.stdout.strip()
    except:
        pass
    return tools, sorted(ports), main_bin

def hc_from_inspect(container, image=''):
    """
    Enhanced healthcheck detection:
    1. Image knowledge base (exact/partial match)
    2. Real container exec probing (tools + ports)
    3. Returns None if nothing found
    """
    # 1. Image knowledge base — strip tag before matching
    img_lower = (image or "").lower().split(':')[0]
    # Also try with registry prefix stripped
    img_base = img_lower.split('/')[-1] if '/' in img_lower else img_lower
    for pattern, hc_tuple in IMAGE_HC_DB.items():
        pat_lower = pattern.lower()
        if pat_lower in img_lower or pat_lower == img_base:
            cmd, interval, timeout, retries, start = hc_tuple
            return (cmd, interval, timeout, retries, start, f"db:{pattern}")

    # 2. Check container is running
    try:
        r = subprocess.run(["docker", "inspect", container, "--format", "{{.State.Status}}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0 or r.stdout.strip() != "running":
            return None
    except:
        return None

    # 3. Real exec probing
    tools, ports, main_bin = probe_container(container)

    # Distroless — no shell available
    if "shell" not in tools:
        if main_bin:
            return (["CMD", main_bin, "--version"], "30s", "5s", 3, "10s", "probe:distroless")
        return None

    # Has shell — build from available tools and ports
    web_ports = [p for p in ports if p in [80, 81, 443, 3000, 3001, 8000, 8080, 8443, 9000, 9090, 9091]]
    if web_ports:
        port = web_ports[0]
        if "curl" in tools:
            return (["CMD-SHELL", f"curl -sf http://localhost:{port}/ || exit 1"],
                    "30s", "10s", 3, "30s", f"probe:curl-{port}")
        if "wget" in tools:
            return (["CMD-SHELL", f"wget -qO- http://localhost:{port}/ || exit 1"],
                    "30s", "10s", 3, "30s", f"probe:wget-{port}")
    if ports and "nc" in tools:
        port = ports[0]
        return (["CMD-SHELL", f"nc -z localhost {port} || exit 1"],
                "30s", "5s", 3, "20s", f"probe:nc-{port}")
    if "curl" in tools:
        return (["CMD-SHELL", "curl -sf http://localhost/ || exit 1"],
                "30s", "5s", 3, "30s", "probe:curl-generic")
    if "wget" in tools:
        return (["CMD-SHELL", "wget -qO- http://localhost/ || exit 1"],
                "30s", "5s", 3, "30s", "probe:wget-generic")
    return None

def _ns_to_s(ns):
    """Docker inspect returns durations in nanoseconds; convert to '12s'."""
    try:
        if not ns:
            return None
        secs = int(ns) // 1_000_000_000
        if secs <= 0:
            return None
        return f"{secs}s"
    except Exception:
        return None

def choose_healthcheck(svc, deep_inspect):
    """Deep-inspect running container first, then pattern/port/generic."""
    if deep_inspect and svc['name']:
        res = hc_from_inspect(svc['name'], svc.get('image', ''))
        if res:
            return res
    return hc_from_pattern(svc['image'], svc['ports'])

def format_healthcheck(cmd, interval, timeout, retries, start):
    lines = ['    healthcheck:']
    lines.append('      test:')
    for item in cmd:
        lines.append(f'        - "{item}"')
    lines.append(f'      interval: {interval}')
    lines.append(f'      timeout: {timeout}')
    lines.append(f'      retries: {retries}')
    lines.append(f'      start_period: {start}')
    return '\n'.join(lines) + '\n'

# ── Robust service parser ──────────────────────────────────────────────────────
def parse_services_with_positions(path):
    """
    Bulletproof healthcheck detection so we NEVER double-inject into a service
    that already has one (the bug that wiped Wazuh/dokploy/Coolify checks).
    Detects healthcheck in block form, flow form {...}, disabled form, and
    commented form within the service's own indentation block.
    """
    lines = open(path).readlines()
    services = []
    in_services = False
    current = None

    for i, line in enumerate(lines):
        stripped = line.rstrip()

        if re.match(r'^services:\s*$', stripped):
            in_services = True
            continue

        if in_services and re.match(r'^[a-zA-Z]', stripped) and not stripped.startswith(' '):
            if current:
                current['block_end'] = i - 1
                services.append(current)
                current = None
            in_services = False
            continue

        if not in_services:
            continue

        m = re.match(r'^  ([a-zA-Z0-9][a-zA-Z0-9_.\-]*):\s*$', stripped)
        # Skip YAML anchor keys that appear at service indent but aren't services
        _anchor_keys = {'cap_add','sysctls','tmpfs','security_opt','dns',
                        'volumes','networks','ports','environment','labels',
                        'devices','ulimits','logging','deploy','secrets',
                        'configs','build','command','entrypoint','depends_on',
                        'healthcheck','restart','image','container_name'}
        if m and not m.group(1).startswith('x-') and m.group(1) not in _anchor_keys:
            if current:
                current['block_end'] = i - 1
                services.append(current)
            current = {
                'name': m.group(1),
                'image': '',
                'ports': [],
                'has_healthcheck': False,
                'block_start': i,
                'block_end': len(lines) - 1,
            }
            continue

        if current:
            im = re.match(r'^\s+image:\s+(.+)', stripped)
            if im:
                current['image'] = im.group(1).strip()
            pm = re.match(r'^\s+-\s+"?(\S+:\d+:\d+)', stripped)
            if pm:
                current['ports'].append(pm.group(1))
            # Healthcheck detection — any of these counts as "already present":
            #   healthcheck:            (block)
            #   healthcheck: {...}      (flow / inline)
            #   healthcheck: disable    (rare)
            #   # healthcheck: ...      (commented — leave the service alone)
            #   <<: *something-with-healthcheck  (anchor merge; treat as present)
            low = stripped.strip()
            if re.match(r'^#?\s*healthcheck\s*:', low):
                current['has_healthcheck'] = True
            if 'healthcheck' in low and re.search(r'\*[\w\-]*health', low):
                current['has_healthcheck'] = True

    if current:
        services.append(current)

    return services, lines


def replace_hc_in_service(lines, svc, hc_tuple):
    """Replace an existing healthcheck block in a service with a new one."""
    hc_cmd, interval, timeout_s, retries, start, source = hc_tuple
    new_hc_text = format_healthcheck(hc_cmd, interval, timeout_s, retries, start)
    result = list(lines)
    # Find the healthcheck block inside this service
    in_service = False
    hc_start = None
    hc_end = None
    for i, line in enumerate(lines):
        if i == svc['block_start']:
            in_service = True
        if in_service and i > svc['block_start']:
            stripped = line.strip()
            if re.match(r'^[a-zA-Z0-9]', line) and not line.startswith(' '):
                break
            if re.match(r'^  [a-zA-Z0-9]', line) and not line.startswith('    '):
                if hc_start is not None:
                    hc_end = i
                    break
            if stripped.startswith('healthcheck:'):
                hc_start = i
            elif hc_start is not None and stripped and not stripped.startswith('healthcheck'):
                indent = len(line) - len(line.lstrip())
                if indent <= 4:
                    hc_end = i
                    break
    if hc_start is None:
        return lines, False
    if hc_end is None:
        hc_end = svc['block_end'] + 1
    # Replace the block
    result[hc_start:hc_end] = (new_hc_text + '\n').splitlines(keepends=True)
    return result, True

def inject_hc_into_service(lines, svc, deep_inspect):
    """Insert a healthcheck. Caller guarantees svc has none. Returns (lines, note)."""
    hc_cmd, interval, timeout, retries, start, source = choose_healthcheck(svc, deep_inspect)
    hc_text = format_healthcheck(hc_cmd, interval, timeout, retries, start)

    insert_after = None
    for i in range(svc['block_start'], svc['block_end'] + 1):
        l = lines[i].rstrip()
        if re.match(r'^\s+(blkio_config|ulimits|deploy|storage_opt|logging):', l):
            insert_after = i
            break
    if insert_after is None:
        # No anchor — insert right AFTER the image: line so it lands inside
        # the service block, never past the end of the file.
        for i in range(svc['block_start'], svc['block_end'] + 1):
            if re.match(r'^\s+image:', lines[i]):
                insert_after = i + 1
                break
    if insert_after is None:
        # Last resort: right after the service's own header line
        insert_after = svc['block_start'] + 1

    new_lines = lines[:insert_after] + [hc_text] + lines[insert_after:]
    return new_lines, source

# ── Creator-file discovery (NOT hard-coded to core_*) ──────────────────────────
def top_level_block_names(content, block):
    """Return names defined under a top-level `networks:` or `volumes:` block."""
    names = []
    in_block = False
    for line in content.splitlines():
        if re.match(rf'^{block}:\s*$', line):
            in_block = True
            continue
        if in_block and re.match(r'^[a-zA-Z]', line) and not line.startswith(' '):
            in_block = False
        if not in_block:
            continue
        m = re.match(r'^  ([a-zA-Z0-9][a-zA-Z0-9_.\-]*):', line)
        if m:
            names.append(m.group(1))
    return names

def real_defined_nets(stacks_dir, skip_files=None):
    """Networks defined WITH a subnet/ipam (true creators) across ALL files."""
    defined = set()
    skip_files = skip_files or set()
    for f in sorted(os.listdir(stacks_dir)):
        if not f.endswith(('.yml','.yaml')): continue
        if f in skip_files: continue
        try: content = open(os.path.join(stacks_dir,f)).read()
        except Exception: continue
        in_block = False
        for line in content.splitlines():
            if re.match(r'^networks:\s*$', line): in_block=True; continue
            if in_block and re.match(r'^[a-zA-Z]', line) and not line.startswith(' '): in_block=False
            if not in_block: continue
            m = re.match(r'^  ([a-zA-Z0-9][a-zA-Z0-9_.\-]*):(.*)$', line)
            if m and ('subnet' in m.group(2) or 'ipam' in m.group(2)):
                defined.add(m.group(1))
    return defined

def real_defined_vols(stacks_dir, skip_files=None):
    """Volumes defined anywhere as a top-level entry."""
    defined = set()
    skip_files = skip_files or set()
    for f in sorted(os.listdir(stacks_dir)):
        if not f.endswith(('.yml','.yaml')): continue
        if f in skip_files: continue
        try: content = open(os.path.join(stacks_dir,f)).read()
        except Exception: continue
        in_block = False
        for line in content.splitlines():
            if re.match(r'^volumes:\s*$', line): in_block=True; continue
            if in_block and re.match(r'^[a-zA-Z]', line) and not line.startswith(' '): in_block=False
            if not in_block: continue
            m = re.match(r'^  ([a-zA-Z0-9][a-zA-Z0-9_.\-]*):', line)
            if m: defined.add(m.group(1))
    return defined

def discover_creator_files(stacks_dir, skip_files=None):
    """
    A "creator file" is any compose file that defines named entries under a
    top-level networks: or volumes: block. Returns dict:
       path -> {"nets": set(...), "vols": set(...), "size": bytes}
    Independent of filename, so it works on any setup.
    """
    creators = {}
    skip_files = skip_files or set()
    for f in sorted(os.listdir(stacks_dir)):
        if not (f.endswith('.yml') or f.endswith('.yaml')):
            continue
        if f in skip_files:
            continue
        path = os.path.join(stacks_dir, f)
        try:
            content = open(path).read()
        except Exception:
            continue
        nets = set(top_level_block_names(content, 'networks'))
        vols = set(top_level_block_names(content, 'volumes'))
        if nets or vols:
            creators[path] = {"nets": nets, "vols": vols,
                              "size": os.path.getsize(path)}
    return creators

def smallest_file_overall(stacks_dir):
    """Find smallest yml file that has a provisioner container.
    Falls back to smallest creator file with a networks: section.
    Never picks files without a provisioner or networks: block."""
    best = None; best_size = float('inf')
    fallback = None; fallback_size = float('inf')
    for f in sorted(os.listdir(stacks_dir)):
        if not (f.endswith('.yml') or f.endswith('.yaml')):
            continue
        path = os.path.join(stacks_dir, f)
        try:
            content = open(path).read()
        except: continue
        sz = os.path.getsize(path)
        # Prefer files with provisioner container
        if re.search(r'container_name:\s*provisioner', content):
            if sz < best_size:
                best_size = sz; best = path
        # Fallback: files with networks: section
        elif re.search(r'^networks:\s*$', content, re.MULTILINE):
            if sz < fallback_size:
                fallback_size = sz; fallback = path
    return best or fallback

def all_used_subnets(creators, subnet_base):
    """Scan every creator file for used 3rd octets in <base>.<N>.0/24."""
    used = set()
    esc = re.escape(subnet_base)
    pat = re.compile(rf'{esc}\.(\d{{1,3}})\.0/24')
    for path in creators:
        try:
            for m in pat.finditer(open(path).read()):
                used.add(int(m.group(1)))
        except Exception:
            pass
    return used

def next_subnet_octet(used):
    """Gap-fill from 1..254; if full, climb above the highest."""
    for n in range(1, 255):
        if n not in used:
            return n
    return (max(used) + 1) if used else 1

# ── Service reference collection ───────────────────────────────────────────────
def collect_service_refs(stacks_dir, creators, skip_files=None):
    """
    Walk every NON-creator service file and gather the network & volume names
    that services actually reference. Names taken verbatim. Returns
    (needed_nets:set, needed_vols:set).
    """
    needed_nets = set()
    needed_vols = set()
    creator_paths = set(creators.keys())
    skip_files = skip_files or set()

    for f in sorted(os.listdir(stacks_dir)):
        if not (f.endswith('.yml') or f.endswith('.yaml')):
            continue
        if f in skip_files:
            continue
        path = os.path.join(stacks_dir, f)
        if path in creator_paths:
            # creators define; we read their service refs too, but skip their
            # own top-level defs (handled separately)
            pass
        try:
            content = open(path).read()
        except Exception:
            continue

        # Networks referenced by services: under a service-level `networks:`
        # either list form (- foo_net) or mapping form (foo_net:)
        for m in re.finditer(r'^\s{4,6}-\s+"?([a-zA-Z0-9][a-zA-Z0-9_.\-]*_net)"?\s*$',
                             content, re.M):
            needed_nets.add(m.group(1))
        for m in re.finditer(r'^\s{6}([a-zA-Z0-9][a-zA-Z0-9_.\-]*_net):\s*$',
                             content, re.M):
            needed_nets.add(m.group(1))

        # Volumes referenced by services: "- name:/path" where name is a
        # NAMED volume (no leading slash, not a bind mount, no ./ or ~).
        # Anchored at end + URL-scheme guard so healthcheck commands like
        # - "wget -qO- http://localhost:8080/ || exit 1" are NOT misread.
        for m in re.finditer(r'^\s{4,8}-\s+"?([a-zA-Z0-9][a-zA-Z0-9_.\-]*):(/[^"\s]+)"?\s*$',
                             content, re.M):
            vol = m.group(1)
            path_part = m.group(2)
            if vol.startswith(('.', '/', '~')):
                continue
            if vol in ('http', 'https', 'ftp', 'ws', 'wss', 'tcp', 'udp'):
                continue
            if '//' in path_part:
                continue
            needed_vols.add(vol)

    return needed_nets, needed_vols

# ── Depends-on injection ─────────────────────────────────────────────────────
DB_SUFFIXES = ('-db', '-database', '-postgres', '-mysql', '-mongo', '-mariadb',
               '_db', '_database', '_postgres', '_mysql', '_mongo', '_mariadb',
               '-sqlite', '-clickhouse', '-cassandra', '-couchdb', '-dynamodb')
CACHE_SUFFIXES = ('-redis', '-cache', '-memcached', '-valkey', '_redis', '_cache')
WORKER_SUFFIXES = ('-worker', '-celery', '-beat', '-scheduler', '_worker', '_celery',
                   '-realtime', '-agent', '-proxy', '-exporter', '-cron', '-daemon',
                   '-sidekiq', '-resque', '-queue', '-consumer', '-listener',
                   '_realtime', '_agent', '_proxy', '_cron', '_daemon')

def classify_container(name):
    """
    Classify container role: db, cache, worker, companion, or app.
    - db: database containers (postgres, mysql, mongo etc)
    - cache: redis, memcached etc
    - worker: background workers, celery, realtime companions
    - app: main application container
    """
    n = name.lower()
    for s in DB_SUFFIXES:
        if n.endswith(s): return 'db'
    for s in CACHE_SUFFIXES:
        if n.endswith(s): return 'cache'
    for s in WORKER_SUFFIXES:
        if n.endswith(s): return 'worker'
    return 'app'

def inject_depends_on(fpath, cfg):
    """
    Auto-inject depends_on for related containers using stacks_families algorithm.
    The main app (family head) gets depends_on listing all other family members.
    FIX_AUTO_DEPENDS=1  : inject missing depends_on
    FIX_FORCE_DEPENDS=1 : remove and re-inject all depends_on in families
    FIX_AUTO_DEPENDS=0  : remove all depends_on from family heads (cleanup mode)
    """
    auto = cfg.get("FIX_AUTO_DEPENDS","0") == "1"
    force = cfg.get("FIX_FORCE_DEPENDS","0") == "1"
    remove_all = cfg.get("FIX_REMOVE_DEPENDS","0") == "1"
    if not auto and not force and not remove_all:
        return []
    # Remove-only mode: strip all depends_on from this file
    if remove_all:
        data = open(fpath).read()
        lines = data.splitlines(keepends=True)
        new_lines = []
        in_dep = False
        for l in lines:
            if re.match(r"    depends_on:", l): in_dep = True; continue
            if in_dep:
                if re.match(r"      [-{]", l): continue
                else: in_dep = False
            new_lines.append(l)
        if len(new_lines) != len(lines):
            open(fpath, "w").writelines(new_lines)
            return [f"removed depends_on from {fpath.split('/')[-1]}"]
        return []
    notes = []
    try:
        import sys as _sys
        _sys.path.insert(0, '/usr/local/lib')
        from stacks_families import get_families, get_family_of, is_support
        # Get all families globally
        all_families = get_families()
        if not all_families: return []
        # Get containers in THIS file
        data = open(fpath).read()
        cnames = [c.strip().strip('"').strip("'") for c in re.findall(r'container_name:\s*(\S+)', data)]
        if not cnames: return []
        lines = data.splitlines(keepends=True)

        # Pre-pass: strip depends_on from non-head family members (cycle-proof)
        _strip = set()
        for _cn in cnames:
            _h, _m = get_family_of(_cn)
            if _h and _h != _cn:
                _strip.add(_cn)
        for _cn in _strip:
            _idx = data.find("container_name: " + _cn)
            if _idx < 0:
                continue
            _ln = data[:_idx].count(chr(10))
            _out = []; _ind = False
            for _j, _l in enumerate(lines):
                if (not _ind) and _ln <= _j < _ln + 60 and re.match(r"    depends_on:", _l):
                    _ind = True
                    notes.append("stripped depends_on from " + _cn)
                    continue
                if _ind:
                    if re.match(r"      [-{]", _l):
                        continue
                    _ind = False
                _out.append(_l)
            lines = _out
            data = "".join(lines)

        for cname in cnames:
            head, members = get_family_of(cname)
            if not head or head != cname: continue  # only process family heads
            if cname not in cnames: continue  # head must be in this file
            # Build deps list - all family members in ANY file except head
            deps = sorted(m for m in members if m != cname)
            if not deps: continue
            # Find insertion point
            idx = data.find(f"container_name: {cname}")
            if idx < 0: continue
            line_num = data[:idx].count("\n")
            # Find image: line to insert after
            insert_after = line_num
            for j in range(line_num, min(line_num+15, len(lines))):
                if re.match(r"    image:\s*", lines[j]):
                    insert_after = j; break
            # Check existing depends_on
            block = "".join(lines[line_num:line_num+60])
            has_deps = "depends_on:" in block
            if has_deps and not force: continue
            # Remove existing if force
            if has_deps and force:
                new_lines = []
                in_dep = False
                for j, l in enumerate(lines):
                    if j < line_num: new_lines.append(l); continue
                    if "depends_on:" in l and j > line_num and j < line_num+60:
                        in_dep = True; continue
                    if in_dep:
                        if re.match(r"      [-{]", l): continue
                        else: in_dep = False
                    new_lines.append(l)
                lines = new_lines
                data = "".join(lines)
                line_num = data[:data.find(f"container_name: {cname}")].count("\n")
                insert_after = line_num
                for j in range(line_num, min(line_num+15, len(lines))):
                    if re.match(r"    image:\s*", lines[j]):
                        insert_after = j; break
            # Inject depends_on
            dep_lines = ["    depends_on:\n"] + [f"      - {d}\n" for d in deps]
            lines = lines[:insert_after+1] + dep_lines + lines[insert_after+1:]
            data = "".join(lines)
            notes.append(f"depends_on: {cname} -> {deps}")

        # ── Include injection: for any dep member NOT in this file, add include
        #    for the stack file that defines it. Guards: dedup, no-self, no-cycle.
        if on(cfg.get("FIX_DEPENDS_INCLUDES", "1")):
            data = "".join(lines)
            this_file = os.path.basename(fpath)
            local_cn = set(re.findall(r'container_name:\s*(\S+)', data))
            local_cn = {c.strip().strip(chr(34)).strip(chr(39)) for c in local_cn}
            sd2 = os.path.dirname(fpath)
            needed_files = set()
            for cname in cnames:
                head, members = get_family_of(cname)
                if not head or head != cname:
                    continue
                for m in members:
                    if m == cname or m in local_cn:
                        continue
                    # find which file defines this cross-stack member
                    for fn in sorted(os.listdir(sd2)):
                        if not fn.endswith((".yml", ".yaml")) or fn == this_file:
                            continue
                        try:
                            fdata = open(os.path.join(sd2, fn)).read()
                        except OSError:
                            continue
                        if re.search(r'container_name:\s*[\"\x27]?' + re.escape(m) + r'[\"\x27]?\s', fdata):
                            needed_files.add(fn)
                            break
            # no-cycle guard: skip a file that already includes THIS file
            safe_inc = []
            for fn in sorted(needed_files):
                try:
                    tdata = open(os.path.join(sd2, fn)).read()
                except OSError:
                    continue
                if re.search(r'include:.*' + re.escape(this_file), tdata, re.S):
                    notes.append(f"include SKIPPED (would cycle): {fn}")
                    continue
                safe_inc.append(fn)
            # dedup against existing includes already in this file
            existing_inc = set(re.findall(r'-\s*\S*/([^/\n]+\.ya?ml)', data))
            existing_inc |= set(re.findall(r'include:\s*\n(?:\s*-\s*\S+\n)*', data))
            to_add = [fn for fn in safe_inc if fn not in data]
            if to_add:
                lines2 = "".join(lines).splitlines(keepends=True)
                # insert include block after the name: line (or at top)
                ins = 0
                for _i, _l in enumerate(lines2):
                    if re.match(r'name:\s*', _l):
                        ins = _i + 1; break
                if "include:" in data:
                    # append to existing include block
                    for _i, _l in enumerate(lines2):
                        if re.match(r'include:\s*$', _l):
                            blk = [f"  - {sd2}/{fn}\n" for fn in to_add]
                            lines2 = lines2[:_i+1] + blk + lines2[_i+1:]
                            break
                else:
                    blk = ["include:\n"] + [f"  - {sd2}/{fn}\n" for fn in to_add]
                    lines2 = lines2[:ins] + blk + lines2[ins:]
                lines = lines2
                for fn in to_add:
                    notes.append(f"include added: {fn}")

        if notes:
            open(fpath, "w").writelines(lines)
        return notes
    except Exception as e:
        return [f"depends_on error: {e}"]

    # OLD CODE BELOW - replaced by families algorithm
    if False:
        groups = []

        lines = open(fpath).readlines()
        content = "".join(lines)

        for group in groups:
            # Classify each member
            roles = {name: classify_container(name) for name in group}
            apps    = [n for n,r in roles.items() if r == 'app']
            dbs     = [n for n,r in roles.items() if r == 'db']
            caches  = [n for n,r in roles.items() if r == 'cache']
            workers = [n for n,r in roles.items() if r == 'worker']

            # Skip groups with no app or worker - pure db groups
            if not apps and not workers:
                continue
            # Skip groups where group is too large (>8) - likely false positive
            if len(group) > 8:
                continue

            # Build depends map: ONLY main app gets depends_on
            # Main app = shortest name in group (coolify vs coolify-realtime)
            # deps = all workers + dbs + caches that share prefix with app
            deps_map = {}

            def shares_prefix(a, b):
                """True if a and b share a meaningful name prefix."""
                pa = a.split('-')[0].split('_')[0]
                pb = b.split('-')[0].split('_')[0]
                return pa == pb or a.startswith(pb) or b.startswith(pa)

            for app in apps:
                # Find workers/dbs/caches that belong to THIS app by prefix
                my_workers = [w for w in workers if shares_prefix(app, w)]
                my_dbs     = [d for d in dbs     if shares_prefix(app, d)]
                my_caches  = [c for c in caches   if shares_prefix(app, c)]
                deps = my_workers + my_dbs + my_caches
                if deps:
                    deps_map[app] = deps
            # Workers do NOT get depends_on (only app does)

            for cname, dep_list in deps_map.items():
                # Find container block
                idx = content.find(f"container_name: {cname}")
                if idx < 0: continue

                # Check if already has depends_on
                block_end = content.find("\n  ", idx+1)
                if block_end < 0: block_end = len(content)
                block = content[idx:idx+2000]
                if "depends_on" in block and not force:
                    continue

                # Find line number of container_name
                line_num = content[:idx].count("\n")

                # Find insertion point - after image: line or after container_name
                insert_after = None
                for i in range(line_num, min(line_num+30, len(lines))):
                    if re.match(r"    image:\s*", lines[i]):
                        insert_after = i
                        break
                if insert_after is None:
                    insert_after = line_num + 1

                # Build depends_on block
                dep_lines = ["    depends_on:\n"]
                for dep in dep_list:
                    dep_lines.append(f"      - {dep}\n")

                # Remove existing depends_on if force
                if force and "depends_on" in block:
                    new_lines = []
                    in_deps = False
                    for i, l in enumerate(lines):
                        if i < line_num: new_lines.append(l); continue
                        if "depends_on:" in l and i > line_num: in_deps = True; continue
                        if in_deps:
                            if re.match(r"      - ", l): continue
                            else: in_deps = False
                        new_lines.append(l)
                    lines = new_lines
                    content = "".join(lines)
                    line_num = content[:content.find(f"container_name: {cname}")].count("\n")
                    insert_after = line_num + 1


# ── Network/volume definition templates ────────────────────────────────────────
def net_definition(name, octet, subnet_base):
    base = name[:-4] if name.endswith('_net') else name
    return (
        f"  {name}: {{name: {name}, driver: bridge, attachable: true, "
        f"external: false, internal: false, enable_ipv6: false, "
        f"labels: [\"com.stacks.network={base}\", "
        f"\"com.stacks.env=production\"], "
        f"ipam: {{driver: default, config: [{{subnet: {subnet_base}.{octet}.0/24, "
        f"gateway: {subnet_base}.{octet}.1}}]}}}}\n"
    )

def vol_definition(name, external=True):
    if external:
        return f"  {name}: {{name: {name}, external: true}}\n"
    else:
        return f"  {name}: {{name: {name}, external: false}}\n"

def find_provisioner_block(lines):
    """Return (start_idx, end_idx) of the first provisioner_* service block, or None."""
    in_services = False
    for i, line in enumerate(lines):
        if re.match(r'^services:\s*$', line.rstrip()):
            in_services = True
            continue
        if in_services:
            m = re.match(r'^  (provisioner[a-zA-Z0-9_.\-]*):\s*$', line.rstrip())
            if m:
                # find block end
                for j in range(i+1, len(lines)):
                    if re.match(r'^  [a-zA-Z0-9]', lines[j]) or \
                       (re.match(r'^[a-zA-Z]', lines[j]) and not lines[j].startswith(' ')):
                        return (i, j)
                return (i, len(lines))
    return None

def add_to_creator(path, new_nets, new_vols, subnet_base, used_subnets, dry_run):
    """
    Append network/volume definitions to a creator file's top-level blocks,
    and sync them into that file's provisioner_* service lists.
    Inserts only — never deletes existing lines.
    """
    content = open(path).read()
    lines = content.split('\n')
    notes = []

    existing_nets = set(top_level_block_names(content, 'networks'))
    existing_vols = set(top_level_block_names(content, 'volumes'))

    # ---- Networks ----
    def insert_after_block_header(lines, header, payload_lines):
        for i, l in enumerate(lines):
            if re.match(rf'^{header}:\s*$', l.rstrip()):
                return lines[:i+1] + payload_lines + lines[i+1:]
        return None

    for net in sorted(new_nets):
        if net in existing_nets:
            continue
        octet = next_subnet_octet(used_subnets)
        used_subnets.add(octet)
        payload = [net_definition(net, octet, subnet_base).rstrip('\n')]
        res = insert_after_block_header(lines, 'networks', payload)
        if res is None:
            # No top-level networks: block — create one before `services:`
            for i, l in enumerate(lines):
                if re.match(r'^services:\s*$', l.rstrip()):
                    lines = lines[:i] + ['networks:'] + payload + [''] + lines[i:]
                    break
            else:
                lines = ['networks:'] + payload + [''] + lines
        else:
            lines = res
        existing_nets.add(net)
        notes.append(f"net {net} -> {subnet_base}.{octet}.0/24")

    # ---- Volumes ----
    for vol in sorted(new_vols):
        if vol in existing_vols:
            continue
        payload = [vol_definition(vol).rstrip('\n')]
        res = insert_after_block_header(lines, 'volumes', payload)
        if res is None:
            for i, l in enumerate(lines):
                if re.match(r'^services:\s*$', l.rstrip()):
                    lines = lines[:i] + ['volumes:'] + payload + [''] + lines[i:]
                    break
            else:
                lines = ['volumes:'] + payload + [''] + lines
        else:
            lines = res
        existing_vols.add(vol)
        notes.append(f"vol {vol}")

    # ---- Provisioner sync ----
    prov = find_provisioner_block(lines)
    if prov and (new_nets or new_vols):
        pstart, pend = prov
        block = lines[pstart:pend]
        # networks: list inside provisioner
        def ensure_in_list(block, key, items, quote=True):
            # find "    key:" line inside block
            for bi, bl in enumerate(block):
                if re.match(rf'^    {key}:\s*$', bl.rstrip()):
                    # gather existing entries
                    existing = set()
                    insert_at = bi + 1
                    for k in range(bi+1, len(block)):
                        mm = re.match(r'^      -\s+"?([^"\s]+)"?', block[k])
                        if mm:
                            existing.add(mm.group(1).split(':')[0])
                            insert_at = k + 1
                        elif re.match(r'^    [a-zA-Z]', block[k]):
                            break
                    adds = []
                    for it in sorted(items):
                        nm = it if not quote else it
                        if nm not in existing:
                            if key == 'volumes':
                                adds.append(f'      - "{it}:/provision/{it}"')
                            else:
                                adds.append(f'      - "{it}"')
                    if adds:
                        block = block[:insert_at] + adds + block[insert_at:]
                    return block
            return block
        block = ensure_in_list(block, 'networks', new_nets)
        block = ensure_in_list(block, 'volumes', new_vols)
        lines = lines[:pstart] + block + lines[pend:]

    new_content = '\n'.join(lines)
    if new_content != content and notes:
        if dry_run:
            pr(f"  {Y}[dry-run] would add to {os.path.basename(path)}: "
               f"{'; '.join(notes)}{X}")
        else:
            _backup(path)
            open(path, 'w').write(new_content)
            pr(f"  {G}✔ {os.path.basename(path)}: added {'; '.join(notes)}{X}")
        return len(notes)
    return 0

# ── Typo healing in creator files (safe set only) ──────────────────────────────
def heal_creator_typos(path, dry_run):
    """Fix known safe typos in creator files. Names are never touched."""
    content = open(path).read()
    original = content
    fixes = []

    # external: <typo-of-true/false>  -> nearest of true/false (edit dist <=2)
    def near(word, target):
        # tiny Levenshtein
        a, b = word, target
        if abs(len(a)-len(b)) > 2:
            return 99
        prev = list(range(len(b)+1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j]+1, cur[-1]+1,
                               prev[j-1] + (ca != cb)))
            prev = cur
        return prev[-1]

    def fix_external(m):
        val = m.group(1)
        if val in ('true', 'false'):
            return m.group(0)
        cand = min(('true', 'false'), key=lambda t: near(val, t))
        if near(val, cand) <= 2:
            fixes.append(f"external: {val} -> {cand}")
            return m.group(0).replace(val, cand)
        return m.group(0)

    content = re.sub(r'external:\s*([A-Za-z]+)', fix_external, content)

    if content != original and fixes:
        if dry_run:
            pr(f"  {Y}[dry-run] would heal {os.path.basename(path)}: "
               f"{'; '.join(fixes)}{X}")
        else:
            _backup(path)
            open(path, 'w').write(content)
            pr(f"  {G}✔ healed {os.path.basename(path)}: {'; '.join(fixes)}{X}")
        return len(fixes)
    return 0

# ── Healthcheck pass on one file ───────────────────────────────────────────────
def fix_healthchecks(path, cfg, target_svc, dry_run, replace_broken=False):
    deep = on(cfg["FIX_DEEP_INSPECT"])
    skip = set(cfg["FIX_HC_SKIP"].split())
    services, lines = parse_services_with_positions(path)
    if target_svc:
        services = [s for s in services if s['name'] == target_svc]

    changes = 0
    for svc in reversed(services):  # reverse keeps line numbers valid
        if not svc['image']:
            continue
        # Never healthcheck idle holders (provisioners, bare alpine sleepers)
        if svc['name'].startswith('provisioner') or re.match(r'^alpine(:|$)', svc['image'].strip()):
            pr(f"  {C}  {svc['name']}: idle holder, skipping{X}")
            continue
        if svc['name'] in skip:
            pr(f"  {C}  {svc['name']}: in skip-list, leaving alone{X}")
            continue
        if svc['has_healthcheck']:
            # Check if we should replace actively-failing healthchecks
            _replaced = False
            if replace_broken and svc['name']:
                try:
                    _ri = subprocess.run(
                        ["docker", "inspect", svc['name'], "--format",
                         "{{if .State.Health}}{{.State.Health.Status}}|{{.State.Health.FailingStreak}}{{end}}"],
                        capture_output=True, text=True, timeout=5)
                    _parts = (_ri.stdout.strip() or "|0").split("|")
                    _hc_status = _parts[0] if _parts else ""
                    _failing = int(_parts[1] if len(_parts) > 1 else "0")
                    if _hc_status == "unhealthy" or _failing > 0:
                        _new_hc = hc_from_inspect(svc['name'], svc.get('image', ''))
                        if not _new_hc:
                            _new_hc = hc_from_pattern(svc.get('image', ''), svc.get('ports', []))
                        if _new_hc:
                            if dry_run:
                                pr(f"  {Y}  [dry-run] BROKEN HC on {svc['name']} (failing:{_failing}) → {_new_hc[5]}{X}")
                                changes += 1
                                _replaced = True
                            else:
                                _lines2, _changed = replace_hc_in_service(lines, svc, _new_hc)
                                if _changed:
                                    lines = _lines2
                                    pr(f"  {G}  ✔ {svc['name']}: broken HC replaced → {_new_hc[5]}{X}")
                                    changes += 1
                                    _replaced = True
                except Exception as _e:
                    pass
            if not _replaced:
                pr(f"  {C}  {svc['name']}: already has healthcheck — NOT touched{X}")
            continue
        if dry_run:
            _, src = choose_healthcheck(svc, deep), None
            pr(f"  {Y}[dry-run] would add healthcheck to {svc['name']}{X}")
            changes += 1
            continue
        lines, source = inject_hc_into_service(lines, svc, deep)
        changes += 1
        pr(f"  {G}💉 {svc['name']}: healthcheck added ({source}){X}")

    if changes > 0 and not dry_run:
        _backup(path)
        open(path, 'w').writelines(lines)
    return changes

# ── Main ────────────────────────────────────────────────────────────────────────
def strip_profiles_from_file(filepath, dry_run=False):
    """Remove profiles: blocks from a compose file. Returns True if changed."""
    try:
        content = open(filepath).read()
    except:
        return False
    lines = content.split('\n')
    result = []
    skip_until_dedent = None
    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith('profiles:'):
            skip_until_dedent = indent
            continue
        if skip_until_dedent is not None:
            if stripped == '' or indent > skip_until_dedent or stripped.startswith('-'):
                continue
            else:
                skip_until_dedent = None
        result.append(line)
    new_content = '\n'.join(result)
    if new_content != content:
        if not dry_run:
            _backup(filepath)
            open(filepath, 'w').write(new_content)
        return True
    return False


def collapse_blank_lines(filepath, dry_run=False):
    """Fix all blank line issues: leading blanks, double-spacing, gaps in blocks."""
    import re as _re
    try:
        content = open(filepath).read()
    except:
        return False
    original = content
    # Strip leading blank lines
    content = content.lstrip('\n')
    # Collapse 3+ blank lines to 1
    content = _re.sub(r'\n{3,}', '\n\n', content)
    # Smart removal if still heavily gapped
    lines = content.split('\n')
    blank_count = sum(1 for l in lines if l.strip() == '')
    total = len(lines)
    if total > 10 and blank_count / total > 0.05:
        result = []
        for i, line in enumerate(lines):
            if line.strip() == '':
                prev = next((lines[j] for j in range(i-1,-1,-1) if lines[j].strip()), '')
                nxt = next((lines[j] for j in range(i+1,len(lines)) if lines[j].strip()), '')
                prev_i = len(prev) - len(prev.lstrip())
                nxt_i = len(nxt) - len(nxt.lstrip())
                if (_re.match(r'^    (blkio_config|storage_opt|ulimits|deploy):', prev)
                        and _re.match(r'^  [a-zA-Z#]', nxt)):
                    result.append(line)
                elif (prev_i == 0 and nxt_i == 0
                      and not prev.startswith('#')
                      and not nxt.startswith('#')):
                    result.append(line)
            else:
                result.append(line)
        content = '\n'.join(result)
    if content != original:
        if not dry_run:
            open(filepath, 'w').write(content)
        return True
    return False

def remove_gaps_from_file(filepath, dry_run=False):
    """
    Remove blank lines inside service blocks and after art banners.
    Blank lines between top-level sections (services:, networks:, volumes:) are kept.
    """
    try:
        content = open(filepath).read()
    except:
        return False

    lines = content.split('\n')
    result = []
    in_service_block = False
    in_services_section = False
    changed = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Track services: section
        if re.match(r'^services:\s*$', line):
            in_services_section = True
            result.append(line)
            continue

        # Top-level keys end services section
        if in_services_section and re.match(r'^[a-zA-Z]', line) and not line.startswith(' '):
            in_services_section = False

        # Track service block (2-space indent service name)
        if in_services_section and re.match(r'^  [a-zA-Z0-9]', line):
            in_service_block = True

        # Remove blank lines inside service blocks
        if in_service_block and stripped == '':
            # Check if next non-blank line is still in a service block
            next_content = ''
            for j in range(i+1, min(i+5, len(lines))):
                if lines[j].strip():
                    next_content = lines[j]
                    break
            # Keep blank line if next line is a new top-level service or section
            if next_content and re.match(r'^  [a-zA-Z0-9]', next_content):
                # New service starting — keep one blank line as separator
                result.append(line)
            elif next_content and re.match(r'^[a-zA-Z#]', next_content):
                # Top level — keep
                result.append(line)
            else:
                # Inside service block — remove the blank line
                changed = True
                continue
        else:
            result.append(line)

    new_content = '\n'.join(result)

    # Also remove multiple consecutive blank lines in comment/header area (art banner gaps)
    import re as _re
    new_content2 = _re.sub(r'(^#.*$\n)\n(^#)', r'\1\2', new_content, flags=_re.MULTILINE)
    if new_content2 != new_content:
        new_content = new_content2
        changed = True

    if changed:
        if not dry_run:
            _backup(filepath)
            open(filepath, 'w').write(new_content)
        return True
    return False


def get_bind_mounts(svc_block_lines):
    """Extract host-side paths from bind mount volume entries."""
    mounts = []
    in_volumes = False
    for line in svc_block_lines:
        stripped = line.strip()
        if re.match(r'^volumes:\s*$', stripped):
            in_volumes = True
            continue
        if in_volumes:
            if stripped.startswith('-'):
                val = stripped.lstrip('- ').strip().strip('"').strip("'")
                if val.startswith('/'):
                    host_path = val.split(':')[0]
                    mounts.append(host_path)
            elif stripped and not stripped.startswith('#'):
                indent = len(line) - len(line.lstrip())
                if indent <= 4:
                    in_volumes = False
    return mounts

def create_volume_dirs(paths, dry_run=False):
    """Create host directories for bind mounts if they don't exist."""
    created = []
    for path in paths:
        # Never create dirs in /tmp, /proc, /sys, /dev
        if any(path.startswith(p) for p in ['/tmp', '/proc', '/sys', '/dev', '/run']):
            continue
        # Must start with / and be a reasonable path
        if not path.startswith('/'):
            continue
        if not os.path.exists(path):
            if dry_run:
                created.append(f"[dry-run] would create: {path}")
            else:
                try:
                    os.makedirs(path, mode=0o755, exist_ok=True)
                    created.append(f"created: {path}")
                except PermissionError:
                    # Try with sudo
                    try:
                        import subprocess as _sp
                        r = _sp.run(['sudo', 'mkdir', '-p', path],
                                   capture_output=True, timeout=5)
                        if r.returncode == 0:
                            created.append(f"created (sudo): {path}")
                        else:
                            created.append(f"failed (permission): {path}")
                    except Exception as e2:
                        created.append(f"failed: {path} ({e2})")
                except Exception as e:
                    created.append(f"failed: {path} ({e})")
    return created

def convert_named_to_bind(lines, vol_base, dry_run=False):
    """
    Convert named volume references to bind mounts.
    Uses parse_services_with_positions to correctly identify services.
    """
    import tempfile, os as _os

    # Write lines to temp file to use existing parser
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False)
    tmp.write('\n'.join(lines))
    tmp.close()

    try:
        services, _ = parse_services_with_positions(tmp.path if hasattr(tmp, 'path') else tmp.name)
    except:
        _os.unlink(tmp.name)
        return lines, 0
    _os.unlink(tmp.name)

    # Build set of volumes declared external: true at top level
    external_vols = set()
    in_top_vols = False
    current_vol = None
    for line in lines:
        if re.match(r'^volumes:\s*$', line):
            in_top_vols = True; current_vol = None; continue
        if in_top_vols:
            if line and not line[0].isspace():
                in_top_vols = False; current_vol = None; continue
            m = re.match(r'^  ([a-zA-Z0-9_-]+):', line)
            if m: current_vol = m.group(1)
            if current_vol and 'external: true' in line:
                external_vols.add(current_vol)

    # Build map of named_vol -> (svc_name, container_path)
    named_vols = {}
    for svc in services:
        svc_name = svc['name']
        in_vol = False
        for i in range(svc['block_start'], min(svc['block_end']+1, len(lines))):
            line = lines[i]
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            # Enter volumes block
            if re.match(r'^\s+volumes:\s*$', line):
                in_vol = True
                continue
            # Exit volumes block
            if in_vol and stripped and not stripped.startswith('-') and not stripped.startswith('#'):
                if indent <= 4:
                    in_vol = False
            if in_vol and stripped.startswith('-'):
                val = stripped.lstrip('- ').strip().strip('"').strip("'")
                if ':' in val and not val.startswith('/') and not val.startswith('.'):
                    parts = val.split(':')
                    vol_name = parts[0].strip()
                    cpath = ':'.join(parts[1:]).strip()
                    if (vol_name and not re.match(r'^[0-9]', vol_name)
                            and '.' not in vol_name
                            and ' ' not in vol_name
                            and re.match(r'^[a-zA-Z0-9_-]+$', vol_name)
                            and vol_name not in external_vols):
                        named_vols[vol_name] = (svc_name, cpath)

    if not named_vols:
        return lines, 0

    # Replace named vol references with bind mounts
    result = []
    changes = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('-'):
            val = stripped.lstrip('- ').strip().strip('"').strip("'")
            if ':' in val and not val.startswith('/') and not val.startswith('.'):
                parts = val.split(':')
                vol_name = parts[0].strip()
                cpath = ':'.join(parts[1:]).strip()
                if (vol_name in named_vols and not re.match(r'^[0-9]', vol_name)
                        and re.match(r'^[a-zA-Z0-9_-]+$', vol_name)):
                    svc_name = named_vols[vol_name][0]
                    new_host = _os.path.join(vol_base, svc_name)
                    indent = len(line) - len(line.lstrip())
                    new_line = ' ' * indent + f'- "{new_host}:{cpath}"'
                    result.append(new_line if not dry_run else line)
                    changes += 1
                    continue
        result.append(line)

    return result, changes


# Dependency suffixes that identify non-master services
# Dependency suffixes that identify non-master services
_DEP_SUFFIXES = [
    'db', 'database', 'postgres', 'postgresql', 'mysql', 'mariadb', 'mongo',
    'mongodb', 'redis', 'cache', 'worker', 'backend', 'daemon', 'cron',
    'celery', 'mq', 'queue', 'search', 'indexer', 'storage', 'realtime',
    'hub', 'agent', 'fresh', 'data', 'exporter', 'api', 'proxy',
    'registryctl', 'registry', 'jobservice', 'opensearch', 'rabbitmq',
    'memcached', 'clickhouse', 'vault', 'broker',
]

def is_dep_service(name):
    """Check if a service name looks like a dependency (db, redis, worker etc)."""
    # Normalize: replace _ with - and split
    parts = name.replace('_', '-').split('-')
    # Check if any suffix part matches dep suffixes
    for i in range(1, len(parts)):
        suffix = '-'.join(parts[i:])
        if suffix in _DEP_SUFFIXES:
            return True
        # Also check last part alone
        if parts[-1] in _DEP_SUFFIXES:
            return True
    return False

def get_all_groups_global(all_files):
    """
    Build global group map using stacks_families algorithm.
    Returns {head: {net_name: str, members_by_file: {filename: [svc_names]}}}
    """
    import glob as _gl2
    try:
        from stacks_families import get_families
        families = get_families()
    except Exception:
        families = {}

    # Build container->file index
    import re as _re
    cname_to_file = {}
    for f in all_files:
        try:
            data = open(f).read()
            for c in _re.findall(r'container_name:\s*(\S+)', data):
                cname_to_file[c.strip().strip('"\'\'')] = f
        except: pass

    result = {}
    for head, members in families.items():
        # Network name uses common root (first segment), not full head name
        _root = head.replace('.', '-').replace('_', '-').split('-')[0]
        net_name = f"{_root}_net"
        members_by_file = {}
        for m in members:
            f = cname_to_file.get(m)
            if f:
                if f not in members_by_file:
                    members_by_file[f] = []
                members_by_file[f].append(m)
        if members_by_file:
            result[head] = {
                'net_name': net_name,
                'members_by_file': members_by_file,
                'all_members': list(members),
            }
    return result

def get_service_groups(services):
    """
    Group services by shared name prefix.
    Returns dict: {prefix: {master: name, members: [names]}}
    """
    groups = {}
    for svc in services:
        # Normalize and get prefix (first word)
        norm = svc.replace('_', '-')
        prefix = norm.split('-')[0]
        if prefix not in groups:
            groups[prefix] = []
        groups[prefix].append(svc)

    result = {}
    for prefix, members in groups.items():
        if len(members) < 2:
            continue
        # Find master - shortest name that is NOT a dep service
        non_deps = [s for s in members if not is_dep_service(s)]
        deps = [s for s in members if is_dep_service(s)]

        if non_deps:
            # Master is the shortest non-dep (usually the main app)
            master = sorted(non_deps, key=len)[0]
            net_name = f"{master}_net".replace('-', '_')
        else:
            # All members are deps (master lives in another stack)
            # Use just the prefix: coolify_net not coolify_db_net
            master = prefix
            net_name = f"{prefix}_net"

        result[prefix] = {'master': master, 'net_name': net_name, 'members': members}

    return result


def inject_network_into_service(lines, svc, net_name, priority, dry_run=False):
    """Add a network to a service block if not already present."""
    # Check if network already in service
    block = lines[svc['block_start']:svc['block_end']+1]
    block_text = chr(10).join(block)
    if net_name in block_text:
        return lines, False
    # Skip services using network_mode: service:* (they share another container's network)
    if any('network_mode:' in l for l in block):
        return lines, False

    # Find networks: block in service
    net_start = None
    for i in range(svc['block_start'], min(svc['block_end']+1, len(lines))):
        if re.match(r'^    networks:\s*$', lines[i]):
            net_start = i
            break

    new_lines = list(lines)
    net_entry = f"      {net_name}:\n        priority: {priority}"

    if net_start is not None:
        # Insert after networks: header, no blank lines
        insert_at = net_start + 1
        # Skip any existing entries to insert at end of networks block
        while insert_at < len(new_lines) and re.match(r'^      ', new_lines[insert_at]):
            insert_at += 1
        new_lines.insert(insert_at, f"        priority: {priority}")
        new_lines.insert(insert_at, f"      {net_name}:")
    else:
        # Add networks: block before labels or healthcheck or end of service
        insert_at = svc['block_end']
        for i in range(svc['block_start'], min(svc['block_end']+1, len(lines))):
            if re.match(r'^    (labels|healthcheck|deploy|blkio):', lines[i]):
                insert_at = i
                break
        new_lines.insert(insert_at, f"        priority: {priority}")
        new_lines.insert(insert_at, f"      {net_name}:")
        new_lines.insert(insert_at, "    networks:")

    return new_lines, True


def ensure_network_declared(lines, net_name, subnet=None):
    """Ensure network is declared in the TOP-LEVEL networks: section of this file.
    Finds the EXISTING networks: section and adds there. Never creates a second one."""
    # Check if already declared anywhere in top-level networks section
    in_networks = False
    for line in lines:
        if re.match(r'^networks:\s*$', line):
            in_networks = True
            continue
        if in_networks:
            if re.match(r'^[a-zA-Z]', line) and not line.startswith(' '):
                in_networks = False
                continue
            if re.match(rf'^  {re.escape(net_name)}[\s:{{]', line.rstrip()):
                return lines, False  # already declared

    # Not declared — find the top-level networks: section and add to it
    net_section = None
    for i, line in enumerate(lines):
        if re.match(r'^networks:\s*$', line):
            # Make sure this is the ONLY/FIRST one
            net_section = i
            break

    new_entry = f"  {net_name}: {{driver: bridge, external: false}}"
    new_lines = list(lines)

    if net_section is not None:
        # Insert right after networks: line
        new_lines.insert(net_section + 1, new_entry)
        return new_lines, True
    else:
        # No networks: section at all — add one before services:
        for i, line in enumerate(new_lines):
            if re.match(r'^services:\s*$', line):
                new_lines.insert(i, new_entry)
                new_lines.insert(i, 'networks:')
                return new_lines, True
        # Fallback: append at end
        new_lines.append('networks:')
        new_lines.append(new_entry)
        return new_lines, True

def ensure_network_in_creator_file(net_name, stacks_dir, subnet_base="10.50"):
    """Add network declaration to the appropriate creator file."""
    creators = discover_creator_files(stacks_dir)
    
    # Check if network already declared in any creator file
    for cpath, cdata in creators.items():
        if net_name in cdata.get("nets", {}):
            return False  # already exists
    
    # Also check all files for this network name
    for f in os.listdir(stacks_dir):
        if not (f.endswith('.yml') or f.endswith('.yaml')):
            continue
        fpath = os.path.join(stacks_dir, f)
        content = open(fpath).read()
        if re.search(rf'^  {re.escape(net_name)}:', content, re.MULTILINE):
            return False  # already declared somewhere
    
    # Find target creator file
    if creators:
        # Use smallest creator file
        target = min(creators.keys(), key=lambda p: os.path.getsize(p))
    else:
        target = smallest_file_overall(stacks_dir)
    
    if not target:
        return False
    
    # Get next available subnet
    used = all_used_subnets(creators, subnet_base)
    octet = next_subnet_octet(used)
    subnet = f"{subnet_base}.{octet}.0/24"
    gateway = f"{subnet_base}.{octet}.1"
    
    prefix = net_name.replace('_net', '')
    _n = net_name
    _p = prefix
    _s = subnet
    _g = gateway
    new_entry = (
        f"  {_n}: " + "{" + f"name: {_n}, driver: bridge, "
        f"attachable: true, external: false, internal: false, "
        f"enable_ipv6: false, "
        f'labels: [\"com.stacks.network={_p}\", \"'
        f'com.stacks.env=production\"], '
        f"ipam: " + "{" + f"driver: default, config: [" + "{" +
        f"subnet: {_s}, gateway: {_g}" + "}]}}" + "}"
    )
    
    # Add to creator file's networks: section
    lines = open(target).readlines()
    new_lines = list(lines)
    net_section = None
    for i, line in enumerate(lines):
        if re.match(r'^networks:\s*$', line.rstrip()):
            net_section = i
            break
    
    if net_section is not None:
        new_lines.insert(net_section + 1, new_entry + chr(10))
    else:
        new_lines.append("networks:" + chr(10))
        new_lines.append(new_entry + chr(10))
    
    open(target, 'w').writelines(new_lines)
    return True

def load_global_inject_conf():
    """Load global inject settings from global_inject.conf."""
    conf_path = os.path.expanduser("~/.config/stacks/global_inject.conf")
    if not os.path.exists(conf_path):
        return {}
    cfg = {}
    with open(conf_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip()
    if str(cfg.get('INJECT_FILL_ALL','0')).lower() in ('1','true','force'):
        for _k in ('INJECT_DEPLOY','INJECT_BLKIO','INJECT_ULIMITS','INJECT_COMMON_CAPS',
                   'INJECT_HOSTNAME','INJECT_STORAGE_OPT','INJECT_MAC','INJECT_LABELS',
                   'INJECT_STOP_GRACE','INJECT_LOGGING','INJECT_CPUSET'):
            if _k not in cfg:
                cfg[_k] = '1'
    return cfg

def _gi_enabled(val):
    """Return True if value is '1' or 'force'."""
    return str(val).lower() in ('1', 'force', 'true')

def _gi_force(val):
    """Return True only if value is 'force'."""
    return str(val).lower() == 'force'

def _gi_is_forced(gi, key):
    """Check if a key is forced via FORCE_ALL or individual _FORCE flag."""
    if on(gi.get('FORCE_ALL', '0')):
        return True
    return on(gi.get(key + '_FORCE', '0'))


def inject_into_anchor(lines, gi, dry_run=False):
    """Inject anchor-targeted keys into x-common-caps block."""
    # Find anchor block start
    anchor_start = None
    anchor_end = None
    for i, line in enumerate(lines):
        if re.match(r'^x-common-caps:', line):
            anchor_start = i
            continue
        if anchor_start is not None and anchor_end is None:
            # End of anchor: next top-level key
            if line and not line[0].isspace() and not line.startswith('#'):
                anchor_end = i
                break
    if anchor_start is None:
        return lines, 0
    if anchor_end is None:
        anchor_end = len(lines)

    block = lines[anchor_start:anchor_end]
    block_text = chr(10).join(block)
    new_lines = list(lines)
    changes = 0
    inserts = []
    # Insert before dns: or restart: or at end of anchor
    insert_at = anchor_end
    for i in range(anchor_start, anchor_end):
        if re.match(r'^  (dns|restart|stop_grace):', lines[i]):
            insert_at = i
            break

    _sg = gi.get('INJECT_STOP_GRACE','0')
    if _gi_enabled(_sg):
        force = _gi_is_forced(gi, 'INJECT_STOP_GRACE')
        period = gi.get('STOP_GRACE_PERIOD','120s')
        signal = gi.get('STOP_SIGNAL','SIGTERM')
        if force and 'stop_grace_period:' in block_text:
            new_lines = [re.sub(r'^  stop_grace_period:.*', f"  stop_grace_period: {period}", l) for l in new_lines]
            changes += 1
        elif 'stop_grace_period:' not in block_text:
            inserts.append(f"  stop_grace_period: {period}")
            changes += 1
        if force and 'stop_signal:' in block_text:
            new_lines = [re.sub(r'^  stop_signal:.*', f"  stop_signal: {signal}", l) for l in new_lines]
            changes += 1
        elif 'stop_signal:' not in block_text:
            inserts.append(f"  stop_signal: {signal}")
            changes += 1

    _lg = gi.get('INJECT_LOGGING','0')
    if _gi_enabled(_lg):
        force = _gi_is_forced(gi, 'INJECT_LOGGING')
        driver = gi.get('LOGGING_DRIVER','json-file')
        maxsize = gi.get('LOGGING_MAX_SIZE','50m')
        maxfile = gi.get('LOGGING_MAX_FILE','5')
        log_line = f"  logging: {{driver: {driver}, options: {{max-size: {maxsize}, max-file: '{maxfile}'}}}}"
        if force and 'logging:' in block_text:
            new_lines = [log_line if re.match(r'^  logging:', l) else l for l in new_lines]
            changes += 1
        elif 'logging:' not in block_text:
            inserts.append(log_line)
            changes += 1

    _rs = gi.get('INJECT_RESTART','0')
    if _gi_enabled(_rs):
        force = _gi_is_forced(gi, 'INJECT_RESTART')
        policy = gi.get('RESTART_POLICY','unless-stopped')
        if force and 'restart:' in block_text:
            new_lines = [re.sub(r'^  restart:.*', f"  restart: {policy}", l) for l in new_lines]
            changes += 1
        elif 'restart:' not in block_text:
            inserts.append(f"  restart: {policy}")
            changes += 1

    if inserts and not dry_run:
        for ins in reversed(inserts):
            new_lines.insert(insert_at, ins)

    return new_lines, changes

def inject_global_keys(lines, svc, gi, dry_run=False, stack_prefix=''):
    """Inject global keys into a service block. Add-only unless force mode."""
    block = lines[svc['block_start']:svc['block_end']+1]
    block_text = chr(10).join(block)
    if '<<: *common-caps' in block_text or _gi_enabled(gi.get('INJECT_COMMON_CAPS','0')):
        _am = re.search(r'^x-common-caps:.*?(?=^\S)', chr(10).join(lines), re.MULTILINE | re.DOTALL)
        if _am:
            for _k in re.findall(r'^  ([a-z_]+):', _am.group(0), re.MULTILINE):
                if (_k + ':') not in block_text:
                    block_text += chr(10) + '    ' + _k + ':'
    new_lines = list(lines)
    changes = 0
    insert_before = svc['block_end']

    for i in range(svc['block_start'], min(svc['block_end']+1, len(lines))):
        if re.match(r'^    (blkio_config|deploy|labels|healthcheck):', lines[i]):
            insert_before = i
            break

    inserts = []

    _cc = gi.get('INJECT_COMMON_CAPS','0')
    if _gi_enabled(_cc):
        if '<<: *common-caps' not in block_text and '&common-caps' in chr(10).join(lines):
            inserts.append("    <<: *common-caps"); changes += 1

    _hn = gi.get('INJECT_HOSTNAME','0')
    if _gi_enabled(_hn) and 'hostname:' not in block_text:
        inserts.append("    hostname: " + svc['name']); changes += 1

    _so = gi.get('INJECT_STORAGE_OPT','0')
    if _gi_enabled(_so) and 'storage_opt:' not in block_text:
        inserts.append("    storage_opt: {size: " + gi.get('STORAGE_OPT_SIZE','10G') + "}"); changes += 1

    _sg = gi.get('INJECT_STOP_GRACE','0')
    if _gi_enabled(_sg):
        force = _gi_is_forced(gi, 'INJECT_STOP_GRACE')
        if force or 'stop_grace_period:' not in block_text:
            if force:
                new_lines = [re.sub(r'^    stop_grace_period:.*', f"    stop_grace_period: {gi.get('STOP_GRACE_PERIOD','120s')}", l) for l in new_lines]
            else:
                inserts.append(f"    stop_grace_period: {gi.get('STOP_GRACE_PERIOD','120s')}")
            changes += 1
        if force or 'stop_signal:' not in block_text:
            if force:
                new_lines = [re.sub(r'^    stop_signal:.*', f"    stop_signal: {gi.get('STOP_SIGNAL','SIGTERM')}", l) for l in new_lines]
            else:
                inserts.append(f"    stop_signal: {gi.get('STOP_SIGNAL','SIGTERM')}")
            changes += 1

    _lg = gi.get('INJECT_LOGGING','0')
    if _gi_enabled(_lg):
        force = _gi_is_forced(gi, 'INJECT_LOGGING')
        driver = gi.get('LOGGING_DRIVER','json-file')
        maxsize = gi.get('LOGGING_MAX_SIZE','50m')
        maxfile = gi.get('LOGGING_MAX_FILE','5')
        log_line = f"    logging: {{driver: {driver}, options: {{max-size: {maxsize}, max-file: '{maxfile}'}}}}"
        if force or 'logging:' not in block_text:
            if force and 'logging:' in block_text:
                new_lines = [log_line if re.match(r'^    logging:', l) else l for l in new_lines]
            else:
                inserts.append(log_line)
            changes += 1

    _dp = gi.get('INJECT_DEPLOY','0')
    if _gi_enabled(_dp):
        force = _gi_force(_dp)
        mem = gi.get('DEPLOY_MEMORY_LIMIT','2G')
        cpu = gi.get('DEPLOY_CPU_LIMIT','0.20')
        res = gi.get('DEPLOY_MEMORY_RESERVATION','256M')
        _plc = gi.get('DEPLOY_PLACEMENT_CONSTRAINT','').strip()
        _plc_str = f", placement: {{constraints: [{_plc}]}}" if _plc else ""
        dep_line = f"    deploy: {{resources: {{limits: {{memory: {mem}, cpus: '{cpu}'}}, reservations: {{memory: {res}}}}}{_plc_str}}}"
        if force or 'deploy:' not in block_text:
            if force and 'deploy:' in block_text:
                new_lines = [dep_line if re.match(r'^    deploy:', l) else l for l in new_lines]
            else:
                inserts.append(dep_line)
            changes += 1

    _bk = gi.get('INJECT_BLKIO','0')
    if _gi_enabled(_bk):
        force = _gi_force(_bk)
        w = gi.get('BLKIO_WEIGHT','500')
        r = gi.get('BLKIO_READ_BPS','750mb')
        wr = gi.get('BLKIO_WRITE_BPS','750mb')
        blk_line = f"    blkio_config: {{weight: {w}, device_read_bps: [{{path: /dev/nvme0n1, rate: {r}}}], device_write_bps: [{{path: /dev/nvme0n1, rate: {wr}}}]}}"
        if force or 'blkio_config:' not in block_text:
            if force and 'blkio_config:' in block_text:
                new_lines = [blk_line if re.match(r'^    blkio_config:', l) else l for l in new_lines]
            else:
                inserts.append(blk_line)
            changes += 1

    _ul = gi.get('INJECT_ULIMITS','0')
    if _gi_enabled(_ul):
        force = _gi_force(_ul)
        ns = gi.get('ULIMIT_NOFILE_SOFT','65535')
        nh = gi.get('ULIMIT_NOFILE_HARD','65535')
        np = gi.get('ULIMIT_NPROC','65535')
        ul_line = f"    ulimits: {{memlock: {{soft: -1, hard: -1}}, nofile: {{soft: {ns}, hard: {nh}}}, nproc: {np}}}"
        if force or 'ulimits:' not in block_text:
            if force and 'ulimits:' in block_text:
                new_lines = [ul_line if re.match(r'^    ulimits:', l) else l for l in new_lines]
            else:
                inserts.append(ul_line)
            changes += 1

    _rs = gi.get('INJECT_RESTART','0')
    if _gi_enabled(_rs):
        force = _gi_is_forced(gi, 'INJECT_RESTART')
        policy = gi.get('RESTART_POLICY','unless-stopped')
        if force or 'restart:' not in block_text:
            if force and 'restart:' in block_text:
                new_lines = [re.sub(r'^    restart:.*', f"    restart: {policy}", l) for l in new_lines]
            else:
                inserts.append(f"    restart: {policy}")
            changes += 1

    _mc = gi.get('INJECT_MAC','0')
    if _gi_enabled(_mc) and 'mac_address:' not in block_text:
        import hashlib as _hl
        _h = _hl.md5(svc['name'].encode()).hexdigest()
        inserts.append("    mac_address: 02:42:ac:11:%s:%s" % (_h[0:2], _h[2:4]))
        changes += 1

    _lb = gi.get('INJECT_LABELS','0')
    if _gi_enabled(_lb):
        _grp = stack_prefix or 'default'
        _want = [
            ('traefik.enable', 'true'),
            ('sablier.enable', 'true'),
            ('sablier.group', _grp),
        ]
        _missing = [(k, v) for (k, v) in _want
                    if ('%s=' % k) not in block_text and ('%s:' % k) not in block_text]
        if _missing:
            _lbl_idx = None
            for _i in range(svc['block_start'], min(svc['block_end'] + 1, len(new_lines))):
                if re.match(r'^    labels:\s*$', new_lines[_i]):
                    _lbl_idx = _i
                    break
            if _lbl_idx is not None:
                for _k, _v in reversed(_missing):
                    new_lines.insert(_lbl_idx + 1, '      - "%s=%s"' % (_k, _v))
                    changes += 1
            else:
                _blk = ['    labels:']
                for _k, _v in _missing:
                    _blk.append('      - "%s=%s"' % (_k, _v))
                inserts.extend(_blk)
                changes += 1

    if inserts and not dry_run:
        for ins in reversed(inserts):
            new_lines.insert(insert_before, ins)

    return new_lines, changes


def inject_cpuset(lines, svc, gi, stack_prefix, dry_run=False):
    """Inject cpuset and cpu_shares into a service. Add-only unless force."""
    block = lines[svc['block_start']:svc['block_end']+1]
    block_text = chr(10).join(block)
    new_lines = list(lines)
    changes = 0
    insert_before = svc['block_end']

    # Find insert point - before labels/healthcheck/deploy
    for i in range(svc['block_start'], min(svc['block_end']+1, len(lines))):
        if re.match(r'^    (labels|healthcheck|deploy|blkio):', lines[i]):
            insert_before = i
            break

    force_all = on(gi.get('FORCE_ALL','0'))
    force = force_all or on(gi.get('INJECT_CPUSET_FORCE','0'))

    # Determine cpuset for this prefix
    heavy_containers = gi.get('CPUSET_heavy_containers','').split()
    if svc['name'] in heavy_containers:
        cpuset = f"0-{(gi.get('CPUSET_all_cores') or str(__import__('os').cpu_count()-1))}"
        shares = gi.get('CPU_SHARES_heavy','4096')
    else:
        cpuset = gi.get(f'CPUSET_{stack_prefix}',
                 gi.get('CPUSET_default', '0'))
        shares = gi.get('CPU_SHARES_default','256')

    inserts = []
    if force or 'cpuset:' not in block_text:
        if force and 'cpuset:' in block_text:
            new_lines = [re.sub(r'^    cpuset:.*', f'    cpuset: "{cpuset}"', l) for l in new_lines]
            changes += 1
        else:
            inserts.append(f'    cpuset: "{cpuset}"')
            changes += 1

    if force or 'cpu_shares:' not in block_text:
        if force and 'cpu_shares:' in block_text:
            new_lines = [re.sub(r'^    cpu_shares:.*', f'    cpu_shares: {shares}', l) for l in new_lines]
            changes += 1
        else:
            inserts.append(f'    cpu_shares: {shares}')
            changes += 1

    if inserts and not dry_run:
        for ins in reversed(inserts):
            new_lines.insert(insert_before, ins)

    return new_lines, changes



# ── Post-build auto-inject ────────────────────────────────────────────────────
def post_build_inject_network(fpath, svc_name, cfg=None):
    """
    Called after build wizard. Auto-injects:
    1. mastername_net for the new container
    2. traefik_net
    3. Declares network in creator file
    Uses stacks_families to determine the family network name.
    """
    if cfg is None: cfg = load_conf()
    # Only run if explicitly enabled - default OFF to prevent unwanted network creation
    if cfg.get("BUILD_AUTO_NETWORK","0") != "1": return []
    notes = []
    try:
        from stacks_families import get_family_of
        import re as _re
        data = open(fpath).read()
        lines = [l.rstrip("\n") for l in open(fpath).readlines()]
        svcs, raw = parse_services_with_positions(fpath)
        svc = next((s for s in svcs if s["name"] == svc_name), None)
        if not svc: return []

        # Determine network name from family
        head, members = get_family_of(svc_name)
        if head:
            root = head.replace(".","_").replace("-","_").split("_")[0]
            net_name = f"{root}_net"
        else:
            root = svc_name.replace(".","_").replace("-","_").split("_")[0]
            net_name = f"{root}_net"

        # Inject mastername_net
        lines, changed = inject_network_into_service(lines, svc, net_name, 500, False)
        if changed:
            notes.append(f"added {net_name}")
        # Inject traefik_net
        lines, changed = inject_network_into_service(lines, svc, "traefik_net", 1000, False)
        if changed:
            notes.append("added traefik_net")

        # Add network to creator file using add_to_creator
        # This adds top-level def AND provisioner network list
        creator = find_or_create_creator(stacks_dir, cfg)
        if creator:
            _creators = discover_creator_files(stacks_dir)
            _used = all_used_subnets(_creators, cfg.get("FIX_SUBNET_BASE","10.50"))
            _r = add_to_creator(creator, {net_name}, set(),
                cfg.get("FIX_SUBNET_BASE","10.50"), _used, False)
            if _r: notes.append(f"added {net_name} to {os.path.basename(creator)}")
        lines, _ = ensure_network_declared(lines, net_name)
        lines, _ = ensure_network_declared(lines, net_name)

        if notes:
            open(fpath, "w").write("\n".join(lines) + "\n")
    except Exception as e:
        notes.append(f"network inject error: {e}")
    return notes


def post_build_inject_volume(fpath, svc_name, cfg=None):
    """
    Called after build wizard. Auto-creates bind mount volume
    for the new container if it has no volumes defined.
    """
    if cfg is None: cfg = load_conf()
    if cfg.get("BUILD_AUTO_VOLUME","0") != "1": return []
    notes = []
    try:
        import re as _re
        data = open(fpath).read()
        idx = data.find(f"container_name: {svc_name}")
        if idx < 0: return []
        block = data[idx:idx+3000]
        nxt = _re.search(r"\n  [a-zA-Z]", block[10:])
        if nxt: block = block[:nxt.start()+10]
        # Skip if already has a real data volume (not just shared lib mounts)
        if "volumes:" in block:
            import re as _re2
            # Only skip if there's a bind mount that looks like a data dir
            # Ignore /usr/lib shared lib mounts - those aren't data volumes
            data_vols = _re2.findall(r'-\s+(/home/\S+|/data/\S+|/var/lib/\S+|/etc/\S+):', block)
            named_vols = _re2.findall(r'-\s+[\w][\w-]+:/\w', block)
            lib_only = all('/usr/lib' in v or '/usr/local/lib' in v 
                          for v in _re2.findall(r'-\s+(\S+):\S+', block))
            if (data_vols or named_vols) and not lib_only:
                return []
        vol_base = cfg.get("FIX_VOLUME_BASE","/srv/stacks/docker")
        vol_path = f"{vol_base}/{svc_name}/config"
        os.makedirs(vol_path, exist_ok=True)
        # Find insertion point after image: line
        lines = open(fpath).readlines()
        cname_line = next((i for i,l in enumerate(lines)
                          if f"container_name: {svc_name}" in l), None)
        if cname_line is None: return []
        insert_at = cname_line
        for j in range(cname_line, min(cname_line+15, len(lines))):
            if _re.match(r"    image:", lines[j]):
                insert_at = j; break
        vol_lines = [
            "    volumes:\n",
            f"      - {vol_path}:/config\n",
        ]
        lines[insert_at+1:insert_at+1] = vol_lines
        open(fpath, "w").writelines(lines)
        notes.append(f"added volume {vol_path}:/config")
    except Exception as e:
        notes.append(f"volume inject error: {e}")
    return notes



def declare_net_external_in_stack(fpath, net_name):
    """Declare net_name as external:true in the top-level networks: section of fpath.
    Used for stack files (not creators) - the net is created by a core provisioner."""
    import os, re
    try:
        lines = open(fpath).read().split("\n")
    except: return False
    # Already declared at top level?
    in_net = False
    for l in lines:
        if re.match(r"^networks:\s*$", l): in_net = True; continue
        if in_net:
            if l and not l.startswith(" "): in_net = False; continue
            if re.match(rf"^  {re.escape(net_name)}\s*:", l): return False
    # Find top-level networks: section
    entry = "  " + net_name + ": {name: " + net_name + ", external: true}"
    out = []
    inserted = False
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if re.match(r"^networks:\s*$", lines[i]) and not inserted:
            out.append(entry); inserted = True
        i += 1
    if not inserted:
        # No networks: section - add before services:
        out = []
        for l in lines:
            if re.match(r"^services:\s*$", l) and not inserted:
                out.append("networks:"); out.append(entry); inserted = True
            out.append(l)
    if inserted:
        open(fpath, "w").write("\n".join(out))
        return True
    return False

def post_build_inject(fpath, svc_name, cfg=None):
    """
    Called after build wizard. Runs Phase 1 of stacks fix:
    - Scans for missing networks/volumes in the compose file
    - Adds full network definition (with subnet) to creator file
    - Adds network/volume to provisioner container in creator file
    - Adds top-level network/volume references to compose file
    This reuses the exact same code as stacks fix Phase 1.
    """
    if cfg is None: cfg = load_conf()
    notes = []
    try:
        import os as _os
        stacks_dir = _os.path.dirname(fpath)
        stack = _os.path.basename(fpath).replace(".yml","")

        # Enable just Phase 1 (network/volume define)
        _cfg = dict(cfg)
        _cfg["FIX_DEFINE_NETVOL"] = "1"
        _cfg["FIX_HEALTHCHECKS"] = "0"
        _cfg["FIX_STRIP_PROFILES"] = "0"
        _cfg["FIX_REMOVE_GAPS"] = "0"
        _cfg["FIX_AUTO_NAME"] = "0"
        _cfg["FIX_AUTO_DEPENDS"] = "0"
        _cfg["FIX_GROUP_SAME_IP"] = "0"

        # Handle creator file selection from wizard
        if cfg.get("creator_stack") == "new":
            _cfg["FIX_FORCE_CREATE_CREATOR"] = "1"
        elif cfg.get("creator_stack"):
            _cfg["FIX_CREATOR_TARGET"] = cfg["creator_stack"]

        # Handle external vs internal
        if not cfg.get("external_network", True):
            _cfg["FIX_INLINE_NETWORKS"] = "1"
            _cfg["FIX_INLINE_VOLUMES"] = "1"

        # Run Phase 1 directly
        # Discover what's missing in this file
        missing_nets, missing_vols = set(), set()
        for svc in _parse_services_phase1(fpath):
            for net in svc.get("networks", []):
                if net not in ("traefik_net","apartment_net"):
                    missing_nets.add(net)
            for vol in svc.get("named_volumes", []):
                missing_vols.add(vol)

        # Find creator and add - filter already defined
        creators = discover_creator_files(stacks_dir)
        already = set()
        for _cp, _cd in creators.items():
            already |= set(_cd.get("nets", set()))
        new_nets = missing_nets - already
        creator = find_or_create_creator(stacks_dir, _cfg)
        if creator and new_nets:
            used = all_used_subnets(creators, _cfg.get("FIX_SUBNET_BASE","10.50"))
            result = add_to_creator(creator, new_nets, missing_vols,
                _cfg.get("FIX_SUBNET_BASE","10.50"), used, False)
            if result:
                notes.append(f"added {new_nets} to {_os.path.basename(creator)}")
        elif not new_nets:
            notes.append("all networks already defined in creator files")

        # Declare each net as external:true in EVERY stack file that references it
        # (main stack + DB stack), so containers can actually attach to it
        import glob as _glob
        _creator_paths = set(creators.keys())
        for _net in missing_nets:
            for _sf in _glob.glob(_os.path.join(stacks_dir, "*.yml")):
                if _sf in _creator_paths:
                    continue
                try:
                    _refs = any(_net in s.get("networks", []) for s in _parse_services_phase1(_sf))
                except:
                    _refs = False
                if _refs:
                    if declare_net_external_in_stack(_sf, _net):
                        notes.append(f"declared {_net} external in {_os.path.basename(_sf)}")

        # Also add bind mount volume if BUILD_AUTO_VOLUME=1
        if _cfg.get("BUILD_AUTO_VOLUME","0") == "1":
            notes += post_build_inject_volume(fpath, svc_name, _cfg)

    except Exception as e:
        notes.append(f"post_build error: {e}")
    return notes


def _parse_services_phase1(fpath):
    """Parse services from a compose file for Phase 1 network/volume detection."""
    import re as _re
    result = []
    try:
        svcs, _ = parse_services_with_positions(fpath)
        data = open(fpath).read()
        for svc in svcs:
            idx = data.find(f"container_name: {svc['name']}")
            if idx < 0: continue
            block = data[idx:idx+3000]
            nets = _re.findall(r"^\s{6}(\w+_net)\s*:", block, _re.MULTILINE)
            # Named volumes: ONLY parse inside the service's volumes: block
            # (prevents matching http:// in healthcheck command strings)
            vols = []
            in_vols = False
            for ln in block.split("\n"):
                if _re.match(r"^    volumes:\s*$", ln):
                    in_vols = True
                    continue
                if in_vols:
                    # End of volumes block: any new 4-space key or dedent
                    if _re.match(r"^    \w", ln) or (ln and not ln.startswith("      ")):
                        break
                    m = _re.match(r'^\s+-\s+"?([^":\s]+):', ln)
                    if m:
                        v = m.group(1)
                        if not v.startswith("/") and "/" not in v:
                            vols.append(v)
            result.append({"name": svc["name"], "networks": nets, "named_volumes": vols})
    except: pass
    return result

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry_run = '--dry-run' in sys.argv[1:]
    cfg = load_conf()
    replace_broken = '--replace-broken' in sys.argv[1:] or on(cfg.get('FIX_REPLACE_BROKEN_HC', '0'))
    sd = cfg["STACKS_DIR"]

    target = args[0] if args else 'all'
    svc    = args[1] if len(args) > 1 else None
    # Build files list early so all phases can use it
    if target in ("all", "--all"):
        files = sorted(os.path.join(sd, f) for f in os.listdir(sd)
                       if f.endswith((".yml", ".yaml")))
    else:
        _fname = target if target.endswith((".yml",".yaml")) else target+".yml"
        _fpath = os.path.join(sd, _fname)
        files = [_fpath] if os.path.isfile(_fpath) else []


    pr(f"\n{M}╔══════════════════════════════════════╗{X}")
    pr(f"{M}║   🔧 STACKS STACK FIXER               ║{X}")
    pr(f"{M}╚══════════════════════════════════════╝{X}")
    if dry_run:
        pr(f"{Y}   DRY RUN — no files will be written{X}")

    if not os.path.isdir(sd):
        pr(f"{R}✘ Stacks dir not found: {sd}{X}"); sys.exit(1)

    _safety_orig = {}; _safety_valid = {}
    if not dry_run:
        import subprocess as _sub
        for _sf in files:
            try:
                _safety_orig[_sf] = open(_sf).read()
                _safety_valid[_sf] = (_sub.run(["docker","compose","-f",_sf,"config"], capture_output=True).returncode == 0)
            except Exception:
                pass

    total = 0

    # ── Phase 0: strip profiles: blocks (prevents services being skipped) ──
    if on(cfg["FIX_STRIP_PROFILES"]):
        pr(f"\n{C}🧹 Stripping profiles: blocks{X}")
        _prof_fixed = 0
        for f in sorted(os.listdir(sd)):
            if not f.endswith(('.yml', '.yaml')):
                continue
            fp = os.path.join(sd, f)
            if dry_run:
                # Peek without writing
                try:
                    content = open(fp).read()
                    if 'profiles:' in content:
                        pr(f"  {Y}[dry-run] would strip profiles from {f}{X}")
                        _prof_fixed += 1
                except:
                    pass
            else:
                if strip_profiles_from_file(fp, False):
                    pr(f"  {G}✔ {f}: profiles: blocks stripped{X}")
                    _prof_fixed += 1
        if _prof_fixed == 0:
            pr(f"  {G}✔ No profiles: blocks found{X}")
        total += _prof_fixed



    # ── Phase 0.1: Auto-name compose files ──────────────────────────────────
    # Adds/fixes "name: stackname" at top of each compose file
    if on(cfg.get("FIX_AUTO_NAME", "1")):
        for f in files:
            stack_name = os.path.basename(f).replace('.yml','').replace('.yaml','')
            lines = open(f).read().split('\n')
            # Check if name: already correct at top
            has_name = False
            name_correct = False
            for i, line in enumerate(lines[:5]):
                if re.match(r'^name:\s*', line):
                    has_name = True
                    if line.strip() == f'name: {stack_name}':
                        name_correct = True
                    break
            if not name_correct:
                # Remove any existing name: line at top
                lines = [l for l in lines if not re.match(r'^name:\s*', l)]
                # Insert after any leading comment block
                insert_at = 0
                for i, line in enumerate(lines):
                    if line.startswith('#'):
                        insert_at = i + 1
                    else:
                        break
                lines.insert(insert_at, f'name: {stack_name}')
                open(f, 'w').write('\n'.join(lines))
                pr(f"  {G}✔ Named {stack_name}{X}")
                total += 1

    # ── Phase 0.5: Corruption repair ────────────────────────────────────────
    # Uses stacks_repair.py — learned from dev_1.yml reference file
    import importlib.util as _ilu
    _rspec = _ilu.spec_from_file_location("stacks_repair", "/usr/local/lib/stacks_repair.py")
    _rmod = _ilu.module_from_spec(_rspec)
    _rspec.loader.exec_module(_rmod)
    for _rf in files:
        _rfixes = _rmod.repair_file(_rf, dry_run=dry_run)
        if _rfixes:
            for _fix in _rfixes:
                pr(f"  {G}✔ {os.path.basename(_rf)}: {_fix}{X}")
            total += len(_rfixes)

    # ── Phase 0.5b: Legacy corruption repair ─────────────────────────────────
    # Fixes known corruption patterns from label injection bugs:
    # 1. Label lines injected into networks: block
    # 2. HC test values leaked into blkio_config
    _repair_changes = 0
    for f in files:
        content = open(f).read()
        original = content
        lines = content.split('\n')
        result = []
        in_networks = False

        for line in lines:
            # Track networks: block
            if re.match(r'^networks:\s*$', line):
                in_networks = True
            elif re.match(r'^[a-zA-Z]', line) and not line.startswith(' '):
                in_networks = False

            # Remove label lines inside networks block
            if in_networks and re.match(r'\s+- "(traefik\.|sablier\.)', line):
                _repair_changes += 1
                continue

            # Fix HC test values leaked into blkio_config
            if 'blkio_config' in line and ('NONE' in line or 'CMD' in line or 'CMD-SHELL' in line):
                line = re.sub(
                    r'device_read_bps:\s*\[.*?\]',
                    'device_read_bps: [{path: /dev/nvme0n1, rate: 500mb}]',
                    line
                )
                _repair_changes += 1

            result.append(line)

        content = '\n'.join(result)
        if content != original:
            open(f, 'w').write(content)
            pr(f"  {G}✔ Repaired corruption in {os.path.basename(f)}{X}")

    if _repair_changes > 0:
        total += _repair_changes
    
    # ── Phase 1: auto-define missing networks/volumes (dynamic creators) ──
    if on(cfg["FIX_DEFINE_NETVOL"]):
        pr(f"\n{C}🌐 Network/Volume auto-define{X}")
        _skip = set(cfg.get("FIX_SKIP_FILES", "").split())
        creators = discover_creator_files(sd, _skip)
        needed_nets, needed_vols = collect_service_refs(sd, creators, _skip)

        defined_nets = real_defined_nets(sd, _skip)
        defined_vols = real_defined_vols(sd, _skip)

        missing_nets = needed_nets - defined_nets
        missing_vols = needed_vols - defined_vols

        if missing_nets or missing_vols:
            # Pick smallest creator; if none exist, smallest file overall bootstraps
            if creators:
                target_path = min(creators, key=lambda p: creators[p]["size"])
            else:
                target_path = smallest_file_overall(sd)
                pr(f"  {Y}No creator files found — bootstrapping into "
                   f"{os.path.basename(target_path)}{X}")
            used = all_used_subnets(creators, cfg["FIX_SUBNET_BASE"])
            total += add_to_creator(target_path, missing_nets, missing_vols,
                                    cfg["FIX_SUBNET_BASE"], used, dry_run)
        else:
            pr(f"  {G}✔ All referenced networks/volumes already defined{X}")

    # ── Phase 2: heal typos in creator files ──
    if on(cfg["FIX_HEAL_TYPOS"]):
        pr(f"\n{C}🩹 Creator-file typo healing{X}")
        for path in discover_creator_files(sd):
            total += heal_creator_typos(path, dry_run)

    # ── Phase 3: healthchecks ──
    if on(cfg["FIX_HEALTHCHECKS"]):
        pr(f"\n{C}❤️  Healthchecks (add-only; existing ones never touched){X}")
        if target in ('all', '--all'):
            files = sorted(os.path.join(sd, f) for f in os.listdir(sd)
                           if f.endswith('.yml') or f.endswith('.yaml'))
        else:
            fname = target if target.endswith(('.yml', '.yaml')) else target + '.yml'
            fpath = os.path.join(sd, fname)
            if not os.path.isfile(fpath):
                pr(f"{R}✘ Stack not found: {target}{X}"); sys.exit(1)
            files = [fpath]
        for f in files:
            stack_name = os.path.basename(f).replace('.yml', '').replace('.yaml', '')
            pr(f"\n{C}🔧 {stack_name}{X}")
            total += fix_healthchecks(f, cfg, svc, dry_run, replace_broken)

    # ── Phase 3.4: Container name normalization ─────────────────────────────
    # Replaces . and _ with - in container_name, hostname, domainname
    if on(cfg.get("FIX_NORMALIZE_NAMES", "0")):
        pr(f"\n{C}🏷️  Normalizing container names (._  ->  -){X}")
        _norm_fixed = 0
        for f in files:
            try:
                data = open(f).read()
                new_data = data
                # Find all container_name values and normalize
                for match in re.finditer(r'(container_name:\s*)(\S+)', data):
                    old_name = match.group(2).strip('"\' ')
                    new_name = old_name.replace('.','-').replace('_','-')
                    if new_name == old_name: continue
                    if dry_run:
                        pr(f"  {Y}[dry] {old_name} -> {new_name}{X}")
                    else:
                        # Replace ALL occurrences of old_name in file
                        new_data = re.sub(
                            r'\b' + re.escape(old_name) + r'\b',
                            new_name, new_data
                        )
                    _norm_fixed += 1
                if not dry_run and new_data != data:
                    open(f, "w").write(new_data)
            except Exception as _e:
                pr(f"  {R}✘ {os.path.basename(f)}: {_e}{X}")
        if _norm_fixed == 0:
            pr(f"  {G}✔ All names already normalized{X}")
        total += _norm_fixed

    # ── Phase 3.5: depends_on injection ──────────────────────────────────────
    if on(cfg.get("FIX_AUTO_DEPENDS", "0")) or on(cfg.get("FIX_REMOVE_DEPENDS", "0")) or on(cfg.get("FIX_FORCE_DEPENDS", "0")):
        if on(cfg.get("FIX_REMOVE_DEPENDS", "0")) and not on(cfg.get("FIX_AUTO_DEPENDS", "0")):
            pr(f"\n{C}🔗 Removing depends_on (retire mode){X}")
        else:
            pr(f"\n{C}🔗 Injecting depends_on for related containers{X}")
        _deps_fixed = 0
        _dep_files = sorted(os.path.join(sd, f) for f in os.listdir(sd)
                     if f.endswith(".yml") or f.endswith(".yaml")) \
                     if target in ("all","--all") else \
                     [os.path.join(sd, target if target.endswith((".yml",".yaml")) else target+".yml")]
        for f in _dep_files:
            if svc and svc not in open(f).read(): continue
            _notes = inject_depends_on(f, cfg)
            for note in _notes:
                if "error" in note.lower():
                    pr(f"  {R}✘ {os.path.basename(f)}: {note}{X}")
                else:
                    pr(f"  {G}✔ {os.path.basename(f)}: {note}{X}")
                    _deps_fixed += 1
        if _deps_fixed == 0:
            pr(f"  {G}✔ No missing depends_on found{X}")
        total += _deps_fixed


    _files = files  # alias for phase 4/5
    # ── Phase 3.6: Group IP alignment ────────────────────────────────────────
    # Move all family members to same IP as the family head
    if on(cfg.get("FIX_GROUP_SAME_IP", "0")):
        pr(f"\n{C}🔗 Aligning family IPs (all members -> same IP as head){X}")
        import glob as _gl2
        _grp_fixed = 0
        _grp_skip = 0
        try:
            from stacks_families import get_families as _get_fams
            from stacks_collision import scan_all_ports as _scan_ports, is_locked_container as _is_locked
            _all_fams = _get_fams()
            _port_map = _scan_ports()

            def _gip(cname):
                for _fp in _gl2.glob(f"{sd}/*.yml"):
                    _d = open(_fp).read()
                    if f"container_name: {cname}" not in _d: continue
                    _i = _d.find(f"container_name: {cname}")
                    _b = _d[_i:_i+3000]
                    _nx = re.search(r"\n  [a-zA-Z]", _b[10:])
                    if _nx: _b = _b[:_nx.start()+10]
                    _pp = re.findall(r"(192\.168\.1\.\d+):(\d+):\d+", _b)
                    if _pp: return _pp[0][0], [p[1] for p in _pp], _fp
                    _m2 = re.search(r"ipv4_address:\s*(192\.168\.1\.\d+)", _b)
                    if _m2: return _m2.group(1), [], _fp
                return None, [], None

            for _head, _members in sorted(_all_fams.items()):
                _hip, _, _ = _gip(_head)
                if not _hip: continue
                for _dep in sorted(_members):
                    if _dep == _head: continue
                    if _is_locked(_dep): continue
                    _dip, _dports, _dfp = _gip(_dep)
                    if not _dip or not _dfp: continue
                    if _dip == _hip: continue
                    _ok = all(
                        f"{_hip}:{p}" not in _port_map or
                        all(o[1]==_dep for o in _port_map[f"{_hip}:{p}"])
                        for p in _dports
                    )
                    if not _ok:
                        pr(f"  {Y}SKIP {_dep}: port conflict on {_hip}{X}")
                        _grp_skip += 1; continue
                    if dry_run:
                        pr(f"  {G}[dry] {_dep}: {_dip} -> {_hip} [{_head}]{X}")
                    else:
                        _dc = open(_dfp).read()
                        _di = _dc.find(f"container_name: {_dep}")
                        _pre = _dc[:_di]
                        _rest = _dc[_di:_di+3000]
                        _nx2 = re.search(r"\n  [a-zA-Z]", _rest[10:])
                        _bl = _nx2.start()+10 if _nx2 else 3000
                        _blk = _rest[:_bl].replace(f"{_dip}:", f"{_hip}:")
                        open(_dfp, "w").write(_pre + _blk + _rest[_bl:])
                        pr(f"  {G}✔ {_dep}: {_dip} -> {_hip} [{_head}]{X}")
                    _grp_fixed += 1
        except Exception as _ge:
            pr(f"  {R}✘ Group IP error: {_ge}{X}")
        if _grp_fixed == 0 and _grp_skip == 0:
            pr(f"  {G}✔ All family IPs already aligned{X}")
        else:
            pr(f"  {G}✔ {_grp_fixed} aligned, {_grp_skip} skipped{X}")
        total += _grp_fixed

    # ── Phase 4a: collapse double-spaced files ──────────────────────────────
    for f in _files:
        try:
            if collapse_blank_lines(f, dry_run):
                if not dry_run:
                    pr(f"  {G}✔ {os.path.basename(f)}: double-spacing collapsed{X}")
        except:
            pass

    # ── Phase 4: remove blank lines inside service blocks ──
    if on(cfg.get("FIX_REMOVE_GAPS", "1")):
        pr(f"\n{C}🧹 Removing gaps in service blocks{X}")
        _gaps_fixed = 0
        for f in _files:
            if dry_run:
                try:
                    content = open(f).read()
                    if '\n\n' in content:
                        pr(f"  {Y}[dry-run] would remove gaps from {os.path.basename(f)}{X}")
                        _gaps_fixed += 1
                except:
                    pass
            else:
                if remove_gaps_from_file(f, False):
                    pr(f"  {G}✔ {os.path.basename(f)}: gaps removed{X}")
                    _gaps_fixed += 1
        if _gaps_fixed == 0:
            pr(f"  {G}✔ No gaps found{X}")
        total += _gaps_fixed


    # ── Phase 5: Volume management ──────────────────────────────────────────
    vol_base = cfg.get("FIX_VOLUME_BASE", "/srv/stacks/docker")
    vol_cpath = cfg.get("FIX_VOLUME_CONTAINER_PATH", "/config")

    if on(cfg.get("FIX_CREATE_VOLUME_DIRS", "1")):
        pr(f"\n{C}📁 Volume directories{X}")
        _dirs_created = 0
        _dirs_checked = 0
        for f in _files:
            try:
                file_lines = open(f).readlines()
                mounts = get_bind_mounts([l.rstrip() for l in file_lines])
                _dirs_checked += len(mounts)
                results = create_volume_dirs(mounts, dry_run)
                for r in results:
                    pr(f"  {G}✔ {r}{X}")
                    _dirs_created += 1
            except:
                pass
        if _dirs_created > 0:
            pr(f"  {G}✔ {_dirs_created} director(ies) created{X}")
        else:
            pr(f"  {G}✔ All {_dirs_checked} bind mount dirs already exist{X}")

    if on(cfg.get("FIX_CONVERT_NAMED_TO_BIND", "0")):
        pr(f"\n{C}🔄 Converting named volumes to bind mounts{X}")
        _conv_total = 0
        for f in _files:
            try:
                file_lines = open(f).readlines()
                new_lines, changes = convert_named_to_bind(
                    [l.rstrip() for l in file_lines], vol_base, dry_run)
                if changes > 0:
                    if dry_run:
                        pr(f"  {Y}[dry-run] {os.path.basename(f)}: {changes} named→bind{X}")
                    else:
                        _backup(f)
                        open(f, 'w').write('\n'.join(new_lines))
                        pr(f"  {G}✔ {os.path.basename(f)}: {changes} named→bind{X}")
                    _conv_total += changes
            except Exception as e:
                pr(f"  {R}✘ {os.path.basename(f)}: {e}{X}")
        if _conv_total == 0:
            pr(f"  {G}✔ No named volumes found{X}")
        total += _conv_total



    # ── Phase 6: Network auto-injection ─────────────────────────────────────
    auto_nets = cfg.get("FIX_AUTO_NETWORKS", "").split()
    auto_net_pri = int(cfg.get("FIX_AUTO_NETWORK_PRIORITY", "1000"))
    do_link = on(cfg.get("FIX_AUTO_LINK_NETWORKS", "0"))
    link_pri = int(cfg.get("FIX_AUTO_LINK_PRIORITY", "500"))
    do_compose_net = on(cfg.get("FIX_AUTO_COMPOSE_NETWORK", "0"))
    compose_net_pri = int(cfg.get("FIX_AUTO_COMPOSE_NETWORK_PRIORITY", "200"))

    if auto_nets or do_link or do_compose_net:
        pr(f"\n{C}🌐 Network auto-injection{X}")
        _net_changes = 0

        # Build global group map first (cross-file awareness)
        global_groups = get_all_groups_global(_files) if do_link else {}

        for f in _files:
            stack_name = os.path.basename(f).replace('.yml','').replace('.yaml','')
            try:
                services, _raw = parse_services_with_positions(f)
                file_lines = [l.rstrip('\n') for l in _raw]
                real_svcs = [s for s in services
                            if not s['name'].startswith('provisioner')
                            and s.get('image','')
                            and not re.match(r'^alpine(:|$)', s.get('image',''))]
                if not real_svcs:
                    continue

                changed = False
                lines = list(file_lines)

                # 1. Add auto_nets to every service
                for net in auto_nets:
                    for svc in reversed(real_svcs):
                        lines, did_change = inject_network_into_service(
                            lines, svc, net, auto_net_pri, dry_run)
                        if did_change:
                            if dry_run:
                                pr(f"  {Y}[dry-run] {svc['name']}: would add {net}{X}")
                            _net_changes += 1
                            changed = True
                    lines, _ = ensure_network_declared(lines, net)

                # 2. Auto-link networks using GLOBAL groups
                if do_link:
                    svc_names = {s['name'] for s in real_svcs}
                    for prefix, grp in global_groups.items():
                        # Check if any member of this group is in this file
                        file_members = grp['members_by_file'].get(f, [])
                        if not file_members:
                            continue
                        link_net = grp['net_name']
                        for svc in reversed(real_svcs):
                            if svc['name'] in file_members:
                                lines, did_change = inject_network_into_service(
                                    lines, svc, link_net, link_pri, dry_run)
                                if did_change:
                                    if dry_run:
                                        pr(f"  {Y}[dry-run] {svc['name']}: would add {link_net}{X}")
                                    _net_changes += 1
                                    changed = True
                        lines, _ = ensure_network_declared(lines, link_net)

                # 3. Compose-wide network
                if do_compose_net:
                    compose_net = f"{stack_name}_net".replace('-', '_')
                    for svc in reversed(real_svcs):
                        lines, did_change = inject_network_into_service(
                            lines, svc, compose_net, compose_net_pri, dry_run)
                        if did_change:
                            _net_changes += 1
                            changed = True
                    lines, _ = ensure_network_declared(lines, compose_net)

                # Write file if changed
                if changed and not dry_run:
                    content = '\n'.join(l.rstrip('\n') for l in lines)
                    # Safety: never write if duplicate top-level networks: exists
                    net_count = len(re.findall(r'^networks:\s*$', content, re.MULTILINE))
                    if net_count > 1:
                        pr(f"  {R}✘ {stack_name}: duplicate networks: detected — skipping{X}")
                        continue
                    _backup(f)
                    open(f, 'w').write(content)
                    pr(f"  {G}✔ {stack_name}: networks injected{X}")

            except Exception as e:
                pr(f"  {R}✘ {stack_name}: {e}{X}")

        if _net_changes == 0:
            pr(f"  {G}✔ All networks already present{X}")
        else:
            pr(f"  {G}✔ {_net_changes} network injection(s){X}")
        total += _net_changes

    # ── Phase 7: Global key injection ───────────────────────────────────────
    gi = load_global_inject_conf()
    _anchor_keys = ['INJECT_STOP_GRACE','INJECT_LOGGING','INJECT_RESTART']
    _svc_keys = ['INJECT_DEPLOY','INJECT_BLKIO','INJECT_ULIMITS','INJECT_COMMON_CAPS','INJECT_HOSTNAME','INJECT_STORAGE_OPT','INJECT_MAC','INJECT_LABELS','INJECT_RESTART']
    _all_keys = _anchor_keys + _svc_keys
    if any(_gi_enabled(gi.get(k,'0')) for k in _all_keys):
        pr(f"\n{C}⚙️  Global key injection{X}")
        _gi_changes = 0
        for f in _files:
            stack_name = os.path.basename(f).replace('.yml','').replace('.yaml','')
            try:
                file_lines = [l.rstrip('\n') for l in open(f).readlines()]
                lines = list(file_lines)
                changed = False

                # Anchor-targeted keys: inject into x-common-caps block
                if any(_gi_enabled(gi.get(k,'0')) for k in _anchor_keys):
                    lines, anch_changes = inject_into_anchor(lines, gi, dry_run)
                    if anch_changes > 0:
                        _gi_changes += anch_changes
                        changed = True
                        if dry_run:
                            pr(f"  {Y}[dry-run] {stack_name} anchor: {anch_changes} key(s) would be updated{X}")

                # Service-targeted keys: inject into each real service
                if any(_gi_enabled(gi.get(k,'0')) for k in _svc_keys):
                    services, _raw = parse_services_with_positions(f)
                    real_svcs = [s for s in services
                                if not s['name'].startswith('provisioner')
                                and s.get('image','')
                                and not re.match(r'^alpine(:|$)', s.get('image',''))]
                    _m = re.match(r'^([a-zA-Z]+)', stack_name)
                    _sp = _m.group(1) if _m else stack_name
                    for svc in reversed(real_svcs):  # bottom-up keeps positions valid
                        lines, svc_changes = inject_global_keys(lines, svc, gi, dry_run, _sp)
                        if svc_changes > 0:
                            _gi_changes += svc_changes
                            changed = True
                            if dry_run:
                                pr(f"  {Y}[dry-run] " + svc['name'] + f": {svc_changes} key(s) would be injected{X}")

                if changed and not dry_run:
                    _backup(f)
                    open(f, 'w').write('\n'.join(lines))
                    pr(f"  {G}✔ {stack_name}: keys injected{X}")
            except Exception as e:
                pr(f"  {R}✘ {stack_name}: {e}{X}")
        if _gi_changes == 0:
            pr(f"  {G}✔ All global keys already present{X}")
        total += _gi_changes

    # ── Phase 8: CPU core pinning ────────────────────────────────────────────
    gi = load_global_inject_conf()
    if _gi_enabled(gi.get('INJECT_CPUSET','0')):
        pr(f"\n{C}🖥️  CPU core pinning{X}")
        _cpu_changes = 0
        for f in _files:
            stack_name = os.path.basename(f).replace('.yml','').replace('.yaml','')
            # Get stack prefix
            m = re.match(r'^([a-zA-Z]+)', stack_name)
            stack_prefix = m.group(1) if m else stack_name
            try:
                services, _raw = parse_services_with_positions(f)
                file_lines = [l.rstrip('\n') for l in _raw]
                real_svcs = [s for s in services
                            if not s['name'].startswith('provisioner')
                            and s.get('image','')
                            and not re.match(r'^alpine(:|$)', s.get('image',''))]
                if not real_svcs:
                    continue
                changed = False
                lines = list(file_lines)
                for svc in reversed(real_svcs):  # bottom-up keeps positions valid
                    lines, changes = inject_cpuset(lines, svc, gi, stack_prefix, dry_run)
                    if changes > 0:
                        _cpu_changes += changes
                        changed = True
                        if dry_run:
                            pr(f"  {Y}[dry-run] " + svc['name'] + f": cpuset would be set{X}")
                if changed and not dry_run:
                    _backup(f)
                    open(f, 'w').write('\n'.join(lines))
                    pr(f"  {G}✔ {stack_name}: CPU pinning applied{X}")
            except Exception as e:
                pr(f"  {R}✘ {stack_name}: {e}{X}")
        if _cpu_changes == 0:
            pr(f"  {G}✔ All CPU assignments already present{X}")
        total += _cpu_changes


    if not dry_run:
        import subprocess as _sub2
        for _sf in files:
            if not _safety_valid.get(_sf):
                continue
            try:
                _chk = _sub2.run(["docker","compose","-f",_sf,"config"], capture_output=True, text=True)
                if _chk.returncode != 0:
                    open(_sf,'w').write(_safety_orig[_sf])
                    _err = (_chk.stderr.strip().splitlines() or ["unknown"])[-1]
                    pr(f"{R}✘ SAFETY: {_sf.split('/')[-1]} broke validation after fix — REVERTED.{X}")
                    pr(f"{R}   reason: {_err}{X}")
            except Exception:
                pass
    pr(f"\n{G}✨ Done — {total} change(s){'(dry-run, none written)' if dry_run else ''}{X}\n")

if __name__ == '__main__':
    main()
