#!/usr/bin/env python3
"""Shared config loader for the Stacks tool.

Single source of truth = ~/.config/stacks/stacks.yaml (clean, human-friendly).
Falls back to the legacy stacks.conf if the YAML is missing or unreadable.

Usage:
    from stacks_config import load
    cfg = load()                  # -> dict of INTERNAL keys (STACKS_DIR, FIX_*, ...)

    python3 stacks_config.py --env     # prints `export KEY='VALUE'` for bash
    python3 stacks_config.py --check    # diff YAML-derived values vs stacks.conf
"""
import os, sys, shlex

def _resolve_conf_dir():
    """Config dir, generic for any user. Priority:
    $STACKS_CONFIG_DIR → invoking user's home under sudo ($SUDO_USER) →
    $XDG_CONFIG_HOME/stacks → ~/.config/stacks."""
    d = os.environ.get("STACKS_CONFIG_DIR")
    if d: return os.path.expanduser(d)
    su = os.environ.get("SUDO_USER")
    if su and su != "root":
        try:
            import pwd
            return os.path.join(pwd.getpwnam(su).pw_dir, ".config", "stacks")
        except (KeyError, ImportError): pass
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg: return os.path.join(xdg, "stacks")
    return os.path.expanduser("~/.config/stacks")

CONF_DIR  = _resolve_conf_dir()
YAML_PATH = os.path.join(CONF_DIR, "stacks.yaml")
CONF_PATH = os.path.join(CONF_DIR, "stacks.conf")

# friendly YAML key -> internal scalar key (Claude's mapping from stacks_fix.py)
SCALAR_MAP = {
    'stacks_folder':'STACKS_DIR','dynamics_folder':'DYNAMICS_DIR','snapshots_folder':'SNAPSHOT_DIR',
    'info_log':'INFO_LOG','volume_folder':'FIX_VOLUME_BASE','delay_between_stacks':'DELAY',
    'master_network':'FIX_AUTO_NETWORKS','authoritative_networks':'FIX_AUTHORITATIVE_NETWORKS',
    'link_family_networks':'FIX_AUTO_LINK_NETWORKS','stack_wide_network':'FIX_AUTO_COMPOSE_NETWORK',
    'network_subnet_base':'FIX_SUBNET_BASE','build_network':'PRIMARY_NETWORK','build_subnet':'PRIMARY_SUBNET',
    'auto_name_containers':'FIX_AUTO_NAME_CONTAINERS','sync_all_names':'FIX_SYNC_ALL_NAMES',
    'sync_dynamic_names':'FIX_SYNC_DYNAMICS_NAMES','auto_name_stack_files':'FIX_AUTO_NAME',
    'convert_to_bind_mounts':'FIX_CONVERT_NAMED_TO_BIND','force_volume_folder':'FIX_FORCE_VOLUME_BASE',
    'external_volumes':'FIX_EXTERNAL_VOLUMES','remove_all_depends':'FIX_REMOVE_DEPENDS',
    'replace_broken_healthchecks':'FIX_REPLACE_BROKEN_HC','remove_orphan_networks':'FIX_REMOVE_ORPHANS',
    'deep_inspect':'FIX_DEEP_INSPECT','backup_before_changes':'FIX_BACKUP',
    'auto_create_creator':'FIX_AUTO_CREATE_CREATOR','creator_name':'FIX_CREATOR_NAME',
    'creator_max_networks':'FIX_CREATOR_MAX_NETWORKS','creator_max_volumes':'FIX_CREATOR_MAX_VOLUMES',
    'force_new_creator':'FIX_FORCE_NEW_CREATOR','ip_range_start':'IP_RANGE_START','ip_range_end':'IP_RANGE_END',
    'warn_on_ip_collision':'IP_COLLISION_WARN','autofix_ip_collisions':'IP_COLLISION_AUTOFIX',
    'skip_host_network_mode':'NETWORK_MODE_SKIP','port_range_start':'PORT_RANGE_START','port_range_end':'PORT_RANGE_END',
    'warn_on_port_collision':'PORT_COLLISION_WARN','force_all_healthchecks':'FIX_FORCE_HC',
    'sablier_scaling':'SABLIER_SCALE_ENABLED','auto_group_naming':'SCALE_AUTO_GROUP',
    'check_for_updates':'UPDATE_CHECK_ENABLED','update_check_hours':'UPDATE_CHECK_INTERVAL',
    'update_running_only':'UPDATE_CHECK_RUNNING_ONLY','auto_pull_updates':'UPDATE_AUTO_PULL',
    'notify_on_updates':'UPDATE_NOTIFY','domain':'DOMAIN','descriptions_file':'BUILD_DESC_FILE',
    'default_description':'BUILD_DEFAULT_DESC','run_fix_after_build':'BUILD_RUN_FIX',
    'normalize_domains':'FIX_NORMALIZE_DOMAINS',
    # network priorities
    'master_network_priority':'FIX_AUTO_NETWORK_PRIORITY',
    'family_network_priority':'FIX_AUTO_LINK_PRIORITY',
    'stack_wide_network_priority':'FIX_AUTO_COMPOSE_NETWORK_PRIORITY',
    # fix behaviour
    'auto_depends_on':'FIX_AUTO_DEPENDS_ON','create_volume_dirs':'FIX_CREATE_VOLUME_DIRS',
    'manage_dynamics':'FIX_DYNAMICS','remove_blank_gaps':'FIX_REMOVE_GAPS','strip_profiles':'FIX_STRIP_PROFILES',
    # anchor/service injection master toggles
    'inject_stop_grace':'INJECT_STOP_GRACE','inject_logging':'INJECT_LOGGING',
    'inject_restart_policy':'INJECT_RESTART','inject_resource_limits':'INJECT_DEPLOY',
    'inject_cpu_pinning':'INJECT_CPUSET','inject_block_io':'INJECT_BLKIO','inject_ulimits':'INJECT_ULIMITS',
    # dynamics generator (rich, config-driven) + DB entrypoint generation
    'rich_dynamics_generator':'GEN_RICH','generate_db_entrypoints':'GEN_DB_ENTRYPOINTS',
    # base/location overrides (everything else derives from data_folder if unset)
    'data_folder':'STACKS_DATA_DIR','logs_folder':'STACKS_LOG_DIR','backup_folder':'BACKUP_DEST',
}

