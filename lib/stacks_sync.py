#!/usr/bin/env python3
"""
stacks_sync.py — Sync descriptions and all_services.txt from compose files
Run automatically after stacks up/down/build or manually
"""
import os, re, glob

STACKS_DIR = "/srv/stacks/Stacks"
CONF_DIR   = os.path.expanduser("~/.config/stacks")
DESC_DIR   = os.path.join(CONF_DIR, "descriptions")
SVC_FILE   = os.path.join(CONF_DIR, "all_services.txt")

def get_default_desc():
    try:
        import sys as _s; _s.path.insert(0, '/usr/local/lib'); import stacks_config as _sc
        v = _sc.load().get("BUILD_DEFAULT_DESC")
        if v: return v
    except Exception: pass
    try:
        for line in open(os.path.join(CONF_DIR, "stacks.conf")):
            l = line.strip()
            if l.startswith("BUILD_DEFAULT_DESC="):
                return l.split("=",1)[1].strip('" ')
    except: pass
    return "A powerful service running on StacksServer. Edit this description."

def parse_stack(fpath):
    """Get all services and images from a compose file."""
    services = []
    try:
        content = open(fpath).read()
        in_services = False
        current_svc = None
        current_img = ""
        for line in content.split("\n"):
            if line.strip() == "services:":
                in_services = True; continue
            if in_services and re.match(r"^(networks|volumes|configs|secrets):", line):
                in_services = False; continue
            if in_services and re.match(r"^  [a-zA-Z0-9_-]+:\s*$", line):
                if current_svc: services.append((current_svc, current_img))
                current_svc = line.strip().rstrip(":")
                current_img = ""
            if in_services and current_svc and "image:" in line:
                current_img = line.split("image:",1)[1].strip().strip("'\"")
            if in_services and current_svc and "container_name:" in line:
                current_svc = line.split("container_name:",1)[1].strip()
        if current_svc: services.append((current_svc, current_img))
    except: pass
    return services

def sync_descriptions(stack_name, services, default_desc):
    """Add missing services, remove deleted ones from descriptions file."""
    os.makedirs(DESC_DIR, exist_ok=True)
    desc_file = os.path.join(DESC_DIR, f"{stack_name}.conf")
    try: existing = open(desc_file).read()
    except: existing = f"# {stack_name} — Service Descriptions\n# Edit the description under each service name.\n#\n"

    # Build set of valid service names (normalize dash/underscore)
    valid = set()
    for svc, img in services:
        valid.add(svc)
        valid.add(svc.replace("-","_"))
        valid.add(svc.replace("_","-"))

    # Parse existing file into blocks
    # Header = lines before first service entry
    # Each block = service name line + following # lines
    header_lines = []
    blocks = {}  # {svc_name: [lines]}
    current_svc = None
    in_header = True

    for line in existing.split("\n"):
        stripped = line.strip()
        # Check if this line is a bare service name (no # prefix, not empty, not yaml key)
        if stripped and not stripped.startswith("#") and not ":" in stripped and not stripped.startswith("-"):
            in_header = False
            current_svc = stripped
            blocks[current_svc] = []
        elif in_header:
            header_lines.append(line)
        elif current_svc is not None:
            blocks[current_svc].append(line)

    # Rebuild: keep header, keep valid services, add missing ones
    added = removed = 0
    result = "\n".join(header_lines).rstrip("\n")

    for svc_name, svc_lines in blocks.items():
        if svc_name in valid:
            result += f"\n\n{svc_name}\n" + "\n".join(svc_lines).strip("\n")
        else:
            removed += 1

    # Add missing services
    for svc, img in services:
        svc_norm = svc.replace("-","_")
        if svc not in blocks and svc_norm not in blocks and svc.replace("_","-") not in blocks:
            result += f"\n\n{svc}\n# {default_desc}"
            added += 1

    result = result.strip("\n") + "\n"

    if added or removed:
        open(desc_file, "w").write(result)

    return added, removed

def sync_all_services(stack_name, services):
    """Update all_services.txt - add new, remove deleted."""
    try: existing = open(SVC_FILE).read()
    except: existing = "# ALL SERVICES — StacksServer\n# Format: stack | service | image\n# =========================================\n"

    valid_names = {svc for svc, img in services}
    section = f"# ── {stack_name.upper()}"
    lines = existing.split("\n")
    new_lines = []
    added = removed = 0

    for line in lines:
        # Check if this is a service line for this stack
        if line.startswith(stack_name) and "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                svc = parts[1].strip()
                if svc in valid_names:
                    new_lines.append(line)
                else:
                    removed += 1
                    continue
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    existing = "\n".join(new_lines)

    # Add missing
    for svc, img in services:
        if f"| {svc} " not in existing and f"| {svc}\n" not in existing and f"| {svc}" not in existing:
            entry = f"{stack_name:<12} | {svc:<35} | {img}"
            if section in existing:
                lines2 = existing.split("\n")
                for i, l in enumerate(lines2):
                    if l.startswith(section):
                        lines2.insert(i+1, entry)
                        break
                existing = "\n".join(lines2)
            else:
                existing += f"\n{section} ──────────────────────────────────────\n{entry}\n"
            added += 1

    open(SVC_FILE, "w").write(existing)
    return added

def main():
    default_desc = get_default_desc()
    total_desc = 0
    total_svc = 0
    
    for fpath in sorted(glob.glob(f"{STACKS_DIR}/*.yml")):
        stack_name = os.path.basename(fpath).replace(".yml","")
        services = parse_stack(fpath)
        if not services: continue
        added_d, removed_d = sync_descriptions(stack_name, services, default_desc)
        added_s = sync_all_services(stack_name, services)
        total_desc += added_d + removed_d
        total_svc  += added_s

    if total_desc or total_svc:
        print(f"Sync complete: descriptions updated, all_services updated")

if __name__ == "__main__":
    main()
