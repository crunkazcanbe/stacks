#!/usr/bin/env python3
"""
stacks_build.py — Interactive service scaffolder for StacksServer
Clean self-contained UI with loading bar and inline questions
"""
import sys, os, re, json, subprocess, shutil, random, time, signal
signal.signal(signal.SIGWINCH, signal.SIG_IGN)

STACKS_DIR = "/srv/stacks/Stacks"
CONF_DIR   = "~/.config/stacks"
BUILD_CONF = os.path.join(CONF_DIR, "build.conf")

# ── UI — exactly 9 lines, never changes ───────────────────────────────────────
_UI_H      = 9   # MUST match lines printed in _draw()
_st_target = "build"
_st_svc    = "service"
_st_action = "Initializing..."
_st_pct    = 0
_drawn     = False

def _draw():
    global _drawn
    try:
        cols = min(os.get_terminal_size(sys.stdout.fileno()).columns, 120)
    except:
        try:
            cols = min(os.get_terminal_size(sys.stderr.fileno()).columns, 120)
        except:
            cols = 80
    bw     = min(38, cols - 18)
    filled = (_st_pct * bw) // 100
    bar    = "=" * filled + "-" * (bw - filled)

    if _drawn:
        sys.stdout.write(f"\033[{_UI_H}A")

    sys.stdout.write(
        f"\r\033[K\033[38;5;81m     _             _        \033[0m\n"
        f"\r\033[K\033[38;5;81m ___| |_ __ _  ___| | _____ \033[0m\n"
        f"\r\033[K\033[38;5;81m/ __| __/ _` |/ __| |/ / __|\033[0m\n"
        f"\r\033[K\033[38;5;81m\\__ \\ || (_| | (__|   <\\__ \\\033[0m\n"
        f"\r\033[K\033[38;5;218m|___/\\__\\__,_|\\___|_|\\_\\___/ \033[0m\n"
        f"\r\033[K\n"
        f"\r\033[K\033[1;33m  📦 {_st_target[:30]} \033[90m|\033[0m \033[1;36m{_st_svc[:35]}\033[0m\n"
        f"\r\033[K\033[1;34m  ▶ {_st_action[:cols-8]}\033[0m\n"
        f"\r\033[K\033[1;32m  [{bar}] {_st_pct}%\033[0m\n"
    )
    sys.stdout.flush()
    _drawn = True

def init_ui(target, svc):
    global _st_target, _st_svc, _drawn
    _st_target = target; _st_svc = svc; _drawn = False
    sys.stdout.write("\n" * _UI_H)
    sys.stdout.flush()
    _draw()

def update(action, pct):
    global _st_action, _st_pct
    _st_action = action; _st_pct = pct
    _draw()

def clear_ui():
    if _drawn:
        sys.stdout.write(f"\033[{_UI_H}A\033[J")
        sys.stdout.flush()

# ── Ask — prints on line BELOW the bar, then erases ──────────────────────────
def ask(prompt, default=""):
    global _st_action
    _st_action = f"❓ {prompt}"
    update(_st_action, _st_pct)
    subprocess.run(["stty","echo"], stderr=subprocess.DEVNULL)
    sys.stdout.write(f"  \033[1;36m{prompt}\033[0m [\033[1;33m{default}\033[0m]: ")
    sys.stdout.flush()
    try:
        val = sys.stdin.readline().strip()
        try:
            sys.stdout.write("\033[1A\r\033[K")
            sys.stdout.flush()
        except: pass
        return val if val else default
    except (EOFError, KeyboardInterrupt, BrokenPipeError):
        try:
            clear_ui()
        except: pass
        sys.exit(0)