# friendly YAML list key -> (internal key, join char)
LIST_MAP = {
    'ip_blacklist':('IP_BLACKLIST',','), 'port_blacklist':('PORT_BLACKLIST',','),
    'locked_ips':('LOCKED_IPS',','), 'ip_port_locked':('IP_PORT_LOCKED_CONTAINERS',','),
    'skip_healthcheck':('FIX_HC_SKIP',' '), 'update_skip_images':('UPDATE_SKIP_IMAGES',' '),
    'domain_blacklist':('DOMAIN_BLACKLIST',' '),
    # not currently read by code, but carried through for completeness:
    'never_sleep':('NEVER_SLEEP',' '), 'never_rename':('FIX_NEVER_RENAME',' '),
    'update_registries':('UPDATE_REGISTRIES',' '), 'stack_order':('STACK_ORDER',' '),
    'health_check_domains':('HEALTH_CHECK_DOMAINS',' '),
    'ip_whitelist':('IP_WHITELIST',','), 'proxy_skip':('PROXY_SKIP_CONTAINERS',' '),
    'scale_skip':('SCALE_SKIP_CONTAINERS',' '),
}

def _scalar(v):
    if isinstance(v, bool):  return '1' if v else '0'
    return str(v)

def _from_conf():
    cfg = {}
    if os.path.isfile(CONF_PATH):
        for line in open(CONF_PATH, encoding='utf-8', errors='replace'):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, val = line.split('=', 1)
            cfg[k.strip()] = val.strip()
    return cfg

def load():
    """Return internal-key config dict from stacks.yaml (fallback: stacks.conf)."""
    if not os.path.isfile(YAML_PATH):
        return _from_conf()
    try:
        import yaml
        y = yaml.safe_load(open(YAML_PATH, encoding='utf-8')) or {}
    except Exception:
        return _from_conf()
    cfg = {}
    for fk, val in y.items():
        if fk in SCALAR_MAP:
            cfg[SCALAR_MAP[fk]] = _scalar(val)
        elif fk in LIST_MAP:
            key, join = LIST_MAP[fk]
            items = val if isinstance(val, list) else [val]
            cfg[key] = join.join(str(x) for x in items)
    return cfg

