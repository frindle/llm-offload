"""Task fixtures — the interface, and how a fixture is staged for one run.

THE INTERFACE
-------------
A task is a directory containing `task.toml` and a `repo/` subtree:

    fixtures/<name>/
      task.toml           metadata, prompt, verify commands, guards
      repo/               the codebase the agent is given, verbatim
      api_surface.txt     optional; the `apisurface` arm prepends this

`task.toml` fields:

    name          stable identifier; goes in the results CSV
    work_class    which row of the routing table this fills (see METHODOLOGY)
    language      primary language of the fixture
    prompt        the task text handed to the model
    verify        shell command; exit 0 means the task's stated goal is met
    preflight     shell command run against the PRISTINE tree before the run
    preflight_expect  "pass" or "fail" — see below
    restore_paths  files restored from baseline before verify runs

WHY EACH GUARD EXISTS
---------------------
`preflight_expect` is inverted for debugging tasks. A greenfield task's verify
must FAIL on a pristine tree (otherwise every model scores an instant success
for doing nothing). A debugging task's reproduction must ALSO fail on the
pristine tree — that is what makes it a reproduction. Either way the guard runs
before any model does, so a fixture that has quietly stopped discriminating is
caught at setup rather than after a round has been spent on it.

`restore_paths` is the anti-gaming rule. Verify restores the test files from
the baseline before running them, so a model cannot pass by editing the tests
instead of the code. The tests are ground truth; they are not part of the tree
the model is being scored on.

WRITING A FIXTURE THAT MEASURES WHAT YOU THINK
----------------------------------------------
Two rules that cost a real round when they were broken:

1. **Never document a planted defect.** A commit message naming the bug, a test
   docstring narrating the diagnosis, or task text saying "this used to work"
   are all answer keys. `git log` is the first command any competent debugger
   runs. Ship the defect under a plausible refactor message, keep diagnoses out
   of the tests, and purge the honest commit from history.

2. **Remove instruction ambiguity before it becomes a finding.** A prompt that
   says "follow the existing patterns" AND "implement this as a real working
   feature" AND asks which files were created, against a repo where the feature
   partly exists, does not measure recognition — it measures which half of a
   contradictory instruction the model happened to obey. Say explicitly what to
   do if the thing already exists, and the same cell becomes a clean
   discriminator instead.
"""
from __future__ import annotations

import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


class TaskError(RuntimeError):
    pass


@dataclass(frozen=True)
class Task:
    dir: Path
    name: str
    work_class: str
    language: str
    prompt: str
    verify: str
    preflight: str = ""
    preflight_expect: str = "fail"
    restore_paths: tuple[str, ...] = ()
    setup: str = ""
    notes: str = ""

    @property
    def repo(self) -> Path:
        return self.dir / "repo"

    @property
    def api_surface(self) -> str:
        p = self.dir / "api_surface.txt"
        return p.read_text() if p.is_file() else ""


