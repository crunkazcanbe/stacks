#!/usr/bin/env python3
"""
stacks_updates.py — Image update tracker
Checks if running container images have newer versions available
"""
import os, re, glob, json, urllib.request, urllib.parse, time

STACKS_DIR   = "/srv/stacks/Stacks"
CONF_FILE    = os.path.expanduser("~/.config/stacks/stacks.conf")
CACHE_FILE   = os.path.expanduser("~/.config/stacks/update_cache.json")
HISTORY_FILE = os.path.expanduser("~/.config/stacks/update_history.json")
HISTORY_MAX  = 500
UA = "Mozilla/5.0 (stacks-updater/1.0)"
TIMEOUT = 10

def load_conf():
    cfg = {
        "UPDATE_CHECK_ENABLED": "1",
        "UPDATE_CHECK_INTERVAL": "24",
        "UPDATE_CHECK_RUNNING_ONLY": "1",
        "UPDATE_AUTO_PULL": "0",
        "UPDATE_SKIP_IMAGES": "",
    }
    try:
        for line in open(CONF_FILE):
            l = line.strip()
            if "=" in l and not l.startswith("#"):
                k, v = l.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    except: pass
    try:
        import sys as _s; _s.path.insert(0, '/usr/local/lib'); import stacks_config as _sc
        cfg.update(_sc.load())   # YAML master overlay (stacks.yaml wins)
    except Exception: pass
    return cfg

def load_cache():
    try: return json.load(open(CACHE_FILE))
    except: return {}

def save_cache(cache):
    try: json.dump(cache, open(CACHE_FILE, "w"), indent=2)
    except: pass

def load_history():
    try: return json.load(open(HISTORY_FILE))
    except: return []

def save_history(hist):
    try: json.dump(hist[-HISTORY_MAX:], open(HISTORY_FILE, "w"), indent=2)
    except: pass

def _short(d):
    """Short form of a sha256:... digest for display."""
    if not d: return "—"
    if ":" in d: d = d.split(":", 1)[1]
    return d[:12]

def record_history(hist, event, image, tag, stacks, old, new):
    """Append a history record (newest appended to end)."""
    hist.append({
        "ts":     int(time.time()),
        "event":  event,            # "published" (remote changed) | "pulled" (local changed)
        "image":  image,
        "tag":    tag,
        "stacks": stacks,
        "old":    old or "",
        "new":    new or "",
        "old_short": _short(old),
        "new_short": _short(new),
    })

def get_history(limit=None):
    """Return history newest-first."""
    hist = load_history()
    hist = sorted(hist, key=lambda r: r.get("ts", 0), reverse=True)
    return hist[:limit] if limit else hist

def get_all_images():
    """Get all images from compose files."""
    images = {}  # {image: [(stack, service)]}
    for fpath in sorted(glob.glob(f"{STACKS_DIR}/*.yml")):
        stack = os.path.basename(fpath).replace(".yml","")
        try:
            content = open(fpath).read()
            for m in re.finditer(r'image:\s*([^\s\n]+)', content):
                img = m.group(1).strip().strip("'\"")
                if img and not img.startswith("#"):
                    if img not in images:
                        images[img] = []
                    images[img].append(stack)
        except: pass
    return images

def parse_image(image):
    """Parse image into registry, repo, tag."""
    tag = "latest"
    if ":" in image.split("/")[-1]:
        parts = image.rsplit(":", 1)
        image = parts[0]
        tag = parts[1]

    if "/" not in image:
        return "docker.io", f"library/{image}", tag
    parts = image.split("/")
    if "." in parts[0] or ":" in parts[0]:
        registry = parts[0]
        repo = "/".join(parts[1:])
    else:
        registry = "docker.io"
        repo = image
    return registry, repo, tag

