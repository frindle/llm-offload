#!/usr/bin/env python3
"""Hermetic tests for the mini-issue-poller packager: fake gh, claude-api, claude and mini-auto. No network, no queue.
Run on claude-sandbox, never on the Mac:  python3 test-mini-issue-poller-packager.py"""
import importlib.util, json, os, shutil, stat, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
t = Path(tempfile.mkdtemp(prefix="pollerpk-"))
FAILS = []
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
JPG = b"\xff\xd8\xff\xe0" + b"0" * 40
GIF = b"GIF89a" + b"0" * 40
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 40


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {extra}"))
    if not cond:
        FAILS.append(name)


def mk(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


ANSWER = json.dumps({"name": "x-fix", "file": "a.py", "fn": "go",
                     "task_md": "The go() function in a.py returns the wrong total; it must sum all items. Screenshot shows 3 instead of 6."})
mk(t / "gh", '''#!/usr/bin/env python3
import os, sys
d = os.environ["T_DIR"]; a = sys.argv[1:]
open(d + "/gh.calls", "a").write(" ".join(a) + "\\n")
if a[:2] == ["repo", "view"]:
    v = open(d + "/visibility").read().strip()
    if v == "ERR":
        sys.exit(1)
    print('{"visibility": "%s"}' % v)
''')
mk(t / "claude-api", '''#!/usr/bin/env python3
import json, os, sys
d = os.environ["T_DIR"]
open(d + "/api.calls", "a").write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
if os.path.exists(d + "/judge.fail"):
    sys.exit(1)
sys.stderr.write("claude-api: issue-packager cost $0.04 (month $1 of $100)\\n")
print(open(d + "/answer").read())
''')
mk(t / "claude", '''#!/usr/bin/env python3
import json, os, sys
d = os.environ["T_DIR"]
open(d + "/claude.calls", "a").write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
if os.path.exists(d + "/judge.fail"):
    sys.exit(1)
print(json.dumps({"result": open(d + "/answer").read()}))
''')
(t / "answer").write_text(ANSWER)
(t / "home").mkdir()
os.environ.update({"T_DIR": str(t), "POLLER_HOME": str(t / "home"), "POLLER_GH": str(t / "gh"),
                   "MINI_LANE_CLAUDE_API": str(t / "claude-api"), "MINI_LANE_CLAUDE": str(t / "claude")})
ld = SourceFileLoader("poller_pk", str(HERE / "mini-issue-poller"))
P = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld)); ld.exec_module(P)


def calls(name):
    f = t / name
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def reset(vis="PUBLIC"):
    for n in ("api.calls", "claude.calls", "gh.calls", "judge.fail"):
        (t / n).unlink(missing_ok=True)
    (t / "visibility").write_text(vis)


def tmpdirs():
    return set(Path(tempfile.gettempdir()).glob("poller-img-*"))


