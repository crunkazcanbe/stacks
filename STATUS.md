# stacks - Capability & Wiring Status
_Last updated: 2026-06-02_

## PHILOSOPHY (do not blur these two commands)
- FIX = applies MY preferences from the config files. Shapes the compose the way I want it
        (enrichment, networks, depends_on policy). Opinionated, intentional.
- REPAIR = does whatever it takes to make the stack START, without changing what fix decided.
        Fixes breakage: bad indentation, YAML errors, missing/corrupt chunks. When a piece is
        broken or missing, pulls that exact piece from a known-good snapshot (taken during a
        stable startup) and injects it back. Preserves fix's choices; only restores what broke.

## FIX engine - /usr/local/lib/stacks_fix.py  (wrapper line ~1064)
### FIX_* flags (stacks.conf)
healthchecks (FIX_HEALTHCHECKS/DEEP_INSPECT/REPLACE_BROKEN_HC/FORCE_HC/HC_SKIP),
net+vol (FIX_DEFINE_NETVOL/AUTO_NETWORKS/AUTO_LINK_NETWORKS/EXTERNAL|LOCAL|INLINE),
volumes (AUTO_BIND_MOUNTS/AUTO_NAMED_VOLUMES/CONVERT_NAMED_TO_BIND),
depends_on (FIX_AUTO_DEPENDS=1 inject / FIX_REMOVE_DEPENDS=1 strip all / FIX_FORCE_DEPENDS=1 redo),
FIX_HEAL_TYPOS / FIX_STRIP_PROFILES / FIX_REMOVE_GAPS / FIX_BACKUP
### INJECT_* enrichment (global_inject.conf) - master INJECT_FILL_ALL=1
common_caps, hostname, storage_opt, deploy(+DEPLOY_PLACEMENT_CONSTRAINT), blkio, ulimits,
mac, labels(traefik.enable/sablier.enable/sablier.group=prefix), stop_grace, logging, restart, cpuset

## REPAIR engine - /usr/local/lib/stacks_repair.py
repair_file passes in order: corrupt_blkio, labels_in_networks, duplicate_labels,
missing_closing_quotes, n_labels, name_field, duplicate_service_keys, undefined_depends,
dependency_cycles, undefined_networks. Backs up before write.
Snapshots: snapshot_after_up (wrapper ~1207) saves .good only if valid + nothing failed,
right after deploy before Sablier sleeps. SNAPSHOT_DIR/KEEP=5/REQUIRE=none-failed/SETTLE=15.

## WIRING STATUS
- [x] fix -> stacks_fix.py  WORKING
- [x] snapshot_after_up -> wired  WORKING
- [x] fix depends_on removal: FIX_REMOVE_DEPENDS=1 + gate patched  WORKING
- [ ] repair -> repair_file  NOT WIRED (up repair runs old self-heal only)
- [ ] depends_on same-file-only guard for INJECT  NOT BUILT
- [ ] snapshot-restore (pull broken chunk from .good)  DESIGNED, NOT BUILT
- [ ] gerbil_net bug (fix adds wrong 3rd/foreign net)
- [ ] watchdog escalation down->up x3->up repair  NOT BUILT
- [ ] build wizard duplicate-name warning  NOT BUILT
- [ ] healthcheck registry  IDEA

## CONSTRAINTS
- compose 5.1.4 (upgraded 2026-05-21) rejects cross-file depends_on -> only pin same-file families
- AI cannot see live files; all edits via pasted terminal commands (this is the workflow, keep it)## NEXT SESSION — build the PER-SERVICE REPAIR LOOP (repair command)
Josie's exact algorithm:
1. YAML-check the file. If NOT valid -> fix the invalid parts (indentation, etc).
2. For EACH service: bring up JUST that one service, test it starts.
3. If it starts fine -> restart Sablier (so the service goes back to sleep).
4. Put that service back in the compose file -> validate the WHOLE file again.
5. If valid -> that service is fixed. If not -> continue service by service,
   restarting Sablier after each, until the whole stack is fixed and valid.
Divide-and-conquer: prove each service alone, Sablier reset between each, then prove whole.
Known-good source = snapshot system (snapshot_after_up, already built).

CORE PRINCIPLE (Josie): SURGICAL repair only. Replace ONLY the broken piece
(missing char, bad indent, corrupt chunk) from the snapshot — never the whole
service or stack. MUST preserve anything Josie added since the snapshot.
- Broken piece + snapshot exists -> pull just that fragment from snapshot, keep the rest.
- No snapshot -> validate YAML + structural passes, do everything possible to start it.
- NEVER strip user additions. Diff-and-patch the broken region, not wholesale replace.