# ── fzf ───────────────────────────────────────────────────────────────────────
def fzf(items, header="Select", prompt="▶ "):
    global _drawn
    if not items: return None
    if not shutil.which("fzf"):
        for i,it in enumerate(items): print(f"  {i+1}. {it}")
        v = ask("Number","1")
        try: return items[int(v)-1]
        except: return items[0]
    inp = "\n".join(str(x) for x in items)
    import tempfile, shlex
    with tempfile.NamedTemporaryFile(mode='w',suffix='.txt',delete=False) as tf:
        tf.write(inp); tfp = tf.name
    cmd = (f"cat {shlex.quote(tfp)} | fzf --ansi --no-sort --layout=reverse "
           f"--height=~50% --border=rounded --margin=1,3 "
           f"--header={shlex.quote(header)} --prompt={shlex.quote(prompt)} "
           f"--color=bg:#0a1628,bg+:#1a3a5c,fg:#c8d8e8,fg+:#ffffff,"
           f"hl:#4fc3f7,border:#2a6496,header:#4fc3f7,prompt:#81d4fa")
    try:
        with open('/dev/tty','w') as tty:
            r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                             stderr=tty, text=True)
        os.unlink(tfp)
        if r.returncode != 0:
            # Restore terminal after fzf exit
            subprocess.run(["tput","reset"], stderr=subprocess.DEVNULL)
            global _drawn
            _drawn = False
            sys.stdout.write("\n" * _UI_H)
            sys.stdout.flush()
            return None
        out = r.stdout.strip()
        result = out.splitlines()[0] if out else None
        # Restore terminal cleanly after fzf
        subprocess.run(["tput","reset"], stderr=subprocess.DEVNULL)
        _drawn = False
        sys.stdout.write("\n" * _UI_H)
        sys.stdout.flush()
        return result
    except: return None

# ── Config ────────────────────────────────────────────────────────────────────
def load_conf():
    d = {
        "use_common_caps": True, "extra_networks": [],
        "cpuset": "0-15", "cpu_shares": 4096,
        "stop_grace_period": "120s", "stop_signal": "SIGTERM",
        "restart": "no", "user": "0:0",
        "blkio": True, "ulimits": True, "deploy_limits": True, "logging": True,
        "dns": ["192.168.1.114","8.8.8.8"],
        "sablier_group": "", "extra_env": ["TZ=America/New_York"],
        "extra_labels": [], "extra_volumes": [
            "/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4:"
            "/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4:ro"],
    }
    try:
        import sys as _s; _s.path.insert(0, '/usr/local/lib'); import stacks_config as _sc
        d.update({k: v for k, v in _sc.load_doc('build').items() if not str(k).startswith('_')})
    except Exception:
        if os.path.exists(BUILD_CONF):
            try: d.update(json.load(open(BUILD_CONF)))
            except: pass
    return d

# ── Registry search — uses stacks regsearch TUI ───────────────────────────────
def hub_search(term):
    update(f"Searching registries for: {term}", 15)
    # Clear UI so regsearch has full screen
    clear_ui()
    try:
        r = subprocess.run(
            ["python3", "/usr/local/lib/stacks_search.py", term, "--select"],
            text=True, capture_output=False
        )
        # regsearch writes selected image to /tmp/stacks_build_selected
        sel_path = "/tmp/stacks_build_selected"
        if os.path.exists(sel_path):
            image = open(sel_path).read().strip()
            os.unlink(sel_path)
            if image:
                # Reset UI state so it redraws from scratch after TUI
                global _drawn
                _drawn = False
                return image
        return None
    except Exception as e:
        return None

# ── Detect db needs ───────────────────────────────────────────────────────────
def detect_db(image):
    update("Inspecting image for requirements...", 30)
    reqs = {"postgres":False,"mysql":False,"redis":False,"mongo":False}
    try:
        r = subprocess.run(["docker","inspect",image],
                          capture_output=True,text=True,timeout=10)
        if r.returncode != 0:
            subprocess.run(["docker","pull",image],capture_output=True,timeout=60)
            r = subprocess.run(["docker","inspect",image],
                               capture_output=True,text=True,timeout=10)
        data = json.loads(r.stdout)
        if not data: return reqs
        cfg  = data[0].get("Config",{})
        text = " ".join(cfg.get("Env",[]) or [])
        text+= " ".join((cfg.get("Labels",{}) or {}).values())
        if re.search(r'POSTGRES|DATABASE_URL.*post|PGHOST',text,re.I): reqs["postgres"]=True
        if re.search(r'MYSQL|MARIADB',text,re.I):                       reqs["mysql"]=True
        if re.search(r'REDIS|REDIS_HOST',text,re.I):                    reqs["redis"]=True
        if re.search(r'MONGO',text,re.I):                               reqs["mongo"]=True
    except: pass
    return reqs

