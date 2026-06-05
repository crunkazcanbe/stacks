#!/usr/bin/env python3
"""
stacks_gen_dynamic.py — Auto-generate Traefik dynamic config files
Scans compose stacks and generates routers, services, middlewares, TCP routes
Config-driven: reads from stacks.conf for domains, URLs, feature flags
"""
import re, os, sys, yaml

# ── Config defaults ───────────────────────────────────────────────────────────
DEFAULTS = {
    'PRIMARY_DOMAIN':    'example.com',
    'SECONDARY_DOMAIN':  'example.net',
    'AUTHENTIK_URL':     'http://authentik_server:9000',
    'CROWDSEC_URL':      'http://crowdsec_bouncer:8080',
    'SABLIER_URL':       'http://sablier:10000',
    'SABLIER_THEME':     'ghost',
    'SABLIER_DURATION':  '1h',
    'GEN_ROUTERS':       '1',
    'GEN_SERVICES':      '1',
    'GEN_MIDDLEWARES':   '1',
    'GEN_SABLIER':       '1',
    'GEN_TCP':           '1',
    'GEN_AUTH':          '1',   # include authentik middleware (Authentik = the main gate)
    'GEN_CROWDSEC':      '1',   # include crowdsec middleware
    'GEN_DOMAIN':        'primary',  # primary|secondary|both
    # ── Security hardening (config-toggleable; safe defaults) ──────────────────
    'GEN_PERMISSIONS_POLICY': '1',   # add a Permissions-Policy response header (safe, pure addition)
    'PERMISSIONS_POLICY': 'camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()',
    'GEN_CSP':                '0',   # add Content-Security-Policy (OFF by default — CSP breaks many apps)
    'CSP_POLICY': "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; connect-src 'self' https: wss:; font-src 'self' data:; frame-ancestors 'self'",
    'GEN_CF_IPALLOW':         '0',   # restrict origin to Cloudflare + LAN IPs (OFF by default — can lock out access)
    'CF_TRUSTED_IPS':         '',    # extra comma-separated CIDRs to always allow (e.g. your VPN subnet)
}

# Cloudflare published edge ranges (IPv4 + IPv6) + private LAN, used by the
# optional cloudflare-ipallow middleware. Update from https://www.cloudflare.com/ips/
CLOUDFLARE_IPS = [
    '173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22',
    '141.101.64.0/18', '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20',
    '197.234.240.0/22', '198.41.128.0/17', '162.158.0.0/15', '104.16.0.0/13',
    '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22',
    '2400:cb00::/32', '2606:4700::/32', '2803:f800::/32', '2405:b500::/32',
    '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32',
]
PRIVATE_LAN_IPS = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.1/32']

# TCP database port map
TCP_PORTS = {
    'postgres': 5432, 'postgresql': 5432,
    'mysql': 3306, 'mariadb': 3306,
    'mongo': 27017, 'mongodb': 27017,
    'redis': 6379,
    'mssql': 1433,
    'neo4j': 7687,
}

STANDARD_MIDDLEWARES = [
    'https-header', 'crowdsec_bouncer', 'authentik-auth',
    'global-retry', 'compress', 'inflight', 'buffering', 'rate-limit'
]

def load_conf(conf_path):
    cfg = dict(DEFAULTS)
    if os.path.exists(conf_path):
        for line in open(conf_path):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    try:
        import sys as _s; _s.path.insert(0, '/usr/local/lib'); import stacks_config as _sc
        cfg.update(_sc.load())   # YAML master overlay (stacks.yaml wins)
    except Exception: pass
    return cfg

def get_service_port(svc_def):
    """Extract port from traefik label or common defaults."""
    labels = svc_def.get('labels', [])
    if isinstance(labels, dict):
        labels = [f"{k}={v}" for k, v in labels.items()]
    for l in labels:
        m = re.search(r'loadbalancer\.server\.port=(\d+)', str(l))
        if m: return int(m.group(1))
    # Common port defaults by image
    image = svc_def.get('image', '').lower()
    port_map = {
        'nginx': 80, 'apache': 80, 'caddy': 80,
        'grafana': 3000, 'prometheus': 9090,
        'gitea': 3000, 'nextcloud': 80,
        'vaultwarden': 80, 'portainer': 9000,
    }
    for k, p in port_map.items():
        if k in image: return p
    return 80

