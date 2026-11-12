#!/usr/bin/env python3
"""A relaunch after a DEAD attempt starts from its first launch's clean tree
(ollama-queue.py stash_dead_attempt, 2026-10-06).

Canary soak seed 3: a daemon restart orphaned an auto-fix worker mid-run; it exited
with no converged transcript and was requeued; the relaunch's seal step COMMITTED the
dead attempt's half-done target edit as "the previous round's deliverable" (same
round), so the rerun saw verify pass over an untouched tree against the enqueue-time
"fails at baseline" stamp and PAUSED FOR REVIEW -> the bundle hung.

Hermetic: temp git repos. QS_SRC points at another queue source (--revert-check)."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("QS_SRC") or HERE / "ollama-queue.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def repo(root):
    root.mkdir(parents=True)
    g(root, "init", "-q")
    (root / "lib").mkdir()
    (root / "lib" / "x.ts").write_text("export const x = 1;\n")
    g(root, "add", "-A")
    g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return root, g(root, "rev-parse", "HEAD").stdout.strip()


def main():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="da-home-")
    ld = SourceFileLoader("oq_da", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oq_da", ld))
    ld.exec_module(m)
    T = Path(tempfile.mkdtemp(prefix="da-"))

    def job(cwd, head, **k):
        j = {"id": "j1", "label": "s3 [auto-fix r1]", "cwd": str(cwd), "auto_fix_round": 1,
             "launch_baseline": {"head": head, "dirty": 0}, "baseline_at": "launch"}
        j.update(k)
        return j

    r, head = repo(T / "a")
    (r / "lib" / "x.ts").write_text("export const x = 2; // half-done\n")
    (r / "stray.ts").write_text("y\n")
    jb = job(r, head)
    res = m.stash_dead_attempt(jb)
    check("relaunch after a dead attempt: its own edits are stashed",
          sorted((res or {}).get("stashed") or []), ["lib/x.ts", "stray.ts"])
    check("...the tree is clean again (first launch's baseline)", m.measure_baseline(str(r))["dirty"], 0)
    check("...and the attempt is kept as evidence (git stash), not deleted",
          "dead attempt of j1" in g(r, "stash", "list").stdout, True)
    check("...so the seal then has NOTHING of this round's to commit as 'previous round'",
          m.seal_prev_round_baseline(jb), None)
    check("...and HEAD did not move", g(r, "rev-parse", "HEAD").stdout.strip(), head)

    r, head = repo(T / "b")
    (r / "lib" / "x.ts").write_text("export const x = 3;\n")
    check("never-launched job (enqueue-time baseline) -> untouched",
          m.stash_dead_attempt(job(r, head, baseline_at="enqueue")), None)
    check("a first launch that was already dirty -> untouched (cannot attribute)",
          m.stash_dead_attempt(job(r, head, launch_baseline={"head": head, "dirty": 1})), None)
    check("a resumed worker (resume transcript) CONTINUES its tree -> untouched",
          m.stash_dead_attempt(job(r, head, resume_transcript="/x.json")), None)
    check("HEAD moved since the launch -> untouched",
          m.stash_dead_attempt(job(r, "0" * 40)), None)
    check("...in all of those the edit is still on disk",
          (r / "lib" / "x.ts").read_text(), "export const x = 3;\n")

    # a declared creation stub (not dirt for measure_baseline) is never stashed
    r, head = repo(T / "c")
    (r / "lib" / "new.ts").write_text('/** Stub for lib/new.ts -- implement per TASK.md. */\n')
    (r / ".dispatch-harness.json").write_text(json.dumps(
        {"authored": [], "target": "lib/new.ts", "creation_task": True}))
    ex = r / ".git" / "info" / "exclude"
    ex.write_text(".dispatch-harness.json\n")
    if m.measure_baseline(str(r))["dirty"] == 0:
        check("a declared creation stub is not stashed",
              m.stash_dead_attempt(job(r, head)), None)
        check("...and stays on disk", (r / "lib" / "new.ts").exists(), True)

    src = SRC.read_text()
    i = src.find("_dead = stash_dead_attempt(job)")
    s = src.find("_sealed = seal_prev_round_baseline(job)")
    check("the launch stashes a dead attempt BEFORE sealing", 0 < i < s, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("never stashes", '            or lb.get("dirty") != 0 or not lb.get("head") or job.get("resume_transcript")):',
     '            or True):'),
    ("stashes a resumed worker's tree",
     '            or lb.get("dirty") != 0 or not lb.get("head") or job.get("resume_transcript")):',
     '            or lb.get("dirty") != 0 or not lb.get("head")):'),
    ("ignores a moved HEAD", 'if head.returncode != 0 or head.stdout.strip() != lb["head"]:',
     "if head.returncode != 0:"),
    ("not called at launch", "                    _dead = stash_dead_attempt(job)\n",
     "                    _dead = None\n"),
]


def revert_check():
    bad = 0
    src = SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "QS_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
