#!/usr/bin/env python3
import sys, os, re

action   = sys.argv[1]  # inject or strip
target   = sys.argv[2]  # all or specific file
dyn_dir  = sys.argv[3] if len(sys.argv) > 3 else "/srv/stacks/Configs/Dynamics"
conf_path = "~/.config/stacks/art.conf"

# Load art from art.conf
header_art = ""
footer_art = ""
if os.path.exists(conf_path):
    conf = open(conf_path).read()
    for var, key in [("_ba_header", "header"), ("_ba_footer", "footer")]:
        sm = f"##STACKS_ART_START_{key.upper()}"
        em = f"##STACKS_ART_END_{key.upper()}"
        if sm in conf and em in conf:
            if key == "header":
                header_art = conf.split(sm)[1].split(em)[0].strip("\n")
            else:
                footer_art = conf.split(sm)[1].split(em)[0].strip("\n")

def strip_file(path):
    lines = open(path).readlines()
    # Remove leading comment block
    start = 0
    for i, l in enumerate(lines):
        if not l.startswith("#") and l.strip() != "":
            start = i
            break
    # Remove trailing comment block
    end = len(lines)
    for i in range(len(lines)-1, -1, -1):
        if not lines[i].startswith("#") and lines[i].strip() != "":
            end = i + 1
            break
    result = lines[start:end]
    open(path, "w").writelines(result)

def inject_file(path):
    strip_file(path)
    content = open(path).read()
    result = ""
    if header_art:
        result += header_art + "\n"
    result += content
    if footer_art:
        result = result.rstrip("\n") + "\n" + footer_art + "\n"
    open(path, "w").write(result)

# Build file list
if target in ("all", "--all"):
    files = [os.path.join(dyn_dir, f) for f in os.listdir(dyn_dir)
             if f.endswith(".yml") or f.endswith(".yaml")]
elif os.path.isabs(target) and os.path.isfile(target):
    files = [target]
elif os.path.isfile(os.path.join(dyn_dir, target)):
    files = [os.path.join(dyn_dir, target)]
elif os.path.isfile(os.path.join(dyn_dir, target + ".yml")):
    files = [os.path.join(dyn_dir, target + ".yml")]
else:
    print(f"Not found: {target}", file=sys.stderr)
    sys.exit(1)

for f in files:
    if action == "strip":
        strip_file(f)
    else:
        inject_file(f)
    print(os.path.basename(f))