def is_tcp_service(name, svc_def):
    """Check if service is a TCP database."""
    name_lower = name.lower()
    for db in TCP_PORTS:
        if db in name_lower: return True
    image = svc_def.get('image', '').lower()
    for db in TCP_PORTS:
        if db in image: return True
    return False

def get_tcp_port(name, svc_def):
    name_lower = name.lower()
    for db, port in TCP_PORTS.items():
        if db in name_lower: return port
    image = svc_def.get('image', '').lower()
    for db, port in TCP_PORTS.items():
        if db in image: return port
    return None

def service_has_traefik(svc_def):
    labels = svc_def.get('labels', [])
    if isinstance(labels, dict):
        return labels.get('traefik.enable', 'false').lower() == 'true'
    for l in labels:
        if 'traefik.enable=true' in str(l).lower(): return True
    return False

def service_sablier_enabled(svc_def):
    """False if the service is explicitly sablier.enable=false (always-on, never sleeps).
    Always-on services (Authentik, CrowdSec, Traefik, ...) must NOT get a Sablier
    middleware, or every request to them shows the wake 'ghost' loading screen forever."""
    labels = svc_def.get('labels', [])
    if isinstance(labels, dict):
        return str(labels.get('sablier.enable', 'true')).lower() != 'false'
    for l in labels:
        if 'sablier.enable=false' in str(l).lower(): return False
    return True

def get_subdomain(name, svc_def):
    """Get subdomain from traefik label or derive from name."""
    labels = svc_def.get('labels', [])
    if isinstance(labels, dict):
        labels = [f"{k}={v}" for k, v in labels.items()]
    for l in labels:
        m = re.search(r'rule=Host\(`([^.]+)\.', str(l))
        if m: return m.group(1)
    # Derive from container name
    cname = svc_def.get('container_name', name)
    return cname.replace('_', '-').lower()

def gen_router(name, subdomain, domain, svc_name, sablier_mw, cfg):
    mws = []
    if cfg.get('GEN_CF_IPALLOW') == '1': mws.append('cloudflare-ipallow')
    if sablier_mw: mws.append(sablier_mw)
    mws.append('https-header')
    if cfg.get('GEN_CROWDSEC') == '1': mws.append('crowdsec_bouncer')
    if cfg.get('GEN_AUTH') == '1': mws.append('authentik-auth')
    mws += ['global-retry', 'compress', 'inflight', 'buffering', 'rate-limit']
    mw_str = ', '.join(mws)
    return (
        f"    {name}-router:\n"
        f'      rule: "Host(`{subdomain}.{domain}`)"\n'
        f"      service: {svc_name}\n"
        f"      entryPoints: [web]\n"
        f"      middlewares: [{mw_str}]\n"
    )

def gen_service(name, container, port):
    return (
        f"    {name}-svc:\n"
        f"      loadBalancer:\n"
        f'        servers: [{{ url: "http://{container}:{port}" }}]\n'
    )

def gen_sablier_mw(name, container, cfg):
    return (
        f"    sablier-{name}:\n"
        f"      plugin:\n"
        f"        sablier:\n"
        f'          sablierUrl: "{cfg["SABLIER_URL"]}"\n'
        f'          sessionDuration: "{cfg["SABLIER_DURATION"]}"\n'
        f'          names: "{container}"\n'
        f"          dynamic:\n"
        f'            displayName: "{container}"\n'
        f'            provider: "docker"\n'
        f'            stopTimeout: "30s"\n'
        f'            refreshFrequency: "5s"\n'
        f'            theme: "{cfg["SABLIER_THEME"]}"\n'
        f'            timeout: "10m"\n'
        f'            warmupPeriod: "10s"\n'
        f'            healthCheckPath: "/"\n'
        f'            healthCheckInterval: "2s"\n'
        f"            scaling:\n"
        f"              replicas: 1\n"
        f"              minReplicas: 0\n"
        f"              maxReplicas: 1\n"
    )

def gen_tcp_router(name, subdomain, domain, port):
    return (
        f"    {name}-tcp:\n"
        f'      rule: "HostSNI(`{subdomain}.{domain}`)"\n'
        f"      entryPoints: [websecure]\n"
        f"      service: {name}-tcp-svc\n"
        f"      tls:\n"
        f"        passthrough: true\n"
    )