def check_dockerhub(repo, current_tag):
    """Check Docker Hub for latest digest."""
    try:
        # Get token
        auth_url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
        req = urllib.request.Request(auth_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            token = json.loads(r.read())["token"]

        # Get manifest digest for current tag
        man_url = f"https://registry-1.docker.io/v2/{repo}/manifests/{current_tag}"
        req2 = urllib.request.Request(man_url, headers={
            "User-Agent": UA,
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.docker.distribution.manifest.v2+json"
        })
        with urllib.request.urlopen(req2, timeout=TIMEOUT) as r:
            remote_digest = r.headers.get("Docker-Content-Digest","")

        return {"digest": remote_digest, "checked": int(time.time())}
    except Exception as e:
        return {"error": str(e)[:50], "checked": int(time.time())}

def check_ghcr(repo, tag):
    """Check GitHub Container Registry."""
    try:
        url = f"https://ghcr.io/v2/{repo}/manifests/{tag}"
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/vnd.oci.image.manifest.v1+json"
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            digest = r.headers.get("Docker-Content-Digest","")
        return {"digest": digest, "checked": int(time.time())}
    except Exception as e:
        return {"error": str(e)[:50], "checked": int(time.time())}

def get_local_digest(image):
    """Get local image digest via docker inspect."""
    try:
        import subprocess
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            digest = r.stdout.strip()
            if "@" in digest:
                return digest.split("@")[1]
    except: pass
    return None

def check_updates(force=False):
    """Check all images for updates. Returns list of update info."""
    cfg = load_conf()
    if cfg["UPDATE_CHECK_ENABLED"] != "1":
        return []

    cache = load_cache()
    hist  = load_history()
    hist_dirty = False
    interval = int(cfg.get("UPDATE_CHECK_INTERVAL","24")) * 3600
    skip = set(s.strip() for s in cfg["UPDATE_SKIP_IMAGES"].split(",") if s.strip())
    images = get_all_images()
    results = []

    for image, stacks in images.items():
        if image in skip: continue
        if any(s in image for s in skip): continue

        # Check cache age
        cached = cache.get(image, {})
        age = int(time.time()) - cached.get("checked", 0)
        if not force and age < interval and "remote_digest" in cached:
            results.append({**cached, "image": image, "stacks": stacks})
            continue

        registry, repo, tag = parse_image(image)
        local_digest = get_local_digest(image)

        # Check remote
        if registry == "docker.io":
            remote = check_dockerhub(repo, tag)
        elif "ghcr.io" in registry:
            remote = check_ghcr(repo, tag)
        else:
            remote = {"error": "unsupported registry"}

        remote_digest = remote.get("digest","")
        has_update = (
            bool(local_digest) and
            bool(remote_digest) and
            local_digest != remote_digest
        )

        entry = {
            "image": image,
            "tag": tag,
            "stacks": stacks,
            "local_digest": local_digest or "",
            "remote_digest": remote_digest,
            "has_update": has_update,
            "checked": int(time.time()),
            "error": remote.get("error",""),
        }

        # ── record history on any digest change vs the last cached entry ──
        prev = cached
        prev_remote = prev.get("remote_digest", "")
        prev_local  = prev.get("local_digest", "")
        if remote_digest and prev_remote and remote_digest != prev_remote:
            record_history(hist, "published", image, tag, stacks, prev_remote, remote_digest)
            hist_dirty = True
        if local_digest and prev_local and local_digest != prev_local:
            record_history(hist, "pulled", image, tag, stacks, prev_local, local_digest)
            hist_dirty = True

        cache[image] = entry
        results.append(entry)

    save_cache(cache)
    if hist_dirty:
        save_history(hist)
    return results

def pull_updates():
    """Pull every image that currently has an update available, recording history."""
    import subprocess
    cache = load_cache()
    targets = [v for v in cache.values() if isinstance(v, dict) and v.get("has_update")]
    if not targets:
        print("No updates to pull.")
        return
    print(f"Pulling {len(targets)} image(s)...\n")
    for r in targets:
        img = r.get("image", "")
        print(f"⬇ docker pull {img}")
        try:
            subprocess.run(["docker", "pull", img], timeout=600)
        except Exception as e:
            print(f"  pull failed: {e}")
    print("\nRe-checking digests to update history...")
    check_updates(force=True)

def show_history(limit=40):
    hist = get_history(limit)
    if not hist:
        print("No update history yet.")
        return
    print(f"\nUpdate history (newest {len(hist)}):\n")
    for r in hist:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("ts", 0)))
        ev = r.get("event", "")
        arrow = "⬆" if ev == "published" else "⬇"
        print(f"  {when}  {arrow} {ev:<9} {r.get('image',''):<44} "
              f"{r.get('old_short','—')} → {r.get('new_short','—')}")

if __name__ == "__main__":
    import sys
    if "--history" in sys.argv:
        show_history()
        sys.exit(0)
    if "--pull" in sys.argv:
        pull_updates()
        sys.exit(0)
    force = "--force" in sys.argv
    print("Checking for image updates...")
    results = check_updates(force=force)
    updates = [r for r in results if r.get("has_update")]
    errors  = [r for r in results if r.get("error")]
    ok      = [r for r in results if not r.get("has_update") and not r.get("error")]
    print(f"\n✔ Up to date:  {len(ok)}")
    print(f"⬆ Updates:     {len(updates)}")
    print(f"✘ Errors:      {len(errors)}")
    if updates:
        print("\nUpdates available:")
        for r in updates:
            print(f"  {r['image']:<50} stacks: {', '.join(r['stacks'])}")
    hist = get_history(8)
    if hist:
        print("\nRecent changes:")
        for r in hist:
            when = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
            arrow = "⬆" if r.get("event") == "published" else "⬇"
            print(f"  {when} {arrow} {r.get('image',''):<44} {r.get('old_short','—')} → {r.get('new_short','—')}")
