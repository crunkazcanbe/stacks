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
        "FIX_HEALTHCHECKS": "1",
        "FIX_DEFINE_NETVOL": "1",
        "FIX_HEAL_TYPOS": "1",
        "FIX_DEEP_INSPECT": "1",
        "FIX_SUBNET_BASE": "10.50",
        "FIX_BACKUP": "1",
        "FIX_REMOVE_GAPS": "1",  # set to 0 to disable blank line removal in service blocks
        "FIX_HC_IGNORE_STACKS": "",  # space-separated stack files to skip healthcheck changes
        "FIX_REPLACE_BROKEN_HC": "0",  # set to 1 to replace actively-failing healthchecks
        "FIX_STRIP_PROFILES": "1",  # set to 0 to disable auto-stripping of profiles: blocks
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
                v = v.strip().strip('"').strip("'")
                if k in cfg:
                    cfg[k] = v
        except Exception as e:
            pr(f"{Y}⚠ Could not fully read config ({e}); using defaults.{X}")
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
     ['CMD-SHELL', "mongosh --quiet --eval \"db.adminCommand('ping').ok\" || exit 1"],
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
    best = None; best_size = float('inf')
    for f in sorted(os.listdir(stacks_dir)):
        if not (f.endswith('.yml') or f.endswith('.yaml')):
            continue
        path = os.path.join(stacks_dir, f)
        sz = os.path.getsize(path)
        if sz < best_size:
            best_size = sz; best = path
    return best

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

def vol_definition(name):
    return f"  {name}: {{name: {name}, external: true}}\n"

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

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry_run = '--dry-run' in sys.argv[1:]
    cfg = load_conf()
    replace_broken = '--replace-broken' in sys.argv[1:] or on(cfg.get('FIX_REPLACE_BROKEN_HC', '0'))
    sd = cfg["STACKS_DIR"]

    target = args[0] if args else 'all'
    svc    = args[1] if len(args) > 1 else None

    pr(f"\n{M}╔══════════════════════════════════════╗{X}")
    pr(f"{M}║   🔧 STACKS STACK FIXER               ║{X}")
    pr(f"{M}╚══════════════════════════════════════╝{X}")
    if dry_run:
        pr(f"{Y}   DRY RUN — no files will be written{X}")

    if not os.path.isdir(sd):
        pr(f"{R}✘ Stacks dir not found: {sd}{X}"); sys.exit(1)

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

    pr(f"\n{G}✨ Done — {total} change(s){'(dry-run, none written)' if dry_run else ''}{X}\n")

if __name__ == '__main__':
    main()
