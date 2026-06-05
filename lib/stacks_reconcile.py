#!/usr/bin/env python3
"""stacks_reconcile.py — container-state reconcile for `stacks ... repair`.

For a given stack compose file, brings container state in line with what the
compose DEFINES, healing the failure modes that leave a stack half-up:
  1. Remove orphan hash-prefixed duplicate containers (<12hex>_<name>) for this
     stack's services — they block the clean name and abort `compose up`.
  2. Start any of this stack's containers stuck in 'created' (never started).
  3. Create any defined-but-missing service, one service at a time
     (`docker compose up -d --no-deps <key>`), so one bad service can't abort
     the rest of the stack.

Per-service so a single failure never blocks the others. Prints each action and
returns a one-line summary. Safe to run repeatedly (idempotent).
"""
import sys, os, re, subprocess


def _env():
    e = dict(os.environ)
    e.setdefault("DOCKER_HOST", "unix:///var/run/docker.sock")
    return e


def _sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, env=_env(), **kw)


def parse_services(stack_file):
    """Return {container_name: service_key} from a compose file."""
    defn, key = {}, None
    for line in open(stack_file, encoding="utf-8", errors="replace"):
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)        # service key (2-space indent)
        if m:
            key = m.group(1)
            continue
        cm = re.match(r'^\s+container_name:\s*"?([A-Za-z0-9_.-]+)', line)
        if cm and key:
            defn[cm.group(1)] = key
    return defn


def _states():
    """Return {container_name: state}."""
    out = {}
    r = _sh("docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}")
    for line in r.stdout.splitlines():
        if "\t" in line:
            n, s = line.split("\t", 1)
            out[n] = s
    return out


def reconcile(stack_file):
    if not os.path.isfile(stack_file):
        return "reconcile: no such stack file"
    defn = parse_services(stack_file)
    names = set(defn)
    if not names:
        return "reconcile: no services defined"
    states = _states()
    cwd = os.path.dirname(stack_file)
    actions = []

    # 1. remove orphan hash-prefixed duplicates for this stack's services
    for n in list(states):
        m = re.match(r"^[0-9a-f]{12}_(.+)$", n)
        if m and m.group(1) in names:
            if _sh("docker", "rm", "-f", n).returncode == 0:
                actions.append(f"removed orphan dup {n}")
                states.pop(n, None)

    # 2. start this stack's 'created' (never-started) containers
    for cname in names:
        if states.get(cname) == "created":
            ok = _sh("docker", "start", cname).returncode == 0
            actions.append(("started " if ok else "start-FAILED ") + cname)

    # 3. create defined-but-missing services, one at a time
    for cname, key in defn.items():
        if cname not in states:
            r = _sh("docker", "compose", "-f", stack_file, "up", "-d",
                    "--no-deps", key, cwd=cwd, timeout=300)
            if r.returncode == 0:
                actions.append(f"created {cname}")
            else:
                last = (r.stderr.strip().splitlines() or [""])[-1][:70]
                actions.append(f"create-FAILED {cname}: {last}")

    for a in actions:
        print("  " + a)
    return f"reconcile: {len(actions)} action(s)" if actions else "reconcile: already consistent"


if __name__ == "__main__":
    f = sys.argv[1] if len(sys.argv) > 1 else None
    print(reconcile(f) if f else "usage: stacks_reconcile.py <stack.yml>")