## SESSION 2026-06-02 (evening) — repair_loop BUILT
- repair_loop(path): error-driven surgical repair. Runs compose config, reads ONE error,
  classifies (mixed-form/dup/undefined-net/undefined-depends/cycle/indent), fixes that piece
  in place, re-validates, loops (max 25), stops if stuck. Backs up .prerepair each write.
  NEVER reverts whole file, NEVER deletes user additions. TESTED: fixed 3 error types in 3 passes.
- fix_network_form pass: list/mixed networks -> mapping form (traefik 1000, others 500) + dedupe.
- Wired into repair command: repair = repair_file (structural) THEN repair_loop (error-driven).
- Confirmed: fix-before-repair order is bulletproof (wrapper runs fix block then repair block by position).
- core_0 fixed live (was mixed list/mapping networks from compose 5.1.4 strictness).

## repair_loop — FIXERS TO ADD NEXT (onto the working loop)
- spelling/typo fixer (wrong key names, image typos)
- truncated-line detector (a line that got cut off mid-paste)
- name fixer (wrong container/service name)
- snapshot-piece-pull: LAST RESORT, pull just the broken fragment from .good snapshot (not whole file)
- per-service up-test-sablier-reassemble loop (Josie's divide-and-conquer)
- container name-conflict auto-recovery (the kestra "already in use" error -> rm + retry)

## SESSION 2026-06-02 (evening) — repair_loop BUILT
- repair_loop(path): error-driven surgical repair. Runs compose config, reads ONE error,
  classifies (mixed-form/dup/undefined-net/undefined-depends/cycle/indent), fixes that piece
  in place, re-validates, loops (max 25), stops if stuck. Backs up .prerepair each write.
  NEVER reverts whole file, NEVER deletes user additions. TESTED: fixed 3 error types in 3 passes.
- fix_network_form pass: list/mixed networks -> mapping form (traefik 1000, others 500) + dedupe.
- Wired into repair command: repair = repair_file (structural) THEN repair_loop (error-driven).
- core_0 fixed live (mixed list/mapping networks from compose 5.1.4 strictness).

## repair_loop — FIXERS TO ADD NEXT
- container name-conflict auto-recovery (kestra "already in use" -> rm + retry)
- truncated-line detector (line cut off mid-paste)
- spelling/typo + name fixer
- snapshot-piece-pull: LAST RESORT, pull just broken fragment from .good (not whole file)
- per-service up-test-sablier-reassemble loop (divide-and-conquer)

## NEXT FEATURE — CONTAINER AUTO-NAMING (config option, NOT yet built)
Goal: clean family-based container names. Head = bare family name; members = head_role.
  e.g. supabase-auth family -> supabase, supabase_db, supabase_redis, supabase_auth, supabase_meta
Network already uses <root>_net (supabase_net) — naming should match that root.
Config flag: FIX_AUTO_NAME_CONTAINERS (default OFF until proven).
CRITICAL — rename is high-blast-radius. Must update EVERY reference atomically:
  - container_name itself
  - depends_on entries pointing at old name (all files)
  - env vars / DB URLs / connection strings referencing old name
  - extra_hosts entries
  - Sablier + Traefik labels referencing old name
  - any cross-stack references
Build order: (1) compute new name per svc from family head+role, (2) build old->new map for
  ALL containers first, (3) do a global find/replace of references across all 30 files using the map,
  (4) test on copies, confirm depends/connections still resolve, (5) flag-gate, default off.
Do NOT ship without testing inter-service connections survive the rename.

## RENAME ENGINE — BUILT, do NOT run live until dynamics synced
- build_rename_map / rename_report / apply_renames in stacks_fix.py. 232 renames, NO collisions, db_2 test validates.
- Flag FIX_AUTO_NAME_CONTAINERS (code default 0, Josie's conf =1).
- Rule: loner=drop dashes to one word; family=root_role; gerbil/pangolin-client excluded.
- CRITICAL BLOCKER: renaming containers WITHOUT updating Traefik dynamic configs breaks routing.
  Dynamics at /srv/stacks/Configs/Dynamics/ reference containers by name (http://name:port).
- NEXT: build FIX_SYNC_DYNAMICS_NAMES — apply the same old->new rename map across all dynamic config files.
  Then apply_renames must do BOTH stacks + dynamics together. Only run live after both wired.

## DYNAMIC COMMAND GRAMMAR (build next, after fixing rename)
Josie's spec:
  stacks dynamics fix                         -> fix ALL dynamic files
  stacks dynamics <name> fix                  -> fix ONE dynamic
  stacks up dynamic repair recreate fix info  -> all stacks up (repair/recreate/fix) + fix ALL dynamics, show logs
  stacks up <stack> <stack> dynamic <dyn> repair recreate fix -> only listed stacks get repair/recreate/fix; only listed dynamic(s) get fixed
Rules:
  - 'dynamic'/'dynamics' keyword activates dynamic processing; names after = which (or all if none).
  - For NOW: fix on a dynamic = ONLY the renaming (per config). NOT automatic in regular stack fix.
  - Josie wants MANUAL control of dynamics (like manual control of stacks) - regular fix must NOT auto-rename dynamics.
  - Word order irrelevant (like stacks).
  - LATER: recreate/rebuild dynamics, auto-add middleware/services with fix.

## RENAME ENGINE — KNOWN BUG to fix first
apply_renames does BLIND global text replace -> mangles volume names like
  'oauth2-proxy-postgres_data' (container 'oauth2-proxy-postgres' + '_data').
FIX: make rename TARGETED, not blind. Only rewrite: container_name, service-key line,
  hostname, depends_on entries, sablier names=/group, env URLs (@X: //X: ('X').
  NEVER touch volume mounts (- X_data:/path) or network/volume declaration names.
Test (depends stripped + rename) got 27 stacks -> only 2 errors, both oauth2 volume/net mangling.
Config flags exist: FIX_AUTO_NAME_CONTAINERS=1, FIX_SYNC_DYNAMICS_NAMES=1, FIX_RENAME_IGNORE (+ built-in gerbil/pangolin-client/ak-outpost-traefik). All code-default OFF.

## CONFIGURABLE PATHS (already exist, verify universal)
STACKS_DIR + STACKS_DIR_OVERRIDE, DYNAMICS_DIR + DYNAMICS_DIR_OVERRIDE in conf.
Some hardcoded paths remain (stacks lines ~1436,1474,2362) - should use $DYNAMICS_DIR var.

## PROVISIONER-GENERATOR + TWO-WAY VOLUME MANAGER (build next, config-gated)
Networks/volumes mode controlled by EXTERNAL vs INTERNAL toggle:
  EXTERNAL on  -> fix auto-creates a provisioner container in a DEDICATED stack,
                  adds all networks + named volumes to it (provisioner creates them),
                  everything else references them as external.
  INTERNAL on  -> fix adds all networks/volumes DIRECTLY into the current stack,
                  full inline settings: subnet, gateway, driver: bridge, attachable,
                  enable_ipv6: false, labels [com.stacks.network=X, env=production], ipam config.
Two-way VOLUME manager:
  BIND mode  -> all volumes = bind mounts at FIX_VOLUME_BASE/<name> (DONE; FIX_FORCE_VOLUME_BASE keeps them there).
  NAMED mode -> fix converts binds -> named volumes, declares them external, adds to provisioner that creates them.
  Switching back to BIND -> fix converts named -> bind, fills all out under FIX_VOLUME_BASE.
  When external+named: find smallest (kb-wise) and put it in [Josie's note - clarify on build].
Existing pieces: convert_named_to_bind (DONE), FIX_FORCE_VOLUME_BASE (flag registered, behavior TODO),
  FIX_VOLUME_BASE=/srv/stacks/docker. Need: convert_bind_to_named, provisioner-injector, internal-inline-injector.
Network subnet pattern observed: 10.50.X.0/24 gateway 10.50.X.1, incrementing X.

## TONIGHT'S DEPLOY STATE (2026-06-02 late)
Live rename applied to ALL 30 stacks + 29 dynamics. All 30 validate. Deployed via 'stacks up ... repair recreate info'.
~30 running, rest Sablier-asleep (normal). proxmox fully removed. depends stripped. volumes normalized to stacks (377 paths).
4 dead images rehosted to ghcr.io/crunkazcanbe (openclaw-operator, zot, agent-of-empires, dweebui) + stack refs swapped.
Remaining: a few more dead images to rehost (use /home/user/rehost-image.sh if created, or build manually).