def load_named(name):
    """Load any sibling <name>.yaml from the config dir as {KEY: 'string'}.
    Used for the other configs (global_inject, etc.) that keep their own keys.
    Returns {} if the YAML is missing/unreadable so callers keep their .conf."""
    p = os.path.join(CONF_DIR, name + ".yaml")
    if not os.path.isfile(p):
        return {}
    try:
        import yaml
        y = yaml.safe_load(open(p, encoding='utf-8')) or {}
    except Exception:
        return {}
    out = {}
    for k, v in y.items():
        if isinstance(v, list):
            out[k] = ' '.join(str(x) for x in v)
        else:
            out[k] = _scalar(v)
    return out

def load_doc(base):
    """Load a structured config that prefers <base>.yaml, falling back to the
    legacy <base>.conf (JSON). Used for build/menu configs. Returns {} if neither."""
    y = os.path.join(CONF_DIR, base + ".yaml")
    if os.path.isfile(y):
        try:
            import yaml
            return yaml.safe_load(open(y, encoding='utf-8')) or {}
        except Exception:
            pass
    c = os.path.join(CONF_DIR, base + ".conf")
    if os.path.isfile(c):
        try:
            import json
            return json.load(open(c, encoding='utf-8'))
        except Exception:
            return {}
    return {}


# ── stacks.yaml editing (comment-preserving, top-level keys) ──────────────────
def yaml_set_scalar(key, value):
    """Set/replace a top-level `key: value` scalar in stacks.yaml. Keeps comments."""
    import re
    if not os.path.isfile(YAML_PATH): return False
    lines = open(YAML_PATH, encoding="utf-8").read().split("\n")
    pat = re.compile(r"^" + re.escape(key) + r":\s")
    for i, l in enumerate(lines):
        if pat.match(l):
            lines[i] = f"{key}: {value}"
            open(YAML_PATH, "w", encoding="utf-8").write("\n".join(lines)); return True
    lines.append(f"{key}: {value}")
    open(YAML_PATH, "w", encoding="utf-8").write("\n".join(lines)); return True

def yaml_get_list(key):
    """Return the list items under a top-level `key:` block in stacks.yaml."""
    import re
    if not os.path.isfile(YAML_PATH): return []
    out, inblk = [], False
    for l in open(YAML_PATH, encoding="utf-8").read().split("\n"):
        if re.match(r"^" + re.escape(key) + r":\s*(\[\s*\])?\s*$", l):
            inblk = True; continue
        if inblk:
            m = re.match(r"^\s+-\s+(.*\S)\s*$", l)
            if m: out.append(m.group(1).strip().strip('"').strip("'"))
            elif re.match(r"^\s*#", l): continue
            else: break
    return out

def yaml_set_list(key, items):
    """Replace the list under top-level `key:` with items. Keeps surrounding comments."""
    import re
    if not os.path.isfile(YAML_PATH): return False
    lines = open(YAML_PATH, encoding="utf-8").read().split("\n")
    out, i, n, done = [], 0, len(lines), False
    while i < n:
        l = lines[i]
        if not done and re.match(r"^" + re.escape(key) + r":\s*(\[\s*\])?\s*$", l):
            if items:
                out.append(f"{key}:"); out += [f"  - {it}" for it in items]
            else:
                out.append(f"{key}: []")
            i += 1
            while i < n and re.match(r"^\s+-\s", lines[i]): i += 1
            done = True; continue
        out.append(l); i += 1
    if not done:
        out.append(f"{key}:" if items else f"{key}: []"); out += [f"  - {it}" for it in items]
    open(YAML_PATH, "w", encoding="utf-8").write("\n".join(out)); return True

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else '--env'
    cfg = load()
    if mode == '--env':
        for k, v in cfg.items():
            print(f"export {k}={shlex.quote(v)}")
    elif mode == '--check':
        conf = _from_conf()
        shared = sorted(set(cfg) & set(conf))
        bad = [(k, cfg[k], conf[k]) for k in shared if cfg[k] != conf[k]]
        print(f"keys from YAML: {len(cfg)} | shared with stacks.conf: {len(shared)} | mismatches: {len(bad)}")
        for k, a, b in bad:
            print(f"  MISMATCH {k}: yaml='{a}'  conf='{b}'")
        only_conf = sorted(set(conf) - set(cfg))
        if only_conf:
            print(f"\nkeys ONLY in stacks.conf (not produced by YAML): {len(only_conf)}")
            print("  " + ", ".join(only_conf))

if __name__ == '__main__':
    main()
