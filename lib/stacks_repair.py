#!/usr/bin/env python3
"""
stacks_repair.py — Deep compose file repair based on learned patterns from dev_1.yml
Fixes structural corruption, missing keys, bad indentation, and injection artifacts.
Called by stacks fix as Phase 0.5 corruption repair.
"""
import re, os, sys, subprocess, shutil, time, glob

# ── Templates learned from dev_1.yml (perfect reference file) ────────────────
TEMPLATES = {
    'blkio_config': "    blkio_config: {weight: 500, device_read_bps: [{path: /dev/nvme0n1, rate: 500mb}], device_write_bps: [{path: /dev/nvme0n1, rate: 500mb}]}",
    'ulimits':      "    ulimits: {memlock: {soft: -1, hard: -1}, nofile: {soft: 65535, hard: 65535}, nproc: 65535}",
    'storage_opt':  "    storage_opt: {size: 10G}",
    'deploy':       "    deploy: {placement: {constraints: [node.labels.priority == high]}, resources: {limits: {memory: 1G, cpus: '0.2', pids: 1000}, reservations: {memory: 100M, cpus: '0.05'}}}",
}

LABEL_INDENT = '      '  # 6 spaces
SERVICE_INDENT = '  '    # 2 spaces
NETWORK_PRIORITIES = {'traefik_net': 1000}
DEFAULT_NET_PRIORITY = 500


# ── Snapshot store (proven-good compose files) ───────────────────────────────
def _snap_conf():
    """Read snapshot/repair settings from global_inject.conf."""
    cfg = {}
    cp = os.path.expanduser("~/.config/stacks/global_inject.conf")
    try:
        with open(cp) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return {
        'dir':        os.path.expanduser(cfg.get('SNAPSHOT_DIR', '~/.config/stacks/snapshots')),
        'keep':       int(cfg.get('SNAPSHOT_KEEP', '5') or '5'),
        'require':    cfg.get('SNAPSHOT_REQUIRE', 'none-failed'),
        'on_success': str(cfg.get('SNAPSHOT_ON_SUCCESS', '1')).lower() in ('1', 'true'),
        'use':        str(cfg.get('REPAIR_USE_SNAPSHOT', '1')).lower() in ('1', 'true'),
    }


