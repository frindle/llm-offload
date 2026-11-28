#!/usr/bin/env python3
"""Tests for docker_update_watch.py (2026-11-27): auto vs manual, registry digest compare (fake registry, token flow,
multi-arch), git compare for locally built images, the lines, and the post through digest_lines. No network."""
import json
import os
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import docker_update_watch as W  # noqa: E402
import digest_lines as L  # noqa: E402

FAILS, N = [], [0]


def chk(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(name)
        print("FAIL", name, detail)


def ctr(name, image, labels=None, digests=None, img_labels=None):
    return {"Name": "/" + name, "Image": "sha256:" + name, "Config": {"Image": image, "Labels": labels or {}},
            "_image": {"Id": "sha256:" + name, "RepoDigests": digests or [], "Labels": img_labels or {}}}


DM = {"net.unraid.docker.managed": "dockerman"}
CP = {"com.docker.compose.project": "stack"}

# --- split_ref
chk("ref hub official", W.split_ref("nginx:1.27") == ("registry-1.docker.io", "library/nginx", "1.27"))
chk("ref hub user no tag", W.split_ref("foo/bar") == ("registry-1.docker.io", "foo/bar", "latest"))
chk("ref lscr -> ghcr", W.split_ref("lscr.io/linuxserver/sonarr:latest") == ("ghcr.io", "linuxserver/sonarr", "latest"))
chk("ref ghcr", W.split_ref("ghcr.io/a/b:standby") == ("ghcr.io", "a/b", "standby"))
chk("ref registry port", W.split_ref("reg.local:5000/x:1") == ("reg.local:5000", "x", "1"))
chk("ref digest pin", W.split_ref("a/b@sha256:" + "a" * 64) is None)
chk("ref bare id", W.split_ref("sha256:" + "a" * 64) is None and W.split_ref("0123456789ab") is None)
chk("ref localhost", W.split_ref("localhost/x:1") is None)

# --- settings
chk("settings empty", W.parse_settings("")["known"] is False)
chk("settings garbage", W.parse_settings("<<<")["known"] is False)
s = W.parse_settings('{"dockerUpdateAll": "yes", "docker": {"plex": "no", "sonarr": "yes"}}')
chk("settings json", s["known"] and s["all"] is True and s["names"] == {"plex": False, "sonarr": True}, s)
s = W.parse_settings('{"Global": {"docker": ["a", "b"]}}')
chk("settings list", s["known"] and s["names"] == {"a": True, "b": True}, s)
s = W.parse_settings('dockerAll="no"\nfoo="bar"\n')
chk("settings kv", s["known"] and s["all"] is False, s)

# --- classify
st_all = {"known": True, "all": True, "names": {"plex": False}}
chk("compose is manual", W.classify(ctr("x", "i", CP), st_all) == "manual")
chk("no labels manual", W.classify(ctr("x", "i"), st_all) == "manual")
chk("dockerman all-on auto", W.classify(ctr("sonarr", "i", DM), st_all) == "auto")
chk("dockerman excluded manual", W.classify(ctr("plex", "i", DM), st_all) == "manual")
chk("dockerman unknown settings", W.classify(ctr("a", "i", DM), {"known": False, "all": None, "names": {}}) == "unsure")
chk("dockerman listed only", W.classify(ctr("b", "i", DM), {"known": True, "all": None, "names": {"a": True}}) == "manual")

# --- fake registry
D_OLD, D_NEW = "sha256:" + "1" * 64, "sha256:" + "2" * 64
CALLS = []


def make_registry(served, need_token=True):
    """served: {(host, repo, tag): digest}"""
    def head(url, hdr):
        CALLS.append((url, dict(hdr)))
        m = url.split("/v2/", 1)
        host = m[0].replace("https://", "")
        repo, tag = m[1].rsplit("/manifests/", 1)
        if need_token and hdr.get("Authorization") != "Bearer TOK":
            return 401, {"www-authenticate": 'Bearer realm="https://auth.example/token",service="reg",scope="repository:%s:pull"' % repo}
        d = served.get((host, repo, tag))
        return (200, {"docker-content-digest": d}) if d else (404, {})

    def jget(url, headers=None):
        CALLS.append((url, {}))
        if url.startswith("https://auth.example/token"):
            return {"token": "TOK"}
        raise OSError("no net")
    return head, jget


head, jget = make_registry({("registry-1.docker.io", "library/nginx", "1"): D_NEW,
                            ("ghcr.io", "linuxserver/sonarr", "latest"): D_OLD})
chk("behind", W.registry_state(ctr("n", "nginx:1", digests=["nginx@" + D_OLD]), head, jget) == "behind")
chk("current", W.registry_state(ctr("n", "lscr.io/linuxserver/sonarr", digests=["lscr.io/linuxserver/sonarr@" + D_OLD]), head, jget) == "current")
chk("multi-arch accept header sent", any("image.index" in h.get("Accept", "") and "manifest.list" in h.get("Accept", "") for _, h in CALLS))
chk("token requested with repo scope", any("scope=repository%3Alibrary%2Fnginx%3Apull" in u for u, _ in CALLS))
chk("any local digest matches", W.registry_state(ctr("n", "nginx:1", digests=["nginx@" + D_OLD, "nginx@" + D_NEW]), head, jget) == "current")
chk("no RepoDigests unknown", W.registry_state(ctr("n", "nginx:1", digests=[]), head, jget) == "unknown")
chk("registry 404 unknown", W.registry_state(ctr("n", "nginx:9", digests=["nginx@" + D_OLD]), head, jget) == "unknown")


def boom(url, hdr):
    raise OSError("down")


chk("registry down unknown", W.registry_state(ctr("n", "nginx:1", digests=["nginx@" + D_OLD]), boom, jget) == "unknown")
chk("non-https realm refused", W.remote_digest("r", "x", "1", lambda u, h: (401, {"www-authenticate": 'Bearer realm="http://evil/t"'}), jget) is None)

# --- git
SRC = {"org.opencontainers.image.revision": "a" * 40, "org.opencontainers.image.source": "https://github.com/o/r"}
chk("git behind", W.git_state(ctr("g", "loc", img_labels=SRC), lambda u, h=None: {"ahead_by": 12}) == ("behind", 12))
chk("git current", W.git_state(ctr("g", "loc", img_labels=SRC), lambda u, h=None: {"ahead_by": 0}) == ("current", 0))
chk("git no labels", W.git_state(ctr("g", "loc"), lambda u, h=None: {"ahead_by": 3}) == ("unknown", 0))
chk("git private/404", W.git_state(ctr("g", "loc", img_labels=SRC), lambda u, h=None: (_ for _ in ()).throw(OSError())) == ("unknown", 0))
chk("git non-github source", W.git_state(ctr("g", "loc", img_labels=dict(SRC, **{"org.opencontainers.image.source": "https://example.com/o/r"})), lambda u, h=None: {"ahead_by": 3}) == ("unknown", 0))

# --- evaluate + lines
settings = '{"dockerUpdateAll": "yes", "docker": {"plex": "no"}}'
cs = [
    ctr("sonarr", "lscr.io/linuxserver/sonarr", DM, ["lscr.io/linuxserver/sonarr@" + D_OLD]),         # auto: skipped
    ctr("plex", "nginx:1", DM, ["nginx@" + D_OLD]),                                                     # excluded, behind
    ctr("stackapp", "ghcr.io/a/b:standby", CP, ["ghcr.io/a/b@" + D_OLD]),                               # compose, current? no entry -> unknown
    ctr("resell", "resell:local", CP, [], SRC),                                                         # local build, git
    ctr("mystery", "foo:1", None, []),                                                                  # unknown
]
res = W.evaluate(cs, settings, head, lambda u, h=None: {"ahead_by": 12} if "api.github" in u else jget(u, h))
chk("auto counted", res["auto"] == 1, res)
chk("manual behind", res["manual_behind"] == ["plex"], res)
chk("git behind list", res["git_behind"] == [("resell", 12)], res)
chk("unknown list", res["unknown"] == ["mystery", "stackapp"], res)
lines, needs = W.build_lines(res)
chk("lines needs_you", needs is True)
chk("line 1", lines[0] == "1 container need a manual update: plex" or lines[0].startswith("1 container"), lines)
chk("line git", any(l == "1 behind main: resell (12 commits)" for l in lines), lines)
chk("line unknown", any("could not be checked" in l and "mystery" in l for l in lines), lines)
lines2, needs2 = W.build_lines({"manual_behind": [], "git_behind": [], "unknown": [], "unsure": ["a"], "auto": 4, "checked": 2})
chk("all current: no needs", needs2 is False and lines2[0].startswith("No manual updates"), lines2)
chk("unsure line", any("unreadable" in l for l in lines2), lines2)
many = {"manual_behind": ["c%02d" % i for i in range(40)], "git_behind": [], "unknown": [], "unsure": [], "auto": 0, "checked": 40}
ml, _ = W.build_lines(many)
chk("long list capped", len(ml[0]) <= 200 and "more" in ml[0], ml)

# every line must pass the digest's privacy backstop
for l in lines + lines2 + ml:
    chk("private-safe: " + l[:30], not L.PRIVATE_RE.search(l), l)

# --- parse_input + main end to end (dry run and real post into a temp digest dir)
raw = "@@inspect\n" + json.dumps([{"Name": "/plex", "Image": "sha256:plex", "Config": {"Image": "nginx:1", "Labels": DM}}]) + \
      "\n@@images\n" + json.dumps([{"Id": "sha256:plex", "RepoDigests": ["nginx@" + D_OLD]}]) + \
      "\n@@settings\n" + settings + "\n@@end\n"
c, stxt, imgs = W.parse_input(raw)
W.attach_images(c, imgs)
chk("parse input", len(c) == 1 and c[0]["_image"]["RepoDigests"] and json.loads(stxt)["dockerUpdateAll"] == "yes")
try:
    W.parse_input("@@inspect\n{}\n")
    chk("bad inspect rejected", False)
except ValueError:
    chk("bad inspect rejected", True)

with tempfile.TemporaryDirectory() as td:
    os.environ["QUEUE_DIGEST_TOKEN"] = "t" * 32
    os.environ["ERROR_DIGEST_DIR"] = td
    os.environ.pop("DIGEST_LINES_APPS", None)
    status, obj = W.post(["1 container needs a manual update: plex"], True, "2026-11-26",
                         handle=lambda m, p, h, b: L.handle(m, p, h, b, ddir=td, today=__import__("datetime").date(2026, 11, 27)))
    chk("post ok", status == 200 and obj.get("ok"), (status, obj))
    day = L.read_day("2026-11-26", td)
    chk("section stored", day and day[0]["app"] == "docker" and day[0]["title"] == "Docker updates" and day[0]["needs_you"] is True, day)
    status, _ = W.post(["x"], False, "2026-11-26", token="wrong" * 8,
                       handle=lambda m, p, h, b: L.handle(m, p, h, b, ddir=td, today=__import__("datetime").date(2026, 11, 27)))
    chk("wrong token refused", status == 401)

print("%d checks, %d failed" % (N[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
