#!/usr/bin/env python3
"""mini-issue-poller: any repo of the configured owner is allowed (other owners are not); after "passed checks" a DRAFT
PR is opened whose body says it awaits review."""
import importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
t = Path(tempfile.mkdtemp(prefix="pollerpr-"))
os.environ["POLLER_HOME"] = str(t / "home"); (t / "home").mkdir()
os.environ["T_DIR"] = str(t)
gh = t / "gh"
gh.write_text('''#!/usr/bin/env python3
import json, os, sys
d = os.environ["T_DIR"]; a = sys.argv[1:]
open(d + "/gh.calls", "a").write(json.dumps(a) + "\\n")
if a[:2] == ["repo", "list"]:
    print(json.dumps([{"nameWithOwner": "frindle/found"}, {"nameWithOwner": "someone/else"}, {"nameWithOwner": "frindle/nocheckout"}]))
''')
gh.chmod(0o755)
os.environ["POLLER_GH"] = str(gh)
def g(*a, cwd=None):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
root = t / "repos"; (root / "found").mkdir(parents=True)
origin = t / "origin.git"; g("init", "-q", "--bare", "-b", "main", str(origin))
co = root / "found"; g("init", "-q", "-b", "main", cwd=co)
g("config", "user.email", "t@t", cwd=co); g("config", "user.name", "t", cwd=co)
(co / "a.txt").write_text("1\n"); g("add", "-A", cwd=co); g("commit", "-q", "-m", "i", cwd=co)
g("remote", "add", "origin", str(origin), cwd=co); g("push", "-q", "origin", "main", cwd=co); g("fetch", "-q", cwd=co)
(t / "payload").write_text("2\n")
auto = t / "auto"
auto.write_text('''#!/usr/bin/env python3
import sys, os
a = sys.argv[1:]
if a[0] == "base":
    import subprocess
    print(subprocess.run(["git", "-C", os.environ["T_DIR"] + "/repos/found", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
if a[0] == "land":
    assert "--assume-ok" in a and os.environ.get("MINI_AUTO_ASSUME_OK") == "poller-draft-pr"
    open(a[a.index("--into") + 1] + "/a.txt", "w").write(open(os.environ["T_DIR"] + "/payload").read())
''')
auto.chmod(0o755)
os.environ["POLLER_MINI_AUTO"] = str(auto)
ld = importlib.machinery.SourceFileLoader("poller", str(HERE / "mini-issue-poller"))
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("poller", ld)); ld.exec_module(m)
fails = []
def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)

cfg = {**m.DEFAULTS, "checkout_root": str(root), "repos": {"other/x": {"checkout": "/x"}}}
rm = m.owner_repos(cfg)
chk("owner repo with a checkout is allowed", "frindle/found" in rm)
chk("repo of another owner is not", "someone/else" not in rm and "other/x" not in rm)
chk("owner repo without a checkout is skipped", "frindle/nocheckout" not in rm)
ok = m.open_draft_pr("frindle/found", 7, {"job": "abc"}, checkout=rm["frindle/found"])
chk("draft PR opened", ok is True)
calls = [json.loads(l) for l in (t / "gh.calls").read_text().splitlines()]
pr = [c for c in calls if c[:2] == ["pr", "create"]]
chk("created as draft", bool(pr) and "--draft" in pr[0])
chk("body says it awaits review", bool(pr) and "Awaiting review" in pr[0][pr[0].index("--body") + 1])
chk("branch is issue-7 and pushed", subprocess.run(["git", "-C", str(origin), "rev-parse", "--verify", "issue-7"], capture_output=True).returncode == 0)
chk("temporary worktree removed", "poller-pr-" not in subprocess.run(["git", "-C", str(co), "worktree", "list"], capture_output=True, text=True).stdout)
# a diff carrying private info is not pushed
(t / "payload").write_text("host 198.51.100.43\n")
try:
    m.open_draft_pr("frindle/found", 8, {"job": "abc"}, checkout=rm["frindle/found"]); blocked = False
except RuntimeError as e:
    blocked = "private-info" in str(e)
chk("private-info hit aborts before the push", blocked)
chk("no branch issue-8 pushed", subprocess.run(["git", "-C", str(origin), "rev-parse", "--verify", "issue-8"], capture_output=True).returncode != 0)
print("FAIL: " + ", ".join(fails) if fails else "POLLER_PR_OK")
sys.exit(1 if fails else 0)