def _validate(path):
    """True if `docker compose config` succeeds (ignoring the AK_OUTPOST var warning)."""
    try:
        r = subprocess.run(["docker", "compose", "-f", path, "config"],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def _stack_services(path):
    try:
        r = subprocess.run(["docker", "compose", "-f", path, "config", "--services"],
                           capture_output=True, text=True, timeout=60)
        return [x for x in r.stdout.split() if x] if r.returncode == 0 else []
    except Exception:
        return []


def _stack_state_ok(path, require):
    """Gate: no container in a bad state. 'all-healthy' = every svc up & healthy;
       'none-failed' = nothing restarting/dead/unhealthy (sleeping/Sablier OK)."""
    svcs = _stack_services(path)
    if not svcs:
        return False
    bad = ('restarting', 'dead', 'removing')
    for svc in svcs:
        try:
            r = subprocess.run(["docker", "inspect", "-f",
                 "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}", svc],
                 capture_output=True, text=True, timeout=15)
        except Exception:
            return False
        if r.returncode != 0:
            if require == 'all-healthy':
                return False
            continue  # not created = sleeping/Sablier, OK for none-failed
        status, _, health = r.stdout.strip().partition('|')
        # restarting/dead/removing = genuine failure, always bad
        if status in bad:
            return False
        # exited/created = intentionally stopped (Sablier); stale health is meaningless
        if status not in ('running',):
            if require == 'all-healthy':
                return False
            continue
        # running:
        if require == 'all-healthy' and health and health != 'healthy':
            return False
        # none-failed tolerates running+unhealthy (cosmetic healthchecks)
    return True


def snapshot_if_proven(path):
    """Save a versioned .good snapshot only if the file validates AND the stack
       is in an acceptable running state. Returns the snapshot path or None."""
    c = _snap_conf()
    if not c['on_success']:
        return None
    if not _validate(path):
        return None
    if not _stack_state_ok(path, c['require']):
        return None
    os.makedirs(c['dir'], exist_ok=True)
    stack = os.path.basename(path).replace('.yml', '').replace('.yaml', '')
    snap = os.path.join(c['dir'], "%s.good.%d" % (stack, int(time.time())))
    shutil.copy2(path, snap)
    # prune to keep newest N
    existing = sorted(glob.glob(os.path.join(c['dir'], "%s.good.*" % stack)))
    for old in existing[:-c['keep']]:
        try: os.remove(old)
        except OSError: pass
    return snap


def _snapshots_for(stack):
    """Return this stack's .good snapshots, newest first."""
    c = _snap_conf()
    g = glob.glob(os.path.join(c['dir'], "%s.good.*" % stack))
    return sorted(g, reverse=True)


def _deploy_health_ok(path, require, settle):
    """Judge health right after `up`, before Sablier sleeps anything.
       Polls briefly so 'starting' healthchecks can flip to 'healthy'.
       restarting/dead/removing = fail. exited/created = ignored (not what we just upped)."""
    svcs = _stack_services(path)
    if not svcs:
        return False
    deadline = time.time() + max(1, int(settle))
    bad = ('restarting', 'dead', 'removing')
    while True:
        pending = False
        ok = True
        for svc in svcs:
            try:
                r = subprocess.run(["docker", "inspect", "-f",
                     "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}", svc],
                     capture_output=True, text=True, timeout=15)
            except Exception:
                ok = False; break
            if r.returncode != 0:
                continue  # not created — not part of what came up
            status, _, health = r.stdout.strip().partition('|')
            if status in bad:
                ok = False; break
            if status != 'running':
                continue  # exited/created right after up = fine to ignore
            if health == 'starting':
                pending = True
            elif health == 'unhealthy':
                if require == 'all-healthy':
                    ok = False; break
                # none-failed tolerates cosmetic unhealthy
        if not ok:
            return False
        if not pending or time.time() >= deadline:
            return ok
        time.sleep(2)


def _save_snapshot(path, c):
    os.makedirs(c['dir'], exist_ok=True)
    stack = os.path.basename(path).replace('.yml', '').replace('.yaml', '')
    snap = os.path.join(c['dir'], "%s.good.%d" % (stack, int(time.time())))
    shutil.copy2(path, snap)
    existing = sorted(glob.glob(os.path.join(c['dir'], "%s.good.*" % stack)))
    for old in existing[:-c['keep']]:
        try: os.remove(old)
        except OSError: pass
    return snap


def snapshot_after_up(path):
    """Deploy-time snapshot: called right after `up`, before Sablier sleeps.
       This is the ONLY moment health is true. Saves .good if clean."""
    c = _snap_conf()
    if not c['on_success'] or not _validate(path):
        return None
    settle = _snap_conf_int('SNAPSHOT_SETTLE_SECS', 15)
    if _deploy_health_ok(path, c['require'], settle):
        return _save_snapshot(path, c)
    return None


def _snap_conf_int(key, default):
    cp = os.path.expanduser("~/.config/stacks/global_inject.conf")
    try:
        for line in open(cp):
            line = line.strip()
            if line.startswith(key + '='):
                return int(line.split('=', 1)[1].strip())
    except (OSError, ValueError):
        pass
    return default



def repair_file(path, dry_run=False):
    """Run all repair passes on a single compose file. Returns list of fixes made."""
    content = open(path).read()
    original = content
    fixes = []

    content, f = fix_corrupt_blkio(content)
    fixes += f

    content, f = fix_labels_in_networks(content)
    fixes += f

    content, f = fix_duplicate_labels(content)
    fixes += f

    content, f = fix_missing_closing_quotes(content)
    fixes += f

    content, f = fix_n_labels(content)
    fixes += f

    content, f = fix_name_field(content, path)
    fixes += f

    # ── Structural passes (dedup + phantom depends_on) ──
    content, f = fix_duplicate_service_keys(content)
    fixes += f

    content, f = fix_undefined_depends(content)
    fixes += f

    content, f = fix_dependency_cycles(content)
    fixes += f

    content, f = fix_network_form(content)
    fixes += f

    content, f = fix_undefined_networks(content)
    fixes += f

    if not dry_run and content != original:
        # back up the broken file before writing the repaired version
        try:
            bdir = os.path.expanduser("~/.config/stacks/snapshots/repair-backups")
            os.makedirs(bdir, exist_ok=True)
            stack = os.path.basename(path)
            shutil.copy2(path, os.path.join(bdir, "%s.broken.%d" % (stack, int(time.time()))))
        except OSError:
            pass
        open(path, 'w').write(content)

    return fixes


def fix_corrupt_blkio(content):
    """Fix blkio_config where HC test values leaked into device_read_bps."""
    fixes = []
    pattern = r'device_read_bps:\s*\[[^\]]*(?:CMD|NONE|SHELL)[^\]]*\]'
    if re.search(pattern, content):
        content = re.sub(
            pattern,
            'device_read_bps: [{path: /dev/nvme0n1, rate: 500mb}]',
            content
        )
        fixes.append('corrupt_blkio: HC test leaked into blkio_config')
    return content, fixes


def fix_labels_in_networks(content):
    """Remove traefik/sablier label lines that got injected into networks: block."""
    fixes = []
    lines = content.split('\n')
    result = []
    in_networks = False

    for line in lines:
        if re.match(r'^networks:\s*$', line):
            in_networks = True
        elif re.match(r'^[a-zA-Z\[]', line) and not line.startswith(' '):
            in_networks = False

        if in_networks and re.match(r'\s+- "(traefik\.|sablier\.)', line):
            fixes.append(f'labels_in_networks: removed "{line.strip()}"')
            continue

        result.append(line)

    return '\n'.join(result), fixes


def fix_duplicate_labels(content):
    """Remove duplicate traefik.enable, sablier.enable, sablier.group lines per service."""
    fixes = []
    lines = content.split('\n')
    result = []
    seen_labels = set()
    in_labels = False

    for line in lines:
        # Reset on new service
        if re.match(r'^  [a-zA-Z0-9_-]+:\s*$', line):
            in_labels = False
            seen_labels = set()

        if re.match(r'^\s+labels:\s*$', line):
            in_labels = True
            seen_labels = set()
            result.append(line)
            continue

        if in_labels:
            if not line.strip().startswith('-'):
                in_labels = False
            else:
                stripped = line.strip()
                # Only dedup the core enable/group labels, not router-specific ones
                if any(x in stripped for x in ['traefik.enable=', 'sablier.enable=', 'sablier.group=']):
                    if stripped in seen_labels:
                        fixes.append(f'duplicate_label: removed duplicate {stripped}')
                        continue
                    seen_labels.add(stripped)

        result.append(line)

    return '\n'.join(result), fixes


def fix_missing_closing_quotes(content):
    """Fix sablier.group= lines missing closing quote."""
    fixes = []
    lines = content.split('\n')
    result = []
    for line in lines:
        if 'sablier.group=' in line:
            # Check if the line ends with a quote after the group value
            m = re.search(r'sablier\.group=([a-zA-Z0-9_-]+)', line)
            if m:
                val = m.group(1)
                expected = f'sablier.group={val}"'
                if expected not in line:
                    line = re.sub(r'sablier\.group=' + val + r'(?![a-zA-Z0-9_"-])', f'sablier.group={val}"', line)
                    fixes.append(f'missing_quote: fixed sablier.group={val}')
        result.append(line)
    return '\n'.join(result), fixes


def fix_n_labels(content):
    """Remove corrupted single-letter label lines like - \"n\" or - \"h\"."""
    fixes = []
    lines = content.split('\n')
    result = []
    for line in lines:
        if re.match(r'\s+- "[a-z]"\s*$', line):
            fixes.append(f'corrupt_label: removed {line.strip()}')
            continue
        result.append(line)
    return '\n'.join(result), fixes


def fix_name_field(content, path):
    """Ensure name: stackname is at top of file."""
    fixes = []
    stack_name = os.path.basename(path).replace('.yml','').replace('.yaml','')
    lines = content.split('\n')

    # Remove any existing name: lines
    has_correct = any(l == f'name: {stack_name}' for l in lines[:5])
    if has_correct:
        return content, fixes

    lines = [l for l in lines if not re.match(r'^name:\s*', l)]

    # Insert after leading comments
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith('#'):
            insert_at = i + 1
        else:
            break

    lines.insert(insert_at, f'name: {stack_name}')
    fixes.append(f'name_field: set to {stack_name}')
    return '\n'.join(lines), fixes


def fix_network_form(content):
    """Normalize service networks: blocks to mapping form (traefik_net priority 1000,
    others 500). Fixes mixed list+mapping (compose 5.1.4 rejects mixing) and dedupes.
    Repairs the exact corruption compose 5.1.4 started rejecting."""
    fixes = []
    lines = content.split('\n')
    out = []
    i = 0
    n = len(lines)
    while i < n:
        l = lines[i]
        if re.match(r'^    networks:\s*$', l):
            out.append(l); i += 1
            nets = []  # preserve order, dedupe
            seen = set()
            mixed = False
            saw_list = False; saw_map = False
            while i < n:
                lm = re.match(r'^      -\s+"?([a-zA-Z0-9_.-]+)"?\s*$', lines[i])
                mm = re.match(r'^      ([a-zA-Z0-9_.-]+):\s*$', lines[i])
                if lm:
                    saw_list = True
                    net = lm.group(1)
                    if net not in seen: seen.add(net); nets.append(net)
                    i += 1
                elif mm:
                    saw_map = True
                    net = mm.group(1)
                    if net not in seen: seen.add(net); nets.append(net)
                    i += 1
                    # skip its child lines (priority etc, 8-space)
                    while i < n and re.match(r'^        \S', lines[i]):
                        i += 1
                elif re.match(r'^      ', lines[i]):
                    i += 1  # stray indented line, skip
                else:
                    break
            if saw_list and saw_map:
                mixed = True
            # rebuild in mapping form
            for net in nets:
                pri = 1000 if net == 'traefik_net' else 500
                out.append('      %s:' % net)
                out.append('        priority: %d' % pri)
            if mixed:
                fixes.append("network_form: normalized mixed list/mapping networks block to mapping form")
            elif saw_list:
                fixes.append("network_form: converted list-form networks to mapping form")
            continue
        out.append(l); i += 1
    return '\n'.join(out), fixes


def fix_undefined_networks(content):
    """Remove service network references not defined in the file's top-level networks:.
    Strips the network key AND its child lines (e.g. ipv4_address). If a service ends
    up with no networks, ensure it's on traefik_net (the universal floor)."""
    fixes = []
    lines = content.split('\n')
    # defined top-level networks
    defined = set()
    in_net = False
    for line in lines:
        if re.match(r'^networks:\s*$', line):
            in_net = True; continue
        if re.match(r'^[a-zA-Z]', line) and not line.startswith(' '):
            in_net = False
        if in_net:
            m = re.match(r'^  ([a-zA-Z0-9_.-]+):', line)
            if m:
                defined.add(m.group(1))
    if 'traefik_net' not in defined:
        return content, fixes  # safety: don't touch if traefik_net isn't even defined

    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = re.match(r'^(    )networks:\s*$', line)
        if not m:
            out.append(line); i += 1; continue
        # entering a service-level networks: block (4-space)
        out.append(line)
        i += 1
        kept_any = False
        while i < n:
            nl = re.match(r'^      ([a-zA-Z0-9_.-]+):\s*$', lines[i])      # net key (6sp)
            nl_inline = re.match(r'^      ([a-zA-Z0-9_.-]+):\s*\{', lines[i])
            if nl or nl_inline:
                net = (nl or nl_inline).group(1)
                if net in defined:
                    out.append(lines[i]); i += 1; kept_any = True
                    # keep its child lines (8-space) if block form
                    if nl:
                        while i < n and re.match(r'^        \S', lines[i]):
                            out.append(lines[i]); i += 1
                else:
                    fixes.append("undefined_network: removed '%s' from service" % net)
                    i += 1
                    # skip its child lines (8-space)
                    while i < n and re.match(r'^        \S', lines[i]):
                        i += 1
            elif re.match(r'^    \S', lines[i]) or re.match(r'^  \S', lines[i]) or re.match(r'^\S', lines[i]):
                break  # left the networks block
            else:
                out.append(lines[i]); i += 1
        if not kept_any:
            out.append('      traefik_net:')
            out.append('        priority: 1000')
            fixes.append("undefined_network: service left networkless -> added traefik_net")
    return '\n'.join(out), fixes


def fix_dependency_cycles(content):
    """Break dependency cycles. Builds the depends_on graph; for any 2-node cycle
    A->B and B->A, removes the back-edge from the service that is itself depended-on
    (the support service). Only removes edges that actually form a cycle."""
    fixes = []
    lines = content.split('\n')
    svc_re = re.compile(r'^  ([a-zA-Z0-9_.+-]+):\s*$')
    # map each service -> set of deps, and remember line index of each dep entry
    graph = {}
    dep_lines = {}   # (svc, dep) -> line index
    cur = None
    in_dep = False
    for i, line in enumerate(lines):
        m = svc_re.match(line)
        if m:
            cur = m.group(1); graph.setdefault(cur, set()); in_dep = False; continue
        if cur is None:
            continue
        if re.match(r'^    depends_on:\s*$', line):
            in_dep = True; continue
        if in_dep:
            dm = re.match(r'^      -\s+(["\']?)([a-zA-Z0-9_.+-]+)\1\s*$', line)
            if dm:
                graph[cur].add(dm.group(2))
                dep_lines[(cur, dm.group(2))] = i
            else:
                in_dep = False
    # find 2-node cycles
    remove = set()  # line indices to drop
    for a in list(graph):
        for b in list(graph.get(a, ())):
            if b in graph and a in graph.get(b, ()):
                # cycle a<->b. Remove the edge on whichever is depended-on by the OTHER's role.
                # Heuristic: remove b->a if a->b also exists (keep the first-declared direction).
                # Choose to drop the edge from the service that has MORE deps (the over-linked support svc).
                victim = a if len(graph[a]) >= len(graph[b]) else b
                other = b if victim == a else a
                key = (victim, other)
                if key in dep_lines:
                    remove.add(dep_lines[key])
                    graph[victim].discard(other)
                    fixes.append("dependency_cycle: removed '%s' from %s.depends_on" % (other, victim))
    if not remove:
        return content, fixes
    new_lines = [l for i, l in enumerate(lines) if i not in remove]
    return '\n'.join(new_lines), fixes


def fix_undefined_depends(content):
    """Remove depends_on entries that point at services not defined in this file.
    If depends_on becomes empty, remove the key entirely."""
    fixes = []
    lines = content.split('\n')
    # collect all defined service names (2-space indent, under services:)
    svc_re = re.compile(r'^  ([a-zA-Z0-9_.+-]+):\s*$')
    defined = set()
    in_services = False
    for line in lines:
        if re.match(r'^services:\s*$', line):
            in_services = True; continue
        if re.match(r'^[a-zA-Z]', line) and not line.startswith(' '):
            in_services = False
        if in_services:
            m = svc_re.match(line)
            if m:
                defined.add(m.group(1))
    if not defined:
        return content, fixes
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # detect a depends_on: block (list form) at 4-space indent
        m = re.match(r'^(\s+)depends_on:\s*$', line)
        if m:
            indent = m.group(1)
            j = i + 1
            kept = []
            removed = []
            while j < n:
                dm = re.match(r'^\s+-\s+(["\']?)([a-zA-Z0-9_.+-]+)\1\s*$', lines[j])
                if dm and len(lines[j]) - len(lines[j].lstrip()) > len(indent):
                    dep = dm.group(2)
                    if dep in defined:
                        kept.append(lines[j])
                    else:
                        removed.append(dep)
                    j += 1
                else:
                    break
            if removed:
                for d in removed:
                    fixes.append("undefined_depends: removed '%s'" % d)
                if kept:
                    out.append(line); out.extend(kept)
                # if nothing kept, drop the depends_on: line entirely
                i = j
                continue
            else:
                out.append(line)
                i += 1
                continue
        # also handle inline form: depends_on: [a, b]
        mi = re.match(r'^(\s+)depends_on:\s*\[(.*)\]\s*$', line)
        if mi:
            indent, body = mi.group(1), mi.group(2)
            deps = [d.strip().strip('"\'') for d in body.split(',') if d.strip()]
            keep = [d for d in deps if d in defined]
            drop = [d for d in deps if d not in defined]
            if drop:
                for d in drop:
                    fixes.append("undefined_depends: removed '%s'" % d)
                if keep:
                    out.append('%sdepends_on: [%s]' % (indent, ', '.join(keep)))
                i += 1
                continue
        out.append(line)
        i += 1
    return '\n'.join(out), fixes


def fix_duplicate_service_keys(content):
    """Remove duplicate service definitions. When a service name appears twice
    under services:, keep the block with more keys (more complete), drop the other."""
    fixes = []
    lines = content.split('\n')
    # find service block boundaries: lines matching ^  <name>:  (2-space indent, top-level service)
    svc_re = re.compile(r'^  ([a-zA-Z0-9_.+-]+):\s*$')
    blocks = []  # (name, start, end)
    cur = None
    for i, line in enumerate(lines):
        m = svc_re.match(line)
        if m:
            if cur:
                blocks.append((cur[0], cur[1], i))
            cur = (m.group(1), i)
        elif re.match(r'^[a-zA-Z]', line) and cur:
            # left the services section
            blocks.append((cur[0], cur[1], i)); cur = None
    if cur:
        blocks.append((cur[0], cur[1], len(lines)))
    # group by name
    from collections import defaultdict
    by_name = defaultdict(list)
    for b in blocks:
        by_name[b[0]].append(b)
    drop_ranges = []
    for name, bl in by_name.items():
        if len(bl) < 2:
            continue
        # score each by number of non-blank lines (completeness); keep the max
        scored = sorted(bl, key=lambda b: sum(1 for l in lines[b[1]:b[2]] if l.strip()), reverse=True)
        keep = scored[0]
        for b in scored[1:]:
            drop_ranges.append(b)
            fixes.append("duplicate_service: removed second '%s' block (lines %d-%d)" % (name, b[1]+1, b[2]))
    if not drop_ranges:
        return content, fixes
    drop = set()
    for (_, st, en) in drop_ranges:
        drop.update(range(st, en))
    new_lines = [l for i, l in enumerate(lines) if i not in drop]
    return '\n'.join(new_lines), fixes


def _compose_error(path):
    """Run compose config, return (ok, line_no, message). line_no None if not parseable."""
    try:
        r = subprocess.run(["docker","compose","-f",path,"config"],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        return False, None, str(e)
    if r.returncode == 0:
        return True, None, ""
    err = r.stderr.strip()
    # filter the harmless unset-variable warnings
    lines = [l for l in err.splitlines()
             if 'variable is not set' not in l and 'AK_OUTPOST' not in l]
    msg = lines[-1] if lines else err
    # try to extract a line number: "line 206" or "line 168, column 7"
    lno = None
    m = re.findall(r'line (\d+)', msg)
    if m:
        lno = int(m[-1])  # the deepest/last line number compose reports
    return False, lno, msg


def repair_loop(path, max_passes=25, logf=None):
    """Error-driven surgical repair. Runs compose config, reads ONE error at a time,
    fixes just that piece in place, re-validates, repeats until valid or stuck.
    NEVER reverts the whole file or deletes user additions. Returns list of actions."""
    actions = []
    def _log(m):
        actions.append(m)
        if logf:
            try: open(logf,'a').write(m+"\n")
            except OSError: pass

    last_err = None
    for _pass in range(max_passes):
        ok, lno, msg = _compose_error(path)
        if ok:
            _log("repair_loop: VALID after %d pass(es)" % _pass)
            return actions
        if msg == last_err:
            # no progress on the same error -> stop to avoid infinite loop
            _log("repair_loop: STUCK on: %s" % msg)
            break
        last_err = msg
        content = open(path).read()
        before = content
        # classify + dispatch to the right in-place fixer
        fixed_by = None
        ml = msg.lower()
        if 'did not find expected' in ml or 'mapping values' in ml or 'block collection' in ml or 'found character' in ml:
            content, f = fix_network_form(content)          # mixed list/mapping nets
            if f: fixed_by = 'network_form'
            if not f:
                content2, f2 = _fix_indent_at(content, lno)  # generic indent repair
                if f2: content = content2; fixed_by = 'indent'
        elif 'already defined' in ml or 'are equal' in ml or 'duplicate' in ml:
            content, f = fix_duplicate_service_keys(content)
            if f: fixed_by = 'dup_service'
            if not f:
                content, f = fix_network_form(content)       # dedupes net lists too
                if f: fixed_by = 'dup_network'
        elif 'depends on undefined service' in ml:
            content, f = fix_undefined_depends(content)
            if f: fixed_by = 'undefined_depends'
        elif 'undefined network' in ml:
            content, f = fix_undefined_networks(content)
            if f: fixed_by = 'undefined_network'
        elif 'cycle' in ml:
            content, f = fix_dependency_cycles(content)
            if f: fixed_by = 'dependency_cycle'
        if fixed_by and content != before:
            shutil.copy2(path, path + ".prerepair")
            open(path,'w').write(content)
            _log("repair_loop pass %d: %s -> fixed (%s)" % (_pass, msg[:80], fixed_by))
        else:
            _log("repair_loop pass %d: NO FIXER for: %s" % (_pass, msg[:120]))
            break
    return actions


def _fix_indent_at(content, lno):
    """Generic indentation repair near a reported line. Conservative: only
    re-aligns a service-key line that is off the 4-space grid. Returns (content, fixed)."""
    if not lno:
        return content, False
    lines = content.split('\n')
    i = lno - 1
    if i < 0 or i >= len(lines):
        return content, False
    # look at the offending line + a few around it for a common indent mistake:
    # a key indented by an odd number of spaces inside a service block
    fixed = False
    for j in range(max(0,i-2), min(len(lines), i+2)):
        l = lines[j]
        st = l.lstrip(' ')
        ind = len(l) - len(st)
        # service-level keys should be 4 spaces; list items 6; net children 8
        if st and not st.startswith('-') and st.endswith(':') and ind in (3,5):
            lines[j] = (' ' * (ind + 1 if ind in (3,5) else ind)) + st
            fixed = True
    return ('\n'.join(lines), fixed) if fixed else (content, False)


def scan_all(stacks_dir, dry_run=False):
    """Scan all yml files and repair them."""
    total_fixes = 0
    for fname in sorted(os.listdir(stacks_dir)):
        if not fname.endswith('.yml'): continue
        path = os.path.join(stacks_dir, fname)
        fixes = repair_file(path, dry_run=dry_run)
        if fixes:
            print(f"{'[dry-run] ' if dry_run else ''}Fixed {fname}:")
            for f in fixes:
                print(f"  - {f}")
            total_fixes += len(fixes)
    print(f"\nTotal fixes: {total_fixes}")


if __name__ == '__main__':
    stacks_dir = sys.argv[1] if len(sys.argv) > 1 else '/srv/stacks/Stacks'
    dry_run = '--dry-run' in sys.argv
    scan_all(stacks_dir, dry_run=dry_run)