# ── Find existing db containers ───────────────────────────────────────────────
def find_existing(db_type):
    found = []
    pats  = {"postgres":r'postgres',"mysql":r'mysql|mariadb',
             "redis":r'redis',"mongo":r'mongo'}
    pat = pats.get(db_type,"")
    for f in sorted(os.listdir(STACKS_DIR)):
        if not f.endswith('.yml'): continue
        content = open(os.path.join(STACKS_DIR,f)).read()
        in_svc = False; cur=None; img=""; ip=""; port=""
        for line in content.splitlines():
            if re.match(r'^services:',line): in_svc=True; continue
            if re.match(r'^[a-zA-Z]',line) and not line.startswith(' '): in_svc=False
            if not in_svc: continue
            m = re.match(r'^  ([a-zA-Z0-9_.\-]+):\s*$',line)
            if m:
                if cur and re.search(pat,img,re.I):
                    found.append({"name":cur,"image":img,"ip":ip,"port":port,"stack":f})
                cur=m.group(1); img=""; ip=""; port=""
                continue
            if cur:
                mi=re.match(r'\s+image:\s+(.+)',line)
                if mi: img=mi.group(1).strip()
                mp=re.search(r'(\d+\.\d+\.\d+\.\d+):(\d+):\d+',line)
                if mp: ip=mp.group(1); port=mp.group(2)
        if cur and re.search(pat,img,re.I):
            found.append({"name":cur,"image":img,"ip":ip,"port":port,"stack":f})
    return found

def next_ip():
    used = set()
    for f in os.listdir(STACKS_DIR):
        if not f.endswith('.yml'): continue
        try:
            for m in re.finditer(r'192\.168\.1\.(\d+)',
                                 open(os.path.join(STACKS_DIR,f)).read()):
                used.add(int(m.group(1)))
        except: pass
    for i in range(150,254):
        if i not in used: return f"192.168.1.{i}"
    return "192.168.1.200"

def rand_mac():
    return "02:42:ac:{:02x}:{:02x}:{:02x}".format(
        random.randint(0,255),random.randint(0,255),random.randint(0,255))

# ── DB setup ──────────────────────────────────────────────────────────────────
def setup_db(db_type, svc_name):
    update(f"Setting up {db_type}...", 55)
    existing   = find_existing(db_type)
    db_stacks  = sorted([f for f in os.listdir(STACKS_DIR)
                         if re.match(r'db_\d+\.yml',f)])
    choices = []
    for e in existing:
        ip_str = f"{e['ip']}:{e['port']}" if e['ip'] else "no IP"
        choices.append(f"USE  {e['name']}  ({ip_str})  [{e['stack']}]")
    choices.append(f"NEW  Create new {db_type} container")
    choice = fzf(choices, header=f"Use existing {db_type} or create new?")
    if not choice: return None
    if choice.startswith("USE"):
        name  = choice.split()[1]
        match = next((e for e in existing if e['name']==name), None)
        if not match: return None
        return {"type":db_type,"name":name,"ip":match['ip'],
                "port":match['port'],"stack":match['stack'],
                "new":False,"net":f"{svc_name}_net"}
    else:
        sc = fzf(db_stacks, header="Which db stack?")
        if not sc: sc = "db_0.yml"
        defs = {"postgres":("postgres:16-alpine","5432"),
                "mysql":("mariadb:10.11","3306"),
                "redis":("redis:7-alpine","6379"),
                "mongo":("mongo:7","27017")}
        img,port = defs.get(db_type,("postgres:16-alpine","5432"))
        db_name = ask("DB container name", f"{svc_name}-{db_type}")
        db_ip   = ask("DB IP address",     next_ip())
        db_pass = ask("DB password",       "changeme")
        db_db   = ask("DB name",           svc_name.replace("-","_"))
        return {"type":db_type,"name":db_name,"ip":db_ip,"port":port,
                "image":img,"password":db_pass,"dbname":db_db,
                "stack":sc,"new":True,"net":f"{svc_name}_net"}

