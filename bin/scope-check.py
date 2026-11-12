#!/usr/bin/env python3
"""Which files did this diff touch that the task never mentioned?

Part of the Studio pre-gate (2026-08-31). A dispatched model editing files nobody
asked it to touch is one of the failure shapes a first-pass gate should catch,
and it is DECIDABLE: the set of paths in the diff minus the set of paths the task
names is arithmetic, not judgement.

Deliberately ADVISORY, never a hard fail. A task saying "edit route.ts" can
legitimately require touching a helper it never names, and a scope checker that
blocks that would train everyone to ignore it. It answers "here is what you did
not ask for", and a human decides whether that was correct.

Generated files are excluded outright -- a lockfile or .tsbuildinfo changing is a
build product, not an out-of-scope edit (learned the hard way: the reviewer's
first real false positive was firing on tsconfig.tsbuildinfo).
"""
import argparse, json, re, sys
from pathlib import Path

_GENERATED = re.compile(r"""(?xi)
    (^|/)(node_modules|vendor|dist|build|out|\.next|coverage|__snapshots__)/
  | (^|/)[^/]*\.(tsbuildinfo|min\.js|min\.css|map|lock)$
  | (^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock
        |Cargo\.lock|composer\.lock|go\.sum|Gemfile\.lock)$
""")


# Path-shaped tokens in the TASK, used for ONE narrow decision: telling "the
# task named nothing" apart from "the task named files and the diff missed them
# all". Requires a real extension or a known extensionless build filename --
# NOT a bare slash test, which would read prose like "its startup/entrypoint
# script" or even "and/or" as a named path and turn an honest abstain into a
# false accusation.
_TASK_PATH = re.compile(r"""(?xi)
    (?<![\w/])
    [\w.@-]* (?:/[\w.@-]+)* \.
    (tsx?|jsx?|mjs|cjs|py|rb|go|rs|java|cs|c|h|cpp|hpp|swift|kt|php|sql
     |sh|bash|zsh|prisma|ya?ml|toml|json|md|env|cfg|ini)
    (?![\w])
  | (?<![\w/])(Dockerfile|Makefile|Procfile|Justfile|Gemfile|Rakefile)(?![\w])
""")


# FRAMEWORK NAMES ARE NOT FILENAMES. "Next.js" satisfies every structural test
# for a path -- stem, dot, known extension -- and was captured as one on the very
# first job this ran against, inflating "the task names 3 files" to include a
# framework and a docs reference. It matters beyond cosmetics: a task that
# mentions only Next.js and no real file would otherwise be read as "names
# files", turning an honest abstain into a false wrong-target accusation. Same
# recurring class as prose-inside-backticks in completeness.py -- a shallow
# looks-like-code test accepting something that merely resembles code.
_NOT_A_PATH = {
    "next.js", "node.js", "nuxt.js", "vue.js", "three.js", "d3.js", "chart.js",
    "express.js", "discord.js", "socket.io", "backbone.js", "ember.js",
    "angular.js", "react.js", "jquery.js", "moment.js", "video.js",
}


def task_named_paths(task: str) -> list[str]:
    seen, out = set(), []
    for m in _TASK_PATH.finditer(task):
        t = m.group(0).strip(" `.,;:)")
        if not t or t.lower() in _NOT_A_PATH or t in seen:
            continue
        seen.add(t); out.append(t)
    return out


# THE MODEL EDITING ITS OWN TEST is a different finding from the model touching
# an unnamed helper, and it must not be reported at the same severity or lost to
# an abstain. The gate's whole input is "verify exited 0"; if the diff also
# rewrote the thing that produced that 0, the pass certifies nothing. Seen live
# on dashboard-newjobs-fix (2026-08-31), where verify.sh was the ONLY unmentioned
# file and read as an ordinary low alongside four cosmetic ones.
# NARROWED after a false positive on a real job: the first version allowed an
# arbitrary `-suffix`, so `gate-on-complete.py` matched "gate" + "-on-complete"
# and a dispatch that never touched its verify was reported as "the diff edits
# the check that gated it" -- an alarming, wrong finding on correct work.
# `gate` is dropped entirely: gate*.py are harness SOURCE, and when one of them
# genuinely IS the job's verify the --verify basename match below catches it
# precisely, which is the reliable signal anyway.
_SELFTEST = re.compile(r"""(?xi)
    (^|/) (verify|check|test) (-[\w.-]+)? (\.(sh|py|js|mjs|ts|bash|zsh))? $
""")


def diff_paths(diff: str) -> list[str]:
    seen, out = set(), []
    for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M):
        p = m.group(1).strip()
        if p != "/dev/null" and p not in seen:
            seen.add(p); out.append(p)
    if not out:
        for m in re.finditer(r"^diff --git a/\S+ b/(\S+)", diff, re.M):
            p = m.group(1)
            if p not in seen:
                seen.add(p); out.append(p)
    return out


