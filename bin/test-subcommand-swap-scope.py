#!/usr/bin/env python3
"""Behavioural test: code-review-agent.subcommand_swaps fires ONLY on real shell
command swaps (2026-10-06 gate audit).

The live false-BLOCK class: the harness rule `harness-subcommand-swap` (medium,
category code -> verdict concerns + a re-gate) fired on SQL, Prisma, Dockerfile
and Markdown prose -- 42 findings in October, including the eGift s0 rename
round (`CREATE TABLE` -> `CREATE UNIQUE INDEX`, `model OrderEgmtLink` -> `model
OrderEgiftLink`). The true positives it exists for (code-review-bench
bug-scauth / bug-awscp) must still fire, and its known negative (ok-awsflag)
must still stay silent.

Usage: python3 test-subcommand-swap-scope.py [path/to/code-review-agent.py]
(pass a .bak to prove the test bites: it must FAIL against the pre-fix file).
"""
import importlib.util
import sys
from pathlib import Path

target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("code-review-agent.py"))
import importlib.machinery
_ld = importlib.machinery.SourceFileLoader("cra_under_test", str(target))
spec = importlib.util.spec_from_loader("cra_under_test", _ld)
cra = importlib.util.module_from_spec(spec)
_ld.exec_module(cra)

ok = True


def check(name, got, want):
    global ok
    good = got == want
    ok &= good
    print(("PASS " if good else "FAIL ") + name + ("" if good else f": got {got!r} want {want!r}"))


def swaps(diff):
    out = []
    for h in cra.parse_diff(diff):
        out += cra.subcommand_swaps(h)
    return out


# --- the live false positives (verbatim shapes from 01fdda651fba.diff etc.) ---
RENAME_SQL = """diff --git a/prisma/migrations/x/migration.sql b/prisma/migrations/x/migration.sql
--- a/prisma/migrations/x/migration.sql
+++ b/prisma/migrations/x/migration.sql
@@ -1,13 +1,13 @@
 -- CreateTable
-CREATE TABLE "OrderEgmtLink" (
+CREATE TABLE "OrderEgiftLink" (
     "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
     "orderId" INTEGER NOT NULL,
-    CONSTRAINT "OrderEgmtLink_orderId_fkey" FOREIGN KEY ("orderId") REFERENCES "Order" ("id")
+    CONSTRAINT "OrderEgiftLink_orderId_fkey" FOREIGN KEY ("orderId") REFERENCES "Order" ("id")
 );
 -- CreateIndex
-CREATE UNIQUE INDEX "OrderEgmtLink_orderId_key" ON "OrderEgmtLink"("orderId");
+CREATE UNIQUE INDEX "OrderEgiftLink_orderId_key" ON "OrderEgiftLink"("orderId");
"""
check("SQL rename: no subcommand swap", swaps(RENAME_SQL), [])

RENAME_PRISMA = """diff --git a/prisma/schema.prisma b/prisma/schema.prisma
--- a/prisma/schema.prisma
+++ b/prisma/schema.prisma
@@ -420,3 +420,3 @@
-model OrderEgmtLink {
+model OrderEgiftLink {
   id Int @id @default(autoincrement())
"""
check("Prisma identifier fix: no subcommand swap", swaps(RENAME_PRISMA), [])

DOCKER = """diff --git a/Dockerfile b/Dockerfile
--- a/Dockerfile
+++ b/Dockerfile
@@ -1,4 +1,4 @@
 FROM python:3.12-slim
-RUN useradd -m app
+RUN apt-get update && apt-get install -y supervisor
-COPY server.py /app/
+COPY server.py worker.py supervisord.conf /app/
"""
check("Dockerfile RUN of a different binary / COPY args: no swap", swaps(DOCKER), [])

PROSE = """diff --git a/TASK.md b/TASK.md
--- a/TASK.md
+++ b/TASK.md
@@ -3,2 +3,2 @@
-The gate holds the leftovers until the route is how the check runs.
+The TASK.md explicitly forbids the step-1 parent-drop in the same window.
"""
check("Markdown prose: no swap", swaps(PROSE), [])

UNRELATED_SH = """diff --git a/deploy.sh b/deploy.sh
--- a/deploy.sh
+++ b/deploy.sh
@@ -1,3 +1,3 @@
-git add -A
+git commit -qm "release $(date +%F)" --allow-empty --no-verify
"""
check("shell: unrelated lines sharing a binary are not paired", swaps(UNRELATED_SH), [])

# --- the true positives the rule exists for (code-review-bench fixtures) -------
SCAUTH = """diff --git a/base.sh b/base.sh
--- a/base.sh
+++ b/base.sh
@@ -10,2 +10,2 @@
-  sc_auth list -u "$(whoami)" 2>/dev/null | grep -c '.'
+  sc_auth identities -u "$(whoami)" 2>/dev/null | grep -c '.'
"""
check("bench bug-scauth still fires", swaps(SCAUTH), [("sc_auth", "list", "identities")])

AWSCP = """diff --git a/deploy.sh b/deploy.sh
--- a/deploy.sh
+++ b/deploy.sh
@@ -5,1 +5,1 @@
-  aws s3 sync "$BUILD_DIR" "$BUCKET" --delete
+  aws s3 cp "$BUILD_DIR" "$BUCKET" --recursive
"""
check("bench bug-awscp still fires", swaps(AWSCP), [("aws", "s3 sync", "s3 cp")])

AWSFLAG = AWSCP.replace('aws s3 cp "$BUILD_DIR" "$BUCKET" --recursive',
                        'aws s3 sync "$BUILD_DIR" "$BUCKET" --delete --only-show-errors')
check("bench ok-awsflag stays silent", swaps(AWSFLAG), [])

DOCKER_TP = """diff --git a/Dockerfile b/Dockerfile
--- a/Dockerfile
+++ b/Dockerfile
@@ -2,1 +2,1 @@
-RUN aws s3 sync /src s3://bucket --delete
+RUN aws s3 cp /src s3://bucket --recursive
"""
check("Dockerfile RUN line with a real swap still fires", swaps(DOCKER_TP),
      [("aws", "s3 sync", "s3 cp")])

EXTLESS = SCAUTH.replace("base.sh", "bin/pair-status")
check("extension-less script still in scope", swaps(EXTLESS), [("sc_auth", "list", "identities")])

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