# ── Build YAML blocks ─────────────────────────────────────────────────────────
def build_svc(name, image, ip, port, cfg, svc_num, db=None, redis=None):
    net = f"{name}_net"
    mac = rand_mac()
    nets = (f"    networks:\n      traefik_net:\n        priority: 1000\n"
            f"      {net}:\n        priority: 500")
    for xn in cfg.get("extra_networks",[]):
        if isinstance(xn,dict):
            for nn,np in xn.items():
                nets += f"\n      {nn}:\n        priority: {np}"
    env = [f'      - "{e}"' for e in cfg.get("extra_env",[])]
    if db:
        dt,dip,dport = db['type'],db['ip'],db['port']
        dpw=db.get('password','pass'); ddb=db.get('dbname',name)
        if dt=="postgres": env.append(f'      - "DATABASE_URL=postgresql://postgres:{dpw}@{dip}:{dport}/{ddb}"')
        elif dt=="mysql":  env.append(f'      - "DATABASE_URL=mysql://root:{dpw}@{dip}:{dport}/{ddb}"')
        elif dt=="redis":  env.append(f'      - "REDIS_URL=redis://{dip}:{dport}/0"')
        elif dt=="mongo":  env.append(f'      - "MONGODB_URI=mongodb://{dip}:{dport}/{ddb}"')
    if redis:
        rip=redis.get('ip','127.0.0.1'); rpt=redis.get('port','6379')
        env.append(f'      - "REDIS_URL=redis://{rip}:{rpt}/0"')
    env_b = ("    environment:\n" + "\n".join(env) + "\n") if env else ""
    vols  = [f'      - "/home/user/docker/{name}:/data"']
    for v in cfg.get("extra_volumes",[]): vols.append(f'      - "{v}"')
    sg     = cfg.get("sablier_group","") or name.replace("-","").replace("_","")
    labels = ['      - "traefik.enable=true"',
              '      - "sablier.enable=true"',
              f'      - "sablier.group={sg}"']
    for l in cfg.get("extra_labels",[]): labels.append(f'      - "{l}"')
    use_caps = cfg.get("use_common_caps", True)
    caps  = "    <<: *common-caps\n" if use_caps else ""
    blkio = "    blkio_config: {weight: 500, device_read_bps: [{path: /dev/nvme0n1, rate: 300mb}], device_write_bps: [{path: /dev/nvme0n1, rate: 300mb}]}\n" if cfg.get("blkio") else ""
    ulim  = "    ulimits: {memlock: {soft: -1, hard: -1}, nofile: {soft: 65535, hard: 65535}, nproc: 65535}\n" if cfg.get("ulimits") else ""
    dep   = "    deploy: {resources: {limits: {memory: 2G, cpus: '4.0', pids: 1000}, reservations: {memory: 256M, cpus: '0.5'}}}\n" if cfg.get("deploy_limits") else ""
    log   = "" if use_caps else ("    logging: {driver: json-file, options: {max-size: 50m, max-file: '5'}}\n" if cfg.get("logging") else "")
    dns   = "\n".join(f'      - "{d}"' for d in cfg.get("dns",["192.168.1.114","8.8.8.8"]))
    num   = str(svc_num).zfill(2)
    return f"""
  # ---------------------------------------------------------
  # {num}. {name.upper()} 🐳
  # Description: {image} service — edit description here ✅
  # ---------------------------------------------------------
  {name}:
{caps}    image: {image}
    container_name: {name}
    hostname: {name}
    domainname: {name}.example.com
    mac_address: "{mac}"
{"" if use_caps else f"    cpuset: \"{cfg.get('cpuset','0-15')}\"\n    cpu_shares: {cfg.get('cpu_shares',4096)}\n    stop_grace_period: {cfg.get('stop_grace_period','120s')}\n    stop_signal: {cfg.get('stop_signal','SIGTERM')}\n    restart: {cfg.get('restart','no')}\n    user: \"{cfg.get('user','0:0')}\"\n"}{nets}
    ports:
      - "{ip}:{port}:{port}"
{env_b}    volumes:
{chr(10).join('  '+v for v in vols)}
    labels:
{chr(10).join('  '+l for l in labels)}
{"    dns:\n" + dns + "\n" if not use_caps else ""}    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:8080 || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 30s
{blkio}{ulim}{dep}{log}"""