def gen_tcp_service(name, container, port):
    return (
        f"    {name}-tcp-svc:\n"
        f"      loadBalancer:\n"
        f"        servers:\n"
        f'          - address: "{container}:{port}"\n'
    )

def gen_cloudflare_ipallow(cfg):
    """Optional: only accept traffic from Cloudflare edge + LAN (+ user-trusted CIDRs).
    Uses X-Forwarded-For depth=1 so the *real* client IP (from cloudflared) is checked,
    not the docker-network hop. Off unless GEN_CF_IPALLOW=1."""
    extra = [c.strip() for c in cfg.get('CF_TRUSTED_IPS', '').split(',') if c.strip()]
    ranges = CLOUDFLARE_IPS + PRIVATE_LAN_IPS + extra
    lines = '\n'.join(f'          - "{r}"' for r in ranges)
    return (
        "    cloudflare-ipallow:\n"
        "      ipAllowList:\n"
        "        sourceRange:\n"
        f"{lines}\n"
        "        ipStrategy:\n"
        "          depth: 1\n"
    )

def gen_standard_middlewares(cfg):
    auth_url = cfg['AUTHENTIK_URL']
    crowdsec_url = cfg['CROWDSEC_URL']
    # Optional extra security response headers, indented to sit under customResponseHeaders
    _extra_hdrs = ''
    if cfg.get('GEN_PERMISSIONS_POLICY') == '1':
        _extra_hdrs += f'          Permissions-Policy: "{cfg["PERMISSIONS_POLICY"]}"\n'
    if cfg.get('GEN_CSP') == '1':
        _extra_hdrs += f'          Content-Security-Policy: "{cfg["CSP_POLICY"]}"\n'
    out = f"""
    https-header:
      headers:
        customRequestHeaders:
          X-Forwarded-Proto: "https"
        customResponseHeaders:
          X-Frame-Options: "SAMEORIGIN"
          X-Content-Type-Options: "nosniff"
          X-XSS-Protection: "1; mode=block"
          Referrer-Policy: "strict-origin-when-cross-origin"
          Strict-Transport-Security: "max-age=31536000; includeSubDomains; preload"
          Server: ""
          X-Robots-Tag: "noindex, nofollow"
{_extra_hdrs}"""
    if cfg.get('GEN_CF_IPALLOW') == '1':
        out += "\n" + gen_cloudflare_ipallow(cfg) + "\n"
    out += f"""

    global-retry:
      retry:
        attempts: 3
        initialInterval: 100ms

    compress:
      compress:
        minResponseBodyBytes: 1024
        encodings: [zstd, br, gzip]

    inflight:
      inFlightReq:
        amount: 100
        sourceCriterion:
          ipStrategy: {{ depth: 1 }}

    buffering:
      buffering:
        maxRequestBodyBytes: 10485760
        memRequestBodyBytes: 2097152
        maxResponseBodyBytes: 10485760
        memResponseBodyBytes: 2097152
        retryExpression: "IsNetworkError() && Attempts() < 3"

    rate-limit:
      rateLimit:
        average: 100
        burst: 50
        period: 1s
        sourceCriterion:
          ipStrategy: {{ depth: 1 }}

    authentik-auth:
      forwardAuth:
        address: "{auth_url}/outpost.goauthentik.io/auth/traefik"
        trustForwardHeader: true
        authResponseHeaders:
          - X-authentik-username
          - X-authentik-groups
          - X-authentik-email
          - X-authentik-name
          - X-authentik-uid
          - X-authentik-jwt

    crowdsec_bouncer:
      forwardAuth:
        address: "{crowdsec_url}/api/v1/forwardAuth"
        trustForwardHeader: true
"""
    return out