co = t / "checkout"; co.mkdir()
subprocess.run(["git", "init", "-q", "-b", "main", str(co)], check=True)
subprocess.run(["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
(co / "a.py").write_text("x\n")
subprocess.run(["git", "-C", str(co), "add", "."], check=True)
subprocess.run(["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "a"], check=True)
RC = {"checkout": str(co), "base": "HEAD"}
ISS = {"number": 7, "title": "wrong total", "body": "go() is wrong", "author": {"login": "frindle"}}

def cfg(**kw):
    (t / "home" / "config.json").write_text(json.dumps(kw))


# --- visibility -> judge (configured claude-api:haiku)
cfg(packager_judge="claude-api:haiku")
reset("PUBLIC")
check("public repo: visibility true", P.repo_is_public("frindle/pub") is True)
pk = P.package_issue("frindle/pub", RC, ISS)
a = calls("api.calls")
check("public -> claude-api (one call, no claude)", len(a) == 1 and not calls("claude.calls"), str(a))
check("claude-api name, budget <= 0.15, model haiku", a and a[0]["argv"][0] == "issue-packager" and 0 < float(a[0]["argv"][1]) <= 0.15
      and a[0]["argv"][2] == "haiku", str(a[:1]))
check("description is judge text, framed as untrusted; file kept", "wrong total" in pk["desc"] and "untrusted" in pk["desc"] and pk["file"] == "a.py" and pk["fn"] == "go", str(pk))

for vis, label in (("PRIVATE", "private"), ("INTERNAL", "internal"), ("ERR", "check fails"), ("garbage", "unknown")):
    reset(vis)
    pk = P.package_issue("frindle/x", RC, ISS)
    check(f"{label} -> never claude-api", not calls("api.calls") and len(calls("claude.calls")) == 1)
reset("PRIVATE")
P.package_issue("frindle/x", RC, ISS)
check("private judge: no --bare", "--bare" not in calls("claude.calls")[0]["argv"])

# --- budget cap across retries: unparseable answer -> retry; total stays <= 0.15
reset("PUBLIC")
(t / "answer").write_text("not json at all")
try:
    P.package_issue("frindle/pub", RC, ISS)
    check("unparseable judge answer -> raises", False)
except Exception:
    check("unparseable judge answer -> raises", True)
b = [float(c["argv"][1]) for c in calls("api.calls")]
check("retry gets only what is left of 0.15 after the first call cost $0.04", b == [0.15, 0.11], str(b))
(t / "answer").write_text(ANSWER)

# --- image allow-list
ok_urls = ["https://github.com/user-attachments/assets/abc-123",
           "https://private-user-images.githubusercontent.com/1/2.png?jwt=zzz",
           "https://user-images.githubusercontent.com/1/3.png"]
bad_urls = ["http://github.com/user-attachments/assets/a", "https://github.com/frindle/repo/raw/main/a.png",
            "https://evil.example/user-attachments/a.png", "https://github.com.evil.example/user-attachments/a",
            "https://user@github.com/user-attachments/a", "https://github.com:8443/user-attachments/a",
            "https://raw.githubusercontent.com/a/b/c.png", "https://avatars.githubusercontent.com/u/1"]
body = " ".join(f"![i]({u})" for u in ok_urls + bad_urls)
check("allow-list keeps exactly the three hosts", P.image_urls(body) == ok_urls, str(P.image_urls(body)))
check("html img src is found", P.image_urls('<img width="3" src="https://github.com/user-attachments/assets/q">') == ["https://github.com/user-attachments/assets/q"])
many = " ".join(f"https://github.com/user-attachments/assets/{i}" for i in range(9))
check("at most 5 images", len(P.image_urls(many)) == 5)

# --- type and size
check("magic bytes png/jpg/gif/webp", [P.image_type(x) for x in (PNG, JPG, GIF, WEBP)] == ["png", "jpg", "gif", "webp"])
check("svg/html/bmp/pdf rejected", all(P.image_type(x) is None for x in (b"<svg xmlns", b"<html>", b"BM\0\0", b"%PDF-1.4", b"RIFF\0\0\0\0WAVE")))
d = Path(tempfile.mkdtemp(prefix="dl-"))
data = {ok_urls[0]: PNG, ok_urls[1]: b"%PDF-1.7 fake", ok_urls[2]: b"\xff\xd8\xff" + b"0" * (P.IMG_BYTES)}
paths, dropped = P.download_images(" ".join(ok_urls), d, fetch=lambda u: data[u])
check("only the valid-type, in-size image is kept", len(paths) == 1 and paths[0].endswith("image-1.png") and dropped == 2, f"{paths} {dropped}")
data[ok_urls[2]] = b"\xff\xd8\xff" + b"0" * (P.IMG_BYTES - 3)
paths, dropped = P.download_images(ok_urls[2], d, fetch=lambda u: data[u])
check("exactly 10 MB is accepted", len(paths) == 1 and dropped == 0)
def boom(u): raise OSError("net")
paths, dropped = P.download_images(ok_urls[0], d, fetch=boom)
check("a failed fetch is skipped, not fatal", paths == [] and dropped == 1)
shutil.rmtree(d)

# --- images reach the judge as Read-only access, text only to coder; temp dir deleted
reset("PUBLIC")
before = tmpdirs()
body = f"see ![s]({ok_urls[0]}) and ![e](https://evil.example/a.png)"
pk = P.package_issue("frindle/pub", RC, dict(ISS, body=body), fetch=lambda u: PNG)
a = calls("api.calls")[0]
check("images: judge gets Read tool, one --add-dir, image path in prompt", "Read" in a["argv"] and a["argv"].count("--add-dir") == 1
      and "image-1.png" in a["stdin"] and "evil.example" not in a["stdin"].split("Images:")[-1], str(a["argv"]))
reset("PUBLIC")
P.package_issue("frindle/pub", RC, ISS)
check("no images: tools empty, no add-dir", "--add-dir" not in calls("api.calls")[0]["argv"])
check("temp dir removed after success", tmpdirs() == before)
reset("PUBLIC"); (t / "judge.fail").write_text("")
try:
    P.package_issue("frindle/pub", RC, dict(ISS, body=body), fetch=lambda u: PNG)
except Exception:
    pass
check("temp dir removed after failure", tmpdirs() == before)

# --- packaging failure -> failed (status words only), nothing submitted
mk(t / "auto", '''#!/usr/bin/env python3
import json, os, sys
d = os.environ["T_DIR"]; a = sys.argv[1:]
if a[0] == "new":
    open(d + "/auto.calls", "a").write(json.dumps({"args": a, "stdin": sys.stdin.read()}) + "\\n")
    print("job_id: abc123def")
''')
mk(t / "queue", "#!/bin/sh\nexit 0\n")
P.MINI_AUTO = [str(t / "auto")]
P.QUEUE_CLI = [str(t / "queue")]
cfg(packager="on", packager_judge="claude-api:haiku")
reset("PUBLIC"); (t / "judge.fail").write_text("")
jid, ok = P.submit_issue("frindle/pub", RC, ISS, "m1")
check("judge failure -> submit_issue fails, nothing submitted", (jid, ok) == (None, False) and not (t / "auto.calls").exists())
reset("PUBLIC")
jid, ok = P.submit_issue("frindle/pub", RC, ISS, "m1")
s = calls("auto.calls")[0]
check("success: description via stdin, --file/--fn passed, coder is text only", ok and jid == "abc123def" and "--file" in s["args"]
      and "wrong total" in s["stdin"] and "--model" in s["args"])

# --- full tick: failure shows only the status words
P.set_status = lambda repo, n, word, label: writes.append((n, word, label))
writes = []
cfg(packager="on", packager_judge="claude-api:haiku", owner="frindle", label="local-fix", daily_cap=5, models=["m1"],
    owner_repos=False, repos={"frindle/pub": RC})
mk(t / "probe", "#!/bin/sh\nexit 0\n")
P.DB_PROBE_CMD = str(t / "probe")
P.search_issues = lambda owner, label: {"frindle/pub": [dict(ISS, number=9, labels=[{"name": "local-fix"}])]}
reset("PUBLIC"); (t / "judge.fail").write_text(""); (t / "auto.calls").unlink(missing_ok=True)
s = P.tick()
check("tick: packaging failure -> failed, status words only", writes == [(9, "failed", "local-failed")] and not (t / "auto.calls").exists(), str(writes))
st = P.load_state()
check("tick: state records failed", st["jobs"]["frindle/pub#9"]["status"] == "failed")


# --- local default: the lane's default judge through the queue; vision model for images; no claude, no API
ml = P._load_lane()
subs = []
def fake_submit(self, task, job):
    subs.append((self.spec, task, job)); return "jq"
def fake_fetch(self, jid):
    return "done", {"ok": True, "model": subs[-1][2]["model"], "lane": "darkbloom", "result": json.loads(ANSWER)}
ml.EnqueueJudge._submit = fake_submit; ml.EnqueueJudge._fetch = fake_fetch
os.environ["MINI_LANE_JUDGE"] = "darkbloom:text-model-x"
cfg(models=["plain-model", "future-vl-model-9"])
reset("PUBLIC")
body = f"![s]({ok_urls[0]})"
pk = P.package_issue("frindle/pub", RC, ISS)
check("default (no setting) is local text judge via the queue; no claude/claude-api",
      subs and subs[-1][0] == "darkbloom:text-model-x" and "images" not in subs[-1][1] and not calls("api.calls") and not calls("claude.calls")
      and not any("repo view" in c for c in (t / "gh.calls").read_text().splitlines()) if (t / "gh.calls").exists() else True, str(subs[-1:]))
check("local description kept", pk["file"] == "a.py" and "wrong total" in pk["desc"])
pk = P.package_issue("frindle/pub", RC, dict(ISS, body=body), fetch=lambda u: PNG)
check("images -> vision model (first model with a vl part), image parts in the task",
      subs[-1][0] == "darkbloom:future-vl-model-9" and subs[-1][2]["model"] == "future-vl-model-9"
      and subs[-1][1]["images"][0]["mime"] == "image/png" and subs[-1][1]["images"][0]["b64"], str(subs[-1][0]))
cfg(models=["plain-model"], vision_model="my-vision-3")
P.package_issue("frindle/pub", RC, dict(ISS, body=body), fetch=lambda u: PNG)
check("config vision_model wins", subs[-1][0] == "darkbloom:my-vision-3")
cfg(models=["plain-model"])
n0 = len(subs)
P.package_issue("frindle/pub", RC, dict(ISS, body=body), fetch=lambda u: PNG)
check("no vision model: text-only, images dropped", "images" not in subs[-1][1] and len(subs) == n0 + 1 and "could not be used" in subs[-1][1]["text"])
check("a new model needs no code: default-judge names it", (os.environ.update(MINI_LANE_JUDGE="darkbloom:nemotron-next") or True)
      and (P.package_issue("frindle/pub", RC, ISS) and subs[-1][0] == "darkbloom:nemotron-next"))
cfg(packager_judge="claude:haiku"); reset("PUBLIC")
P.package_issue("frindle/pub", RC, ISS)
check("claude:haiku setting: subscription even for a public repo", len(calls("claude.calls")) == 1 and not calls("api.calls"))
cfg(packager_judge="claude-api:haiku"); reset("PRIVATE")
P.package_issue("frindle/pub", RC, ISS)
check("claude-api setting on a private repo -> claude:haiku", len(calls("claude.calls")) == 1 and not calls("api.calls"))

# --- judge not answering (Darkbloom down): wait, not failed
def down_fetch(self, jid): return "queued", None
ml.EnqueueJudge._fetch = down_fetch
cfg(packager="on", packager_judge="local", packager_timeout_s=-1, owner="frindle", label="local-fix", daily_cap=5, models=["m1"],
    owner_repos=False, repos={"frindle/pub": RC})
os.environ["MINI_LANE_JUDGE"] = "darkbloom:text-model-x"
writes.clear(); P.save_state({"jobs": {}, "accepted": {}})
s = P.tick()
check("tick: judge not answering -> no state, no GitHub write, cap not spent", not writes and not P.load_state()["jobs"] and P.accepted_today(P.load_state()) == 0 and s["waited"], str((writes, s)))
ml.EnqueueJudge._fetch = fake_fetch

# --- redirects: GitHub-family https hosts only, never with the token
import urllib.request
rd = P._Redirect(); rq = urllib.request.Request("https://github.com/user-attachments/assets/x", headers={"Authorization": "token T"})
nr = rd.redirect_request(rq, None, 302, "", {}, "https://github-production-user-asset.s3.amazonaws.com/a?sig=1")
check("redirect to the S3 host is followed without the token", nr is not None and "Authorization" not in nr.headers and "Authorization" not in nr.unredirected_hdrs)
check("redirect to a lookalike or http host is refused", all(rd.redirect_request(rq, None, 302, "", {}, u) is None
      for u in ("https://evilamazonaws.com/a", "http://s3.amazonaws.com/a", "https://evil.example/a")))

# --- poller never lets the coder see an image or the raw issue when the packager is on
check("source: only text reaches mini-auto", "input=desc" in (HERE / "mini-issue-poller").read_text())
shutil.rmtree(t, ignore_errors=True)
print("FAILED: " + ", ".join(FAILS) if FAILS else "all ok")
sys.exit(1 if FAILS else 0)
