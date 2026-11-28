#!/usr/bin/env python3
"""docker_update_watch: which docker containers need a MANUAL update, as a section of the daily error digest (2026-11-27).

Runs inside the queue container (docker exec ... python3 docker_update_watch.py), fed by a host-side script that has the
docker CLI. stdin is plain text in three sections, each opened by a marker line:
  @@inspect    the JSON array printed by `docker inspect` for every running container
  @@images     the JSON array printed by `docker image inspect` for their images
  @@settings   the raw text of the CA Auto Update plugin's settings file(s); may be empty
  @@end
It decides per container whether Unraid keeps it up to date by itself, and for the rest whether a newer image exists:

  auto    dockerman container (label net.unraid.docker.managed=dockerman) that the plugin is set to update: skipped.
  manual  compose container (label com.docker.compose.project), a dockerman container the plugin does not update, or
          a container with neither label. A newer image is looked for in two ways:
            registry: the local RepoDigests against the registry's manifest digest for the same tag (HEAD with
                      index + manifest Accept types, so a multi-arch tag compares the list digest; anonymous token
                      from the registry's own WWW-Authenticate challenge). Hub, ghcr and lscr (-> ghcr).
            git:      an image built locally has no RepoDigests; when it carries org.opencontainers.image.revision and
                      .source (a public GitHub repo), the revision is compared with that repo's main (compare API).
          Anything else (private repo, no labels, pinned @sha256, registry down) counts as "unknown".

Output is lines for the app "docker" (digest_lines.py): container names and counts only; no hosts, ips or paths.
  python3 docker_update_watch.py [--dry-run] [--day YYYY-MM-DD]      --dry-run prints the lines and posts nothing
The post goes through digest_lines.handle in this process (same token, same privacy backstop, same store as the
route). The app "docker" must be in DIGEST_LINES_APPS or the post is refused.
"""
import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

APP = "docker"
TIMEOUT = 15
LABEL_MANAGED = "net.unraid.docker.managed"
LABEL_COMPOSE = "com.docker.compose.project"
MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
MAX_NAMES = 12            # names listed on one line before "and N more"
MAX_LINE = 190            # digest_lines allows 200


# ---------------------------------------------------------------------------------------------------- input
def parse_input(text):
    """-> (containers list, settings text, images list). Raises ValueError when a JSON section is not an array."""
    secs, cur = {}, None
    for ln in (text or "").splitlines():
        if ln.startswith("@@"):
            cur = ln[2:].strip()
            secs[cur] = []
            continue
        if cur and cur != "end":
            secs[cur].append(ln)
    out = []
    for k in ("inspect", "images"):
        d = json.loads("\n".join(secs.get(k, [])) or "[]")
        if not isinstance(d, list):
            raise ValueError("%s is not a list" % k)
        out.append(d)
    return out[0], "\n".join(secs.get("settings", [])), out[1]


# ---------------------------------------------------------------------------------------------------- auto-update
_YES = {"yes", "true", "1", "on", "enabled"}
_NO = {"no", "false", "0", "off", "disabled", ""}


def _truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in _YES


def _falsy(v):
    return not _truthy(v) and (isinstance(v, (bool, int, float)) or str(v).strip().lower() in _NO)


def parse_settings(text):
    """CA Auto Update's docker settings -> {"known": bool, "all": bool|None, "names": {name: bool}}.

    The plugin's file layout is not documented where this was written (no host access), so this is tolerant: a JSON
    object, or KEY="value" lines. "all" comes from a key that mentions docker plus all/global; per-container choices
    from a key that mentions docker/container/app holding either a list of names (updated) or {name: yes/no}.
    known=False when nothing recognisable was found: the caller then says so instead of guessing."""
    out = {"known": False, "all": None, "names": {}}
    txt = (text or "").strip()
    if not txt:
        return out
    obj = None
    try:
        obj = json.loads(txt)
    except ValueError:
        kv = {}
        for ln in txt.splitlines():
            m = re.match(r'\s*([A-Za-z0-9_.-]+)\s*=\s*"?([^"]*)"?\s*$', ln)
            if m:
                kv[m.group(1)] = m.group(2)
        obj = kv or None
    if not isinstance(obj, dict):
        return out

    def walk(d, depth=0):
        for k, v in d.items():
            kl = str(k).lower()
            if isinstance(v, dict) and depth < 3:
                if re.search(r"docker|container|app", kl) and v and all(isinstance(x, (str, bool, int)) for x in v.values()):
                    for n, val in v.items():
                        if _truthy(val) or _falsy(val):
                            out["names"][str(n)] = _truthy(val)
                            out["known"] = True
                else:
                    walk(v, depth + 1)
            elif isinstance(v, list) and re.search(r"docker|container|app", kl) and all(isinstance(x, str) for x in v):
                for n in v:
                    out["names"][n] = True
                out["known"] = True
            elif re.search(r"docker", kl) and re.search(r"all|global", kl) and not isinstance(v, (dict, list)):
                if _truthy(v) or _falsy(v):
                    out["all"] = _truthy(v)
                    out["known"] = True
    walk(obj)
    return out