def load(task_dir: Path) -> Task:
    task_dir = Path(task_dir)
    manifest = task_dir / "task.toml"
    if not manifest.is_file():
        raise TaskError(f"no task.toml in {task_dir}")
    with manifest.open("rb") as fh:
        raw = tomllib.load(fh)
    missing = [k for k in ("name", "prompt", "verify") if k not in raw]
    if missing:
        raise TaskError(f"{manifest} is missing: {', '.join(missing)}")
    if not (task_dir / "repo").is_dir():
        raise TaskError(f"{task_dir} has no repo/ subtree")
    return Task(
        dir=task_dir,
        name=raw["name"],
        work_class=raw.get("work_class", "unspecified"),
        language=raw.get("language", "unspecified"),
        prompt=raw["prompt"].strip(),
        verify=raw["verify"].strip(),
        preflight=raw.get("preflight", "").strip(),
        preflight_expect=raw.get("preflight_expect", "fail"),
        restore_paths=tuple(raw.get("restore_paths", []) or []),
        setup=raw.get("setup", "").strip(),
        notes=raw.get("notes", "").strip(),
    )


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def stage(task: Task, dest: Path) -> str:
    """Copy the fixture repo to a scratch tree and give it a baseline commit.

    A fresh copy per run rather than a reset of a shared tree: a shared tree
    leaks state between runs through untracked files and stale build artefacts,
    and the leak shows up as an unexplained result in whichever run happened to
    follow a messy one.

    Returns the baseline commit SHA, which every diff and restore is taken
    against.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(task.repo, dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                  ".git", "node_modules"))
    # `--template=` deliberately empty: a staged tree must not inherit the
    # operator's global git template. Hooks installed there would fire on the
    # harness's own baseline commit and on anything the agent commits during a
    # run, which is both a reproducibility problem and, for a hook that pushes
    # or notifies, a genuine hazard.
    _git(["init", "-q", "--template="], dest)
    _git(["add", "-A"], dest)
    _git(["-c", "user.name=harness", "-c", "user.email=harness@localhost",
          "commit", "-q", "-m", "baseline"], dest)
    sha = _git(["rev-parse", "HEAD"], dest).stdout.strip()
    if task.setup:
        subprocess.run(task.setup, shell=True, cwd=dest,
                       capture_output=True, text=True, timeout=600)
    return sha


def preflight(task: Task, tree: Path, timeout_s: int = 300) -> tuple[bool, str]:
    """Run the guard against the pristine tree.

    A greenfield fixture whose verify already passes, or a debugging fixture
    whose reproduction already passes, scores every model as an instant success
    and the whole cell is worthless. This is the check that catches it — and it
    has to run BEFORE the round, because run afterwards it is indistinguishable
    from explaining away a result you did not like.
    """
    cmd = task.preflight or task.verify
    r = subprocess.run(cmd, shell=True, cwd=tree, capture_output=True,
                       text=True, timeout=timeout_s)
    passed = r.returncode == 0
    want_pass = task.preflight_expect == "pass"
    ok = passed == want_pass
    detail = (f"preflight {'passed' if passed else 'failed'} "
              f"(expected {'pass' if want_pass else 'fail'}) exit={r.returncode}")
    if not ok:
        detail += ("\nThis fixture cannot discriminate. "
                   + ("A greenfield verify that already passes scores everyone an "
                      "instant success; " if want_pass is False and passed else "")
                   + "fix the fixture before dispatching a round.\n"
                   + (r.stdout or "")[-1500:] + (r.stderr or "")[-1500:])
    return ok, detail


def verify(task: Task, tree: Path, baseline_sha: str,
           timeout_s: int = 300) -> tuple[bool, str]:
    """Restore ground-truth files from baseline, then run the verify command.

    The restore is not politeness. Without it a model can pass by rewriting the
    tests, and that row is indistinguishable in the CSV from one that fixed the
    code.
    """
    if task.restore_paths:
        _git(["checkout", baseline_sha, "--", *task.restore_paths], tree)
    r = subprocess.run(task.verify, shell=True, cwd=tree, capture_output=True,
                       text=True, timeout=timeout_s)
    tail = ((r.stdout or "")[-3000:] + "\n" + (r.stderr or "")[-3000:]).strip()
    return r.returncode == 0, tail


def changed_files(tree: Path, baseline_sha: str) -> int:
    tracked = _git(["diff", "--name-only", baseline_sha], tree).stdout.split()
    untracked = [ln for ln in _git(["status", "--porcelain", "-uall"], tree)
                 .stdout.splitlines() if ln.startswith("??")]
    return len(set(tracked)) + len(untracked)


def archive_diff(tree: Path, baseline_sha: str, dest: Path) -> Path:
    """Write the run's full diff, including a porcelain listing of new files.

    `git diff` alone omits untracked files, so a run whose entire output is new
    files archives an empty diff and reads as having done nothing.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = _git(["diff", baseline_sha], tree).stdout
    body += "\n" + _git(["status", "--porcelain", "-uall"], tree).stdout
    dest.write_text(body)
    return dest


def repo_map(tree: Path, max_files: int = 400) -> str:
    """Generated from the worktree at dispatch time, so it can never drift from
    the tree the model is actually given."""
    files = _git(["ls-files"], tree).stdout.splitlines()
    keep = [f for f in files
            if not f.startswith(("node_modules/", "dist/", "build/", ".build/"))
            and not f.endswith(("package-lock.json", "yarn.lock", "pnpm-lock.yaml"))]
    listing = "\n".join(keep[:max_files])
    return ("Repository map (generated, for orientation -- not exhaustive):\n\n"
            + listing)