def build_db_block(db, svc_name):
    dt,name,ip,port = db['type'],db['name'],db['ip'],db['port']
    img=db.get('image','postgres:16-alpine')
    pw =db.get('password','changeme')
    dbn=db.get('dbname',svc_name.replace('-','_'))
    net=f"{svc_name}_net"; mac=rand_mac()
    env_map = {
        "postgres": f'      - "POSTGRES_PASSWORD={pw}"\n      - "POSTGRES_DB={dbn}"',
        "mysql":    f'      - "MYSQL_ROOT_PASSWORD={pw}"\n      - "MARIADB_DATABASE={dbn}"',
        "redis":    '      - "REDIS_REPLICATION_MODE=master"',
        "mongo":    f'      - "MONGO_INITDB_DATABASE={dbn}"',
    }
    vol = f"{name}-data"
    return f"""
  # ---------------------------------------------------------
  # {name.upper()} — {dt.upper()} for {svc_name} 🐳
  # ---------------------------------------------------------
  {name}:
    image: {img}
    container_name: {name}
    hostname: {name}
    mac_address: "{mac}"
    restart: "no"
    networks:
      {net}:
        priority: 1000
    ports:
      - "{ip}:{port}:{port}"
    environment:
{env_map.get(dt,'')}
    volumes:
      - "{vol}:/var/lib/postgresql/data"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres || exit 1"]
      interval: 5s
      timeout: 3s
      retries: 10
      start_period: 30s
""", vol

