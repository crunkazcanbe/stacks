#!/usr/bin/env python3
"""
stacks_repair.py — Deep compose file repair based on learned patterns from dev_1.yml
Fixes structural corruption, missing keys, bad indentation, and injection artifacts.
Called by stacks fix as Phase 0.5 corruption repair.
"""
import re, os, sys

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

    if not dry_run and content != original:
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