def classify(c, settings):
    """-> "auto" | "manual" | "unsure" (dockerman container, settings unreadable)."""
    labels = (c.get("Config") or {}).get("Labels") or {}
    if labels.get(LABEL_COMPOSE):
        return "manual"
    if labels.get(LABEL_MANAGED) != "dockerman":
        return "manual"
    if not settings.get("known"):
        return "unsure"
    name = (c.get("Name") or "").lstrip("/")
    if name in settings["names"]:
        return "auto" if settings["names"][name] else "manual"
    if settings["all"] is True:
        return "auto"
    if settings["all"] is False:
        return "manual"
    return "manual" if settings["names"] else "unsure"


# ---------------------------------------------------------------------------------------------------- registry
def split_ref(image):
    """'lscr.io/linuxserver/x:1' -> (registry host, repository, tag) or None when it cannot be checked (digest pin,
    bare image id, no tag information)."""
    if not image or "@" in image or re.fullmatch(r"(sha256:)?[0-9a-f]{12,64}", image):
        return None
    parts = image.split("/")
    host = "registry-1.docker.io"
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        host, parts = parts[0], parts[1:]
    rest = "/".join(parts)
    tag = "latest"
    if ":" in parts[-1]:
        rest, tag = rest.rsplit(":", 1)
    if host in ("docker.io", "index.docker.io"):
        host = "registry-1.docker.io"
    if host == "lscr.io":
        host = "ghcr.io"
    if host == "registry-1.docker.io" and "/" not in rest:
        rest = "library/" + rest
    if host in ("localhost",) or host.startswith("localhost:"):
        return None
    return host, rest, tag


def http_head(url, headers):
    """-> (status, headers dict lowercased). Never raises on HTTP errors; raises OSError on network trouble."""
    req = urllib.request.Request(url, method="HEAD", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}


def http_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read() or b"{}")


def remote_digest(host, repo, tag, head=http_head, jget=http_json):
    """Digest of the manifest (or manifest list) the registry serves for host/repo:tag, or None."""
    url = "https://%s/v2/%s/manifests/%s" % (host, repo, urllib.parse.quote(tag, safe=""))
    hdr = {"Accept": MANIFEST_ACCEPT, "User-Agent": "docker-update-watch"}
    st, h = head(url, hdr)
    if st in (401, 403):
        ch = h.get("www-authenticate", "")
        m = re.match(r"(?i)bearer\s+(.*)", ch)
        if not m:
            return None
        kv = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if not kv.get("realm", "").startswith("https://"):
            return None
        q = {"service": kv.get("service", ""), "scope": kv.get("scope") or "repository:%s:pull" % repo}
        tok = jget(kv["realm"] + "?" + urllib.parse.urlencode(q))
        t = tok.get("token") or tok.get("access_token")
        if not t:
            return None
        hdr["Authorization"] = "Bearer " + t
        st, h = head(url, hdr)
    if st == 200:
        d = h.get("docker-content-digest", "")
        return d if d.startswith("sha256:") else None
    return None


def registry_state(c, head=http_head, jget=http_json):
    """-> "current" | "behind" | "unknown"."""
    cfg = c.get("Config") or {}
    ref = split_ref(cfg.get("Image") or "")
    img = c.get("_image") or {}
    local = img.get("RepoDigests") or []
    if not ref or not local:
        return "unknown"
    host, repo, tag = ref
    try:
        want = remote_digest(host, repo, tag, head, jget)
    except (OSError, ValueError):
        return "unknown"
    if not want:
        return "unknown"
    mine = [d.split("@", 1)[1] for d in local if "@" in d]
    if not mine:
        return "unknown"
    return "current" if want in mine else "behind"