# ── Inject into stack file ────────────────────────────────────────────────────
def inject(stack, block, network, volume=None):
    fpath = os.path.join(STACKS_DIR, stack)
    if not fpath.endswith('.yml'): fpath += '.yml'
    if not os.path.isfile(fpath): return False
    content = open(fpath).read()
    # Duplicate check
    m = re.search(r"^  ([a-zA-Z0-9_.\-]+):", block, re.MULTILINE)
    if m and re.search(rf"^  {re.escape(m.group(1))}:", content, re.MULTILINE):
        print(f"\n  \033[1;31m✘ {m.group(1)} already exists in {stack}\033[0m")
        return False
    if network and network not in content:
        content = re.sub(r"^(networks:\n)",
                        f"\\1  {network}: {{name: {network}, external: true}}\n",
                        content,count=1,flags=re.MULTILINE)
    if volume and volume not in content:
        content = re.sub(r"^(volumes:\n)",
                        f"\\1  {volume}: {{name: {volume}, external: true}}\n",
                        content,count=1,flags=re.MULTILINE)
    if "##STACKS_ART_START_FOOTER" in content:
        content = content.replace("##STACKS_ART_START_FOOTER",
                                  block.rstrip()+"\n\n##STACKS_ART_START_FOOTER",1)
    else:
        # Find last top-level # line (footer art) and insert before it
        lines = content.splitlines(keepends=True)
        insert = len(lines)
        for i in range(len(lines)-1,-1,-1):
            if not lines[i].startswith('#') and lines[i].strip():
                insert = i+1; break
        lines.insert(insert, block.rstrip()+"\n\n")
        content = ''.join(lines)
    open(fpath,'w').write(content)
    return True

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    args = [a for a in sys.argv[1:]
            if a not in ('--progress',) and not a.startswith('/tmp/')]
    log  = []
    cfg  = load_conf()

    image=None; svc_name=None; target_stack=None
    if len(args)>=3: image=args[0]; svc_name=args[1]; target_stack=args[2]
    elif len(args)==2: svc_name=args[0]; target_stack=args[1]
    elif len(args)==1: svc_name=args[0]

    init_ui(target_stack or "build", svc_name or "service")

    # ── 1. Stack selection ─────────────────────────────────────────────────
    if not target_stack:
        update("Select target stack...", 5)
        stacks = sorted([f.replace('.yml','') for f in os.listdir(STACKS_DIR)
                        if f.endswith('.yml') and not f.startswith('db_')])
        target_stack = fzf(stacks, header="Which stack to add service to?")
        if not target_stack:
            clear_ui(); print("  \033[1;31m✘ Cancelled.\033[0m"); sys.exit(1)
        global _st_target
        _st_target = target_stack

    # ── 2. Image search ────────────────────────────────────────────────────
    if not image:
        update(f"Searching Docker Hub for {svc_name}...", 10)
        image = hub_search(svc_name)
        if not image:
            clear_ui(); print("  \033[1;31m✘ Cancelled.\033[0m"); sys.exit(1)
        log.append(f"Image: {image}")

    update(f"Image: {image}", 20)

    # ── 3. Service details ─────────────────────────────────────────────────
    update("Getting service details...", 25)
    svc_ip  = ask("Service IP (192.168.1.x)", next_ip())
    svc_port = ask("Service port", "8080")
    svc_name_in = ask("Container name", svc_name)
    if svc_name_in: svc_name = svc_name_in
    log.append(f"Name: {svc_name}  IP: {svc_ip}")

    # ── 4. Detect & configure database ────────────────────────────────────
    update("Inspecting image...", 30)
    reqs    = detect_db(image)
    detected= [k for k,v in reqs.items() if v]
    if detected:
        update(f"Detected: {', '.join(detected)}", 35)

    db_info=None; redis_info=None
    for dt,needed in reqs.items():
        if needed:
            # Check if a db for this service already exists
            existing = find_existing(dt)
            already = [e for e in existing if svc_name.replace('-','').replace('_','') in 
                      e['name'].replace('-','').replace('_','')]
            if already:
                e = already[0]
                pr(f"  \033[1;32m✔ Found existing {dt}: {e['name']} ({e['ip']}:{e['port']})\033[0m")
                info = {"type":dt,"name":e["name"],"ip":e["ip"],
                       "port":e["port"],"stack":e["stack"],"new":False,
                       "net":f"{svc_name}_net"}
                if dt=="redis": redis_info=info
                else: db_info=info
            else:
                pr(f"  \033[1;33m⚠ No existing {dt} found for {svc_name}\033[0m")
                yn = ask(f"Add a new {dt} database? (y/n)", "y")
                if yn.lower()=='y':
                    # Ask which db stack to add it to
                    db_stacks = sorted([f.replace('.yml','') for f in os.listdir(STACKS_DIR)
                                       if re.match(r'db_\d+\.yml', f)])
                    update(f"Select db stack for {dt}...", 50)
                    db_target = fzf(db_stacks, header=f"Which db stack to add {dt} to?")
                    if db_target:
                        info = setup_db(dt, svc_name)
                        if info: info['stack'] = db_target + '.yml'
                        if dt=="redis": redis_info=info
                        else: db_info=info

    # ── 5. Manual db prompt ────────────────────────────────────────────────
    if not db_info:
        yn = ask("Does this service need a database? (y/n)", "n")
        if yn.lower()=='y':
            dt = fzf(["postgres","mysql","redis","mongo","none"], header="Database type?")
            if dt and dt!="none":
                db_info = setup_db(dt, svc_name)

    # ── 6. Redis ───────────────────────────────────────────────────────────
    if not redis_info:
        yn = ask("Does this service need Redis? (y/n)", "n")
        if yn.lower()=='y':
            redis_info = setup_db("redis", svc_name)

    # ── 6b. Companion container ───────────────────────────────────────────
    companion_info = None
    yn = ask("Does this service need a companion container? (y/n)", "n")
    if yn.lower() == 'y':
        update("Search for companion image...", 50)
        comp_img = hub_search(svc_name)
        if comp_img:
            comp_name = ask("Companion container name", svc_name + "-worker")
            comp_stacks = sorted([f.replace('.yml','') for f in os.listdir(STACKS_DIR)
                                 if f.endswith('.yml') and not f.startswith('db_')])
            update(f"Select stack for companion...", 55)
            comp_stack = fzf(comp_stacks, header=f"Which stack for {comp_name}?")
            if comp_stack:
                companion_info = {
                    'name': comp_name,
                    'image': comp_img,
                    'stack': comp_stack,
                    'desc': 'companion service'
                }

    # ── 7. Build scaffold ──────────────────────────────────────────────────
    update("Building compose scaffold...", 70)
    fpath = os.path.join(STACKS_DIR,
                         target_stack if target_stack.endswith('.yml')
                         else target_stack+'.yml')
    try:
        existing  = open(fpath).read()
        in_services = False
        svc_count = 0
        for line in existing.splitlines():
            if re.match(r'^services:', line): in_services = True; continue
            if re.match(r'^[a-zA-Z]', line) and not line.startswith(' '): in_services = False; continue
            if not in_services: continue
            if re.match(r'^  [a-zA-Z0-9][a-zA-Z0-9_.\-]+:\s*$', line) and not line.strip().startswith('x-'):
                svc_count += 1
    except: svc_count=0
    svc_num   = svc_count+1
    svc_net   = f"{svc_name}_net"
    svc_block = build_svc(svc_name,image,svc_ip,svc_port,cfg,svc_num,db_info,redis_info)

    # ── 8. Inject ──────────────────────────────────────────────────────────
    update(f"Injecting into {target_stack}...", 80)
    if inject(target_stack, svc_block, svc_net):
        log.append(f"✔ Added #{svc_num} {svc_name} to {target_stack}")

    if db_info and db_info.get("new"):
        update(f"Adding DB to {db_info['stack']}...", 85)
        dblk,dvol = build_db_block(db_info,svc_name)
        if inject(db_info['stack'],dblk,svc_net,dvol):
            log.append(f"✔ DB {db_info['name']} → {db_info['stack']}")
        subprocess.run(["docker","volume","create",dvol],capture_output=True)

    if redis_info and redis_info.get("new"):
        update(f"Adding Redis to {redis_info['stack']}...", 87)
        rblk,rvol = build_db_block(redis_info,svc_name)
        if inject(redis_info['stack'],rblk,svc_net,rvol):
            log.append(f"✔ Redis → {redis_info['stack']}")

    if companion_info:
        update(f"Adding companion {companion_info['name']}...", 89)
        comp_block = build_svc(
            companion_info['name'],
            companion_info['image'],
            next_ip(),
            svc_port,
            cfg,
            svc_num + 1,
            None, None
        )
        if inject(companion_info['stack'], comp_block, svc_net):
            log.append(f"✔ Companion {companion_info['name']} → {companion_info['stack']}")

    # ── 9. Network ─────────────────────────────────────────────────────────
    update(f"Creating network {svc_net}...", 92)
    r = subprocess.run(["docker","network","inspect",svc_net],capture_output=True)
    if r.returncode!=0:
        subprocess.run(["docker","network","create",svc_net],capture_output=True)
        log.append(f"Network: {svc_net}")

    update("Build complete! ✨", 100)
    time.sleep(0.3)

    # Write log
    lpath = f"/srv/stacks/stacks_build_{svc_name}.log"
    with open(lpath,'w') as f:
        f.write(f"=== Build: {svc_name} ===\n")
        for l in log: f.write(l+"\n")

    # ── 10. Ask to start ───────────────────────────────────────────────────
    # Final questions — reset terminal state first
    sys.stdout.write("\033[0m\n")
    sys.stdout.flush()
    update(f"✨ {svc_name} built successfully!", 100)
    yn = ask(f"Start {svc_name} now? (y/n)", "n")
    if yn.lower()=='y':
        mode = ask("(s)ervice only or (w)hole stack?","s")
        if mode.lower().startswith('w'):
            print(f"\n\033[1;32m✨ BUILD COMPLETE: {svc_name}\033[0m")
            print(f"BUILD_OK:{svc_name}")
            print(f"BUILD_START:{target_stack}")
        else:
            print(f"\n\033[1;32m✨ BUILD COMPLETE: {svc_name}\033[0m")
            print(f"BUILD_OK:{svc_name}")
            print(f"BUILD_START:{target_stack} {svc_name}")
    else:
        yn2 = ask(f"Start whole stack {target_stack}? (y/n)", "n")
        print(f"\n\033[1;32m✨ BUILD COMPLETE: {svc_name}\033[0m")
        if yn2.lower() == 'y':
            print(f"BUILD_OK:{svc_name}")
            print(f"BUILD_START:{target_stack}")
        else:
            print(f"BUILD_OK:{svc_name}")

if __name__ == "__main__":
    main()