def generate_dynamic(stack_path, out_path, cfg):
    """Generate a dynamic config from a compose file."""
    try:
        content = open(stack_path).read()
        # Parse with YAML anchors resolved NATIVELY (they're defined in-file).
        # The dynamics only need container_name / ports / labels, so merged anchor
        # keys (caps, tcmalloc, ...) are harmless. Hand-stripping anchors was
        # fragile — it broke on hyphenated names and inline {<<: *x} flow maps.
        data = yaml.safe_load(content)
    except Exception as e:
        print(f"  Parse error {os.path.basename(stack_path)}: {e}")
        return False

    services = data.get('services', {})
    if not services:
        return False

    domain = cfg['PRIMARY_DOMAIN']

    routers_out = ''
    services_out = ''
    middlewares_out = ''
    tcp_routers_out = ''
    tcp_services_out = ''

    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict): continue
        container = svc_def.get('container_name', svc_name)

        if is_tcp_service(svc_name, svc_def) and cfg.get('GEN_TCP') == '1':
            port = get_tcp_port(svc_name, svc_def)
            subdomain = container.replace('_', '-').lower()
            if port:
                tcp_routers_out += gen_tcp_router(svc_name, subdomain, domain, port)
                tcp_services_out += gen_tcp_service(svc_name, container, port)
            continue

        if not service_has_traefik(svc_def): continue

        port = get_service_port(svc_def)
        subdomain = get_subdomain(svc_name, svc_def)
        sablier_mw = f'sablier-{svc_name}' if (cfg.get('GEN_SABLIER') == '1' and service_sablier_enabled(svc_def)) else ''

        if cfg.get('GEN_ROUTERS') == '1':
            routers_out += gen_router(svc_name, subdomain, domain,
                                      f'{svc_name}-svc', sablier_mw, cfg)
        if cfg.get('GEN_SERVICES') == '1':
            services_out += gen_service(svc_name, container, port)
        if cfg.get('GEN_SABLIER') == '1' and service_sablier_enabled(svc_def):
            middlewares_out += gen_sablier_mw(svc_name, container, cfg)

    if not routers_out and not services_out:
        return False

    out = "http:\n"
    out += "  serversTransports:\n"
    out += "    insecureTransport:\n"
    out += "      insecureSkipVerify: true\n\n"

    if routers_out:
        out += "  routers:\n\n" + routers_out + "\n"
    if services_out:
        out += "  services:\n\n" + services_out + "\n"
    if middlewares_out or cfg.get('GEN_MIDDLEWARES') == '1':
        out += "  middlewares:\n"
        if cfg.get('GEN_MIDDLEWARES') == '1':
            out += gen_standard_middlewares(cfg)
        if middlewares_out:
            out += middlewares_out

    if tcp_routers_out:
        out += "\ntcp:\n  routers:\n\n" + tcp_routers_out
        out += "\n  services:\n\n" + tcp_services_out

    open(out_path, 'w').write(out)
    return True


def main():
    conf_path = os.path.expanduser('~/.config/stacks/stacks.conf')
    cfg = load_conf(conf_path)
    stacks_dir = cfg.get('STACKS_DIR_OVERRIDE') or '/srv/stacks/Stacks'
    dyn_dir = cfg.get('DYNAMICS_DIR_OVERRIDE') or '/srv/stacks/Configs/Dynamics'

    target = sys.argv[1] if len(sys.argv) > 1 else 'all'

    # Stacks that run on a REMOTE host (VPS) — their compose lives here for
    # editing but they must NOT get a local Traefik dynamic, or their routers
    # collide with the local stacks' routers for the same hostnames (duplicate
    # traefik.love/pangolin.love/etc. → non-deterministic routing + ghost
    # middlewares). Skip any '*-ext' stack during 'all'.
    EXCLUDE_SUFFIXES = ('-ext.yml',)
    if target == 'all':
        files = sorted(f for f in os.listdir(stacks_dir)
                       if f.endswith('.yml') and not f.endswith(EXCLUDE_SUFFIXES))
    else:
        files = [target if target.endswith('.yml') else target + '.yml']

    generated = 0
    for fname in files:
        stack_path = os.path.join(stacks_dir, fname)
        if not os.path.exists(stack_path): continue
        out_name = fname  # same name in dynamics dir
        out_path = os.path.join(dyn_dir, out_name)
        # Don't overwrite existing unless --force
        if os.path.exists(out_path) and '--force' not in sys.argv:
            print(f"  skip (exists): {fname}")
            continue
        if generate_dynamic(stack_path, out_path, cfg):
            print(f"  generated: {fname}")
            generated += 1
        else:
            print(f"  skip (no traefik services): {fname}")

    print(f"\nGenerated {generated} dynamic config(s)")

if __name__ == '__main__':
    main()