# ---------------------------------------------------------------------------------------------------- git (locally built)
def git_state(c, jget=http_json):
    """-> ("current", 0) | ("behind", n) | ("unknown", 0). Public GitHub repos only."""
    labels = (c.get("Config") or {}).get("Labels") or {}
    labels = dict(labels, **((c.get("_image") or {}).get("Labels") or {}))
    rev = labels.get("org.opencontainers.image.revision") or ""
    src = labels.get("org.opencontainers.image.source") or ""
    m = re.match(r"https?://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", src)
    if not m or not re.fullmatch(r"[0-9a-f]{7,40}", rev):
        return "unknown", 0
    try:
        d = jget("https://api.github.com/repos/%s/compare/%s...main" % (m.group(1), rev),
                 {"Accept": "application/vnd.github+json", "User-Agent": "docker-update-watch"})
    except (OSError, ValueError):
        return "unknown", 0
    n = d.get("ahead_by")
    if not isinstance(n, int):
        return "unknown", 0
    return ("behind", n) if n > 0 else ("current", 0)


# ---------------------------------------------------------------------------------------------------- run
def evaluate(containers, settings_text, head=http_head, jget=http_json):
    """-> dict: manual_behind [names], git_behind [(name, n)], unknown [names], auto n, unsure [names], settings_known."""
    st = parse_settings(settings_text)
    res = {"manual_behind": [], "git_behind": [], "unknown": [], "unsure": [], "auto": 0, "checked": 0,
           "settings_known": st["known"]}
    for c in containers:
        name = (c.get("Name") or "").lstrip("/")
        if not name:
            continue
        kind = classify(c, st)
        if kind == "auto":
            res["auto"] += 1
            continue
        if kind == "unsure":
            res["unsure"].append(name)
            continue
        res["checked"] += 1
        state = registry_state(c, head, jget)
        if state == "behind":
            res["manual_behind"].append(name)
        elif state == "unknown":
            g, n = git_state(c, jget)
            if g == "behind":
                res["git_behind"].append((name, n))
            elif g == "unknown":
                res["unknown"].append(name)
    for k in ("manual_behind", "unknown", "unsure"):
        res[k].sort()
    res["git_behind"].sort()
    return res


def _names(names):
    shown = names[:MAX_NAMES]
    s = ", ".join(shown)
    if len(names) > len(shown):
        s += " and %d more" % (len(names) - len(shown))
    return s


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def build_lines(res):
    """-> (lines, needs_you)."""
    lines = []
    mb, gb = res["manual_behind"], res["git_behind"]
    if mb:
        lines.append(("%s need a manual update: %s" % (_plural(len(mb), "container"), _names(mb)))[:MAX_LINE])
    if gb:
        lines.append(("%s behind main: %s" % (len(gb), ", ".join("%s (%s)" % (n, _plural(k, "commit")) for n, k in gb[:MAX_NAMES])))[:MAX_LINE])
    if not mb and not gb:
        lines.append("No manual updates needed (%d checked by hand, %d auto-updated by Unraid)" % (res["checked"], res["auto"]))
    if res["unknown"]:
        lines.append(("%s could not be checked (no registry or git info): %s" % (len(res["unknown"]), _names(res["unknown"])))[:MAX_LINE])
    if res["unsure"]:
        lines.append(("Auto-update settings unreadable, not checked: %s" % _names(res["unsure"]))[:MAX_LINE])
    return lines[:10], bool(mb or gb)


def attach_images(containers, images):
    """Join each container to its image record (docker image inspect) by image id, as c["_image"]."""
    by = {str(i.get("Id")): i for i in images if isinstance(i, dict)}
    for c in containers:
        c["_image"] = by.get(str(c.get("Image") or ""), {})
    return containers


def post(lines, needs_you, day, handle=None, token=None):
    import digest_lines
    tok = token if token is not None else digest_lines.token()
    handle = handle or digest_lines.handle
    body = json.dumps({"app": APP, "day": day, "lines": lines, "needs_you": needs_you}).encode()
    return handle("POST", digest_lines.ROUTE, {"Authorization": "Bearer " + tok}, body)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--day")
    a = ap.parse_args(argv)
    text = sys.stdin.read()
    try:
        containers, settings_text, images = parse_input(text)
    except ValueError as e:
        print("docker-update-watch: bad input (%s)" % type(e).__name__)
        return 2
    attach_images(containers, images)
    res = evaluate(containers, settings_text)
    lines, needs = build_lines(res)
    day = a.day or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    print("docker-update-watch: %d containers, %d auto, %d checked, %d behind, %d unknown, settings %s" % (
        len(containers), res["auto"], res["checked"], len(res["manual_behind"]) + len(res["git_behind"]),
        len(res["unknown"]), "read" if res["settings_known"] else "unreadable"))
    if a.dry_run:
        for ln in lines:
            print("  " + ln)
        print("  needs_you=%s" % str(needs).lower())
        return 0
    status, obj = post(lines, needs, day)
    print("docker-update-watch: post %s %s" % (status, obj.get("error", "ok") if isinstance(obj, dict) else ""))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
