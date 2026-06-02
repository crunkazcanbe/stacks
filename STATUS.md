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
- AI cannot see live files; all edits via pasted terminal commands (this is the workflow, keep it)
