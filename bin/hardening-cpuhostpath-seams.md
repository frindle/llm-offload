# CPU-lane host-path seams (2026-10-14)

Seam: cpu_stage.eligibility() host-path scan vs the scaffold's sub-package env-parity block.
- S1: verify.sh containing the `# --- sub-package node_modules (env parity) ---` block (absolute source path, WARN else) is
  runner-eligible iff no file reachable from verify.test.* (relative require/import graph) imports a bare package declared by
  the sub-package's package.json and absent from the root lockfile. Unresolvable edge / unreadable file / >400 files => local.
- S2: a host path anywhere OUTSIDE the block still forces local (fixture sp_other in test-cpu-stage.py section 14).
- S3: kill switches CPU_LANE_SUBPKG_GUARD=0 (restores old verdict), DISPATCH_CPU_LINT=0 (silences scaffold warning).
- Canary seam to add (canary not edited): a scaffold-emitted verify.sh for a sub-package target whose test imports a
  sub-package-only dep must report local; one that does not must report eligible.
- Known residual: the runner installs only the ROOT lockfile; sub-package-dep tests stay local until the runner dep phase
  installs sub-package lockfiles (runner image change, not done).