def mentioned(task: str, path: str) -> str | None:
    """Did the task name this path, its basename, or its containing directory?"""
    if path in task:
        return "full path"
    base = path.rsplit("/", 1)[-1]
    if re.search(rf"(?<![\w/]){re.escape(base)}(?![\w])", task):
        return f"basename `{base}`"
    stem = base.rsplit(".", 1)[0]
    if len(stem) > 3 and re.search(rf"(?<![\w/]){re.escape(stem)}(?![\w])", task):
        return f"stem `{stem}`"
    # NO directory-level matching. The first version treated any file under a
    # mentioned directory as in-scope, so a task naming server/telemetry-watchdog.js
    # silently blessed every other file under server/ -- measured on a 5-file
    # refactor where it reported "every edited file is named by the task" and
    # named none of them specifically. For a scope check the false NEGATIVE is the
    # dangerous direction: it exists to surface what you did not ask for. Since
    # the output is advisory, over-reporting costs a glance and under-reporting
    # costs the whole point.
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--diff", required=True)
    ap.add_argument("--verify", default="",
                    help="the dispatch's verify command, so a diff that edits the "
                         "check which gated it is reported as such")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    task = Path(a.task_file).expanduser().read_text()
    diff = Path(a.diff).expanduser().read_text()

    in_scope, unmentioned, generated, selftest = [], [], [], []
    weak_scope: list[tuple[str, str]] = []
    verify_target = (a.verify or "").strip()
    for p in diff_paths(diff):
        if _GENERATED.search(p):
            generated.append(p); continue
        base = p.rsplit("/", 1)[-1]
        if _SELFTEST.search(p) or (verify_target and base and base in verify_target):
            selftest.append(p); continue
        why = mentioned(task, p)
        (in_scope if why else unmentioned).append((p, why))
        # Record HOW WEAK the match was. A stem is frequently an ordinary word in
        # prose ("fix the config handling" -> config.py), so a stem-only match is
        # much weaker evidence that the task actually asked for this file than a
        # full path or a basename. Reported, never reclassified: see the module
        # note -- promoting these to unmentioned would manufacture noise, and a
        # noisy scope check is an ignored one.
        if why and why.startswith("stem "):
            weak_scope.append((p, why))

    # ABSTAIN when the task anchors NOTHING. If not one edited file is named by
    # the task, there is no scope signal to give: "all N files are unmentioned"
    # carries the same information as "0 files are unmentioned", and it inflates
    # a clean N-file feature into N concerns.
    #
    # The trigger is measured, not guessed: it is "the task named none of the
    # edited files", NOT "the task contains no path-shaped text". Checked against
    # the live batch 2026-08-31 -- all four symptom-based tasks DID anchor
    # (5, 3, 2 and 1 named files respectively), so this abstain correctly fires
    # on none of them and their unmentioned files stay reported. A text-level
    # "does the task mention a path" test would have been a coin flip on prose
    # like "its startup/entrypoint script".
    #
    # Abstaining is NOT passing. It is reported as a visible not-asserted state,
    # the same rule completeness follows, because "not checked" reading as
    # "checked and fine" is the one thing a gate must never do. Self-test edits
    # are reported REGARDLESS of the abstain -- that signal does not depend on
    # whether the dispatcher named files.
    # TWO VERY DIFFERENT SITUATIONS produce an empty in_scope, and collapsing
    # them was a real defect in the first version of this abstain (2026-08-31):
    #
    #   (a) the task names NO file at all  -> nothing to compare against, abstain.
    #   (b) the task NAMES files and the diff touched NONE of them -> the
    #       STRONGEST scope finding there is. The model worked somewhere else
    #       entirely. Caught on resell-cc-edit-inline (ef842e8c36cd), where the
    #       task named app/cardcenter/page.tsx and the diff touched only
    #       components/GiftCards.tsx -- and the first abstain silently swallowed
    #       exactly the signal it was built alongside.
    #
    # (b) is reported HIGH but stays category=input at the gate: a task can name
    # a file as CONTEXT ("see route.ts for the pattern") and correctly edit
    # elsewhere, so this must be loud without being able to fail good code.
    named = task_named_paths(task)
    wrong_target = bool(named) and not in_scope and bool(unmentioned)
    asserted = bool(in_scope) or wrong_target
    abstained = [] if asserted else unmentioned
    if not asserted:
        unmentioned = []

    if a.json:
        print(json.dumps({"in_scope": [p for p, _ in in_scope],
                          "weak_scope": [p for p, _ in weak_scope],
                          "weak_scope_detail": [f"{p} ({w})" for p, w in weak_scope],
                          "unmentioned": [p for p, _ in unmentioned],
                          "generated": generated,
                          "selftest": selftest,
                          "asserted": asserted,
                          "unanchored": [p for p, _ in abstained],
                          "wrong_target": wrong_target,
                          "task_named": named[:12],
                          "not_asserted_reason": (
                              "" if asserted else
                              "the task names none of the edited files, so scope "
                              "has no anchor -- it did not run")}, indent=1))
        return 0

    print("=== scope: what did the diff touch that the task did not name? ===")
    for p, why in in_scope:
        print(f"  [named]       {p}   ({why})")
    for p, _ in unmentioned:
        print(f"  [UNMENTIONED] {p}")
    for p, _ in abstained:
        print(f"  [unanchored]  {p}   (scope abstained; see below)")
    for p in selftest:
        print(f"  [SELF-TEST]   {p}   (the diff edits the check that gated it)")
    for p in generated:
        print(f"  [generated]   {p}   (build product, ignored)")
    print()
    if selftest:
        print(f"  {len(selftest)} file(s) the diff edits are the VERIFY/CHECK itself. "
              f"The gate's input is 'verify exited 0'; if the diff also rewrote what "
              f"produced that 0, read the change to the check before the change to "
              f"the code.")
    if wrong_target:
        print(f"  WRONG TARGET? -- the task names {len(named)} file(s) "
              f"({', '.join(named[:4])}) and the diff touched NONE of them. Either "
              f"the model worked in the wrong place, or those were context "
              f"references. Read this before the code.")
    elif not asserted:
        print(f"  NOT ASSERTED -- the task names none of the {len(abstained)} edited "
              f"file(s), so there is nothing to compare against and scope did not "
              f"run. This is an abstain, NOT a clean result.")
    elif unmentioned:
        print(f"  {len(unmentioned)} file(s) the task never names. ADVISORY, not a "
              f"failure: a task can legitimately require touching a helper it does "
              f"not name. Confirm each was necessary.")
    else:
        print("  Every edited file is named by the task.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
