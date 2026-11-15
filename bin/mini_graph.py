#!/usr/bin/env python3
"""mini_graph: read-only queries over a graphify graph.json (plain Python, no graphify CLI, never writes).
Loaded by path (importlib) from mini-spec and mini-swe-runner.py. Self-test: python3 ~/bin/mini_graph.py --self-test

Graph facts: top-level directed:false, nodes, links, built_at_commit. Node: id, label ('f()'), source_file (repo-relative),
source_location ('L194', start line only), community. Link relations: contains, calls, imports, imports_from, indirect_call;
source -> target keeps caller -> callee. One edge per caller/callee pair. graphify-out/manifest.json = {relpath: {...}}.
Function END lines are NOT in the graph: callers take them from the runner's functions().
A graph is only trusted for a file when it is `fresh` (see fresh()); otherwise callers fall back to their text rules."""
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path

CALL_RELS = ("calls", "indirect_call")
IMPORT_RELS = ("imports", "imports_from")
IN_RELS = CALL_RELS + IMPORT_RELS


def _git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)


def locate_graph(wt, override=None):
    """Path of graphify-out/graph.json: spec key `graph` (override) wins; else the MAIN checkout (parent of the git common dir),
    since worktrees have no graphify-out of their own. None when absent."""
    if override:
        p = Path(os.path.expanduser(str(override)))
        return p if p.is_file() else None
    r = _git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    p = Path(r.stdout.strip()).parent / "graphify-out" / "graph.json"
    return p if p.is_file() else None


def load(path):
    """Index a graph.json: by_id, file_fns[file] = sorted [(start, id, name)], out[id]/inn[id] = {relation: [ids]}, file_node[file]."""
    path = Path(path)
    raw = json.loads(path.read_text())
    G = dict(path=str(path), built_at_commit=raw.get("built_at_commit") or "", by_id={}, file_fns={}, out={}, inn={}, file_node={}, manifest=set())
    for n in raw.get("nodes", []):
        G["by_id"][n["id"]] = n
    for n in sorted(raw.get("nodes", []), key=lambda n: n["id"]):
        f = n.get("source_file")
        if not f:
            continue
        name = re.sub(r"\(\)$", "", n.get("label", ""))
        if name == Path(f).name or n["id"] == _file_id(f):
            G["file_node"].setdefault(f, n["id"])
        loc = re.match(r"L(\d+)$", str(n.get("source_location") or ""))
        if n.get("_callable") and loc and name != Path(f).name:
            G["file_fns"].setdefault(f, []).append((int(loc.group(1)), n["id"], name))
    for f in G["file_fns"]:
        G["file_fns"][f].sort()
    for l in raw.get("links", []):
        s, t, rel = l.get("source"), l.get("target"), l.get("relation")
        if s in G["by_id"] and t in G["by_id"]:
            G["out"].setdefault(s, {}).setdefault(rel, []).append(t)
            G["inn"].setdefault(t, {}).setdefault(rel, []).append(s)
    try:
        m = json.loads((path.parent / "manifest.json").read_text())
        G["manifest"] = set(m)
    except (OSError, ValueError):
        pass
    return G


def _file_id(f):
    return re.sub(r"[^a-z0-9]+", "_", str(Path(f).with_suffix("")).lower()).strip("_")


def fresh(G, wt, f):
    """True when the graph describes file f as it is now: f in manifest AND built_at_commit exists in wt AND f is unchanged since it.
    Never compares mtimes."""
    sha = G.get("built_at_commit")
    if not sha or f not in G["manifest"]:
        return False
    if _git(wt, "cat-file", "-e", sha + "^{commit}").returncode != 0:
        return False
    return _git(wt, "diff", "--quiet", sha, "--", f).returncode == 0


def fresh_text(G, wt, f, text):
    """Like fresh(), for callers that run AFTER the working tree was edited (the runner's gates): f counts as fresh when it is in the
    manifest, built_at_commit exists in wt, and the blob at that commit equals `text` (the baseline text). Never compares mtimes."""
    sha = G.get("built_at_commit")
    if not sha or f not in G["manifest"] or text is None:
        return False
    if _git(wt, "cat-file", "-e", sha + "^{commit}").returncode != 0:
        return False
    r = _git(wt, "show", f"{sha}:{f}")
    return r.returncode == 0 and r.stdout == text


def status(G, wt, files):
    """`graph: fresh@<sha7> | stale [files] | absent` for the files a feature relied on."""
    if not G:
        return "graph: absent"
    bad = sorted(f for f in files if not fresh(G, wt, f))
    return f"graph: stale {bad}" if bad else f"graph: fresh@{G['built_at_commit'][:7]}"


def node_for(G, f, fn):
    """Node id of function `fn` in file `f` (first by line when names collide: same-name functions in one file share an id), else None."""
    for _start, nid, name in G["file_fns"].get(f, []):
        if name == fn:
            return nid
    return None


def node_at(G, f, line):
    """Id of the graph function starting closest at/before `line` (start lines only; callers confirm with real spans)."""
    best = None
    for start, nid, _n in G["file_fns"].get(f, []):
        if start <= line:
            best = nid
    return best


def _nbrs(adj, nid, rels):
    return sorted({x for r in rels for x in adj.get(nid, {}).get(r, [])})


def callers(G, ids):
    """Sorted ids that call/indirect_call any of ids."""
    return sorted({c for i in ids for c in _nbrs(G["inn"], i, CALL_RELS)} - set(ids))


def callees(G, ids):
    return sorted({c for i in ids for c in _nbrs(G["out"], i, CALL_RELS)} - set(ids))


def importers(G, f, ids):
    """[(source_file, source_id, relation)] sorted, for every in-edge (calls/indirect_call/imports/imports_from) onto ids or f's file node."""
    targets = list(ids)
    if f in G["file_node"]:
        targets.append(G["file_node"][f])
    out = set()
    for t in targets:
        for rel in IN_RELS:
            for s in G["inn"].get(t, {}).get(rel, []):
                sf = G["by_id"][s].get("source_file")
                if sf and sf != f:
                    out.add((sf, s, rel))
    return sorted(out)


def community_peers(G, ids, limit=12):
    """Callable nodes sharing a community with ids (excluding ids), as sorted (file, name, id)."""
    comms = {G["by_id"][i].get("community") for i in ids if i in G["by_id"]} - {None}
    peers = [(n.get("source_file") or "", re.sub(r"\(\)$", "", n.get("label", "")), n["id"]) for n in G["by_id"].values()
             if n.get("_callable") and n.get("community") in comms and n["id"] not in ids]
    return sorted(peers)[:limit]


def shortest_path(G, a, b, rels=CALL_RELS):
    """Deterministic BFS a -> b over `rels` (caller -> callee), neighbours visited in sorted id order. List of ids, or None."""
    if a not in G["by_id"] or b not in G["by_id"]:
        return None
    prev, q = {a: None}, deque([a])
    while q:
        x = q.popleft()
        if x == b:
            path = []
            while x is not None:
                path.append(x)
                x = prev[x]
            return path[::-1]
        for y in _nbrs(G["out"], x, rels):
            if y not in prev:
                prev[y] = x
                q.append(y)
    return None


# ---- live lookup (mini-graph CLI) and pre-submit freshness -------------------------------------------------------

MAX_LINES = 40


def _label(G, nid):
    n = G["by_id"].get(nid, {})
    loc = re.match(r"L(\d+)$", str(n.get("source_location") or ""))
    return f"{n.get('source_file') or '?'}:{loc.group(1) if loc else '?'} {re.sub(r'[(][)]$', '', n.get('label', nid))}"


def _cap(lines, limit=MAX_LINES):
    if len(lines) > limit:
        return lines[:limit - 1] + [f"... ({len(lines) - limit + 1} more lines cut)"]
    return lines


def lookup(G, wt, cmd, args, limit=MAX_LINES):
    """Output lines of one mini-graph subcommand: callers FILE FN | callees FILE FN | where NAME | neighbors FILE. Read-only.
    Files that are not fresh print `STALE: graph not trusted for <file>, use grep` instead of graph facts."""
    stale = lambda f: [f"STALE: graph not trusted for {f}, use grep"]
    if cmd in ("callers", "callees"):
        f, fn = args
        if not fresh(G, wt, f):
            return stale(f)
        nid = node_for(G, f, fn)
        if not nid:
            return [f"no function {fn} in {f} in the graph (use grep)"]
        ids = (callers if cmd == "callers" else callees)(G, [nid])
        rows = sorted(_label(G, i) for i in ids)
        return _cap([f"{cmd} of {f}:{fn} ({len(rows)}):"] + (rows or ["  (none in graph)"]), limit)
    if cmd == "where":
        name = args[0]
        hits = sorted({_label(G, n["id"]) for n in G["by_id"].values()
                       if name.lower() in re.sub(r"[(][)]$", "", n.get("label", "")).lower() and n.get("source_file")})
        # a hit in a stale file is shown with a marker, never as a trusted fact
        out = []
        for h in hits:
            f = h.split(":", 1)[0]
            out.append(h if fresh(G, wt, f) else h + "  [STALE: verify with grep]")
        return _cap([f"where {name} ({len(out)}):"] + (out or ["  (no match)"]), limit)
    if cmd == "neighbors":
        f = args[0]
        if not fresh(G, wt, f):
            return stale(f)
        ids = [i for _s, i, _n in G["file_fns"].get(f, [])]
        imp = sorted({(sf, rel) for sf, _s, rel in importers(G, f, ids)})
        peers = sorted({(pf, pn) for pf, pn, _i in community_peers(G, ids, limit=200) if pf != f})
        rows = [f"neighbors of {f}:", "importers/callers:"] + [f"  {sf} ({rel})" for sf, rel in imp] + (["  (none)"] if not imp else [])
        rows += ["community peers:"] + [f"  {pf}:{pn}" for pf, pn in peers] + (["  (none)"] if not peers else [])
        return _cap(rows, limit)
    raise ValueError(f"unknown subcommand {cmd}")


def submit_warning(G, wt, files, wtroot=None):
    """One-line pre-submit WARNING (or '') when the graph is missing or untrusted for any of files. Pure: reads git only."""
    files = sorted({f for f in files if f})
    if not files:
        return ""
    root = wtroot or wt
    fix = f"cd {root} && graphify update ."
    if not G:
        return f"WARNING: no graphify graph for this repo ({len(files)} file(s) untrusted, mini-spec/runner fall back to grep); to build: {fix}"
    bad = [f for f in files if not fresh(G, wt, f)]
    if not bad:
        return ""
    return f"WARNING: graph stale for {len(bad)} of {len(files)} target file(s) ({', '.join(bad[:4])}{'...' if len(bad) > 4 else ''}); refresh: {fix}"


def cli(argv, wt=None):
    import argparse
    ap = argparse.ArgumentParser(prog="mini-graph", description="read-only graphify lookups (never writes, never runs graphify)")
    ap.add_argument("--graph", help="graph.json override (env MINI_GRAPH_JSON, or MINI_GRAPH when it ends in .json)")
    ap.add_argument("cmd", choices=["callers", "callees", "where", "neighbors"])
    ap.add_argument("args", nargs="+")
    a = ap.parse_args(argv)
    need = {"callers": 2, "callees": 2, "where": 1, "neighbors": 1}[a.cmd]
    if len(a.args) != need:
        ap.error(f"{a.cmd} takes {need} argument(s)")
    env = os.environ.get("MINI_GRAPH", "")
    ov = a.graph or os.environ.get("MINI_GRAPH_JSON") or (env if env.endswith(".json") else None)
    wt = wt or os.getcwd()
    gp = locate_graph(wt, ov)
    if not gp:
        print("no graph for this repo: use grep")
        return 0
    try:
        G = load(gp)
    except (OSError, ValueError):
        print("graph unreadable: use grep")
        return 0
    top = _git(wt, "rev-parse", "--show-toplevel").stdout.strip() or wt
    rel = lambda x: os.path.relpath(os.path.realpath(x), os.path.realpath(top)) if os.path.isabs(x) else x
    a.args = [rel(x) for x in a.args] if a.cmd != "where" else a.args
    print("\n".join(lookup(G, wt, a.cmd, a.args)))
    return 0


# ---- self-test ----------------------------------------------------------------------------------------------------

REAL = Path.home() / "Desktop" / "GitHub Projects" / "resell-tracker.nosync" / "graphify-out" / "graph.json"


def _fixture(td):
    wt = Path(td) / "repo"
    wt.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (wt / "src").mkdir()
    (wt / "src/a.js").write_text("function f1() {}\nfunction f2() { f1(); }\n")
    (wt / "src/b.js").write_text("const { f2 } = require('./a');\nfunction g() { f2(); }\n")
    (wt / "test-a.js").write_text("require('./src/a');\n")

    def run(*a):
        subprocess.run(["git", *a], cwd=wt, check=True, capture_output=True, env=env)
    run("init", "-q")
    run("add", "-A")
    run("commit", "-qm", "one")
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    node = lambda i, lab, f, loc, c=1, call=True: {"id": i, "label": lab, "source_file": f, "source_location": loc, "_callable": call, "community": c}
    nodes = [node("a", "a.js", "src/a.js", "L1", call=False), node("a_f1", "f1()", "src/a.js", "L1"), node("a_f2", "f2()", "src/a.js", "L2"),
             node("b", "b.js", "src/b.js", "L1", call=False), node("b_g", "g()", "src/b.js", "L2", 2),
             node("t", "test-a.js", "test-a.js", "L1", call=False), node("a_f3", "f3()", "src/a.js", "L3")]
    L = lambda s, t, r: {"source": s, "target": t, "relation": r}
    links = [L("a", "a_f1", "contains"), L("a", "a_f2", "contains"), L("a_f2", "a_f1", "calls"), L("b_g", "a_f2", "calls"),
             L("b", "a", "imports_from"), L("t", "a", "imports_from"), L("a_f2", "a_f3", "calls"), L("a_f1", "a_f3", "calls"),
             L("b_g", "a_f1", "calls"), L("b_g", "a_f3", "calls")]
    out = Path(td) / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(dict(directed=False, nodes=nodes, links=links, built_at_commit=sha)))
    (out / "manifest.json").write_text(json.dumps({"src/a.js": {}, "src/b.js": {}, "test-a.js": {}}))
    return wt, out / "graph.json", run


def self_test():
    with tempfile.TemporaryDirectory() as td:
        wt, gp, run = _fixture(td)
        G = load(gp)
        assert locate_graph(wt) is None  # fixture graph lives outside the repo's parent/graphify-out? parent of .git is wt itself
        assert locate_graph(wt, gp) == gp and locate_graph(wt, str(gp) + ".nope") is None
        assert fresh(G, wt, "src/a.js") and status(G, wt, ["src/a.js"]).startswith("graph: fresh@")
        assert not fresh(G, wt, "unlisted.js")
        # dirty file goes stale, others stay fresh
        (wt / "src/a.js").write_text("function f1() { return 1; }\n")
        assert not fresh(G, wt, "src/a.js") and fresh(G, wt, "src/b.js") and status(G, wt, ["src/a.js", "src/b.js"]) == "graph: stale ['src/a.js']"
        base = _git(wt, "show", "HEAD:src/a.js").stdout
        assert fresh_text(G, wt, "src/a.js", base) and not fresh_text(G, wt, "src/a.js", base + "x") and not fresh_text(G, wt, "nope.js", base)
        run("checkout", "--", "src/a.js")
        assert fresh(G, wt, "src/a.js")
        # a commit touching the file goes stale; an unrelated commit stays fresh
        (wt / "README").write_text("x\n")
        run("add", "-A")
        run("commit", "-qm", "unrelated")
        assert fresh(G, wt, "src/a.js")
        (wt / "src/a.js").write_text("function f1() { return 2; }\nfunction f2() { f1(); }\n")
        run("commit", "-qam", "change a")
        assert not fresh(G, wt, "src/a.js") and fresh(G, wt, "src/b.js")
        G2 = dict(G, built_at_commit="0" * 40)  # unknown commit
        assert not fresh(G2, wt, "src/b.js") and status(None, wt, []) == "graph: absent"
        # lookups
        assert node_for(G, "src/a.js", "f2") == "a_f2" and node_for(G, "src/a.js", "nope") is None and node_at(G, "src/a.js", 2) == "a_f2"
        assert G["file_node"]["src/a.js"] == "a" and callers(G, ["a_f1"]) == ["a_f2", "b_g"] and callees(G, ["a_f2"]) == ["a_f1", "a_f3"]
        assert importers(G, "src/a.js", ["a_f2"]) == [("src/b.js", "b", "imports_from"), ("src/b.js", "b_g", "calls"), ("test-a.js", "t", "imports_from")], \
            importers(G, "src/a.js", ["a_f2"])
        assert [p[1] for p in community_peers(G, ["a_f1"])] == ["f2", "f3"]
        # deterministic BFS: two equal-length routes b_g -> a_f3; sorted neighbours pick the same one every time
        p = shortest_path(G, "b_g", "a_f3")
        assert p == ["b_g", "a_f3"] and shortest_path(G, "b_g", "a_f1") == ["b_g", "a_f1"] and shortest_path(G, "a_f1", "b_g") is None
        assert shortest_path(G, "b_g", "a_f3", ("imports",)) is None
        G3 = load(gp)
        G3["out"]["b_g"]["calls"] = ["a_f2", "a_f3", "a_f1"]  # reordering the adjacency must not change the answer
        assert shortest_path(G3, "b_g", "a_f3") == p
    if REAL.is_file():
        R = load(REAL)
        hc = node_for(R, "sidecar/src/poll.js", "handleCommand")
        assert hc == "sidecar_src_poll_handlecommand", hc
        assert "sidecar_src_lib_patchcommand" in callees(R, [hc]), "handleCommand callees"
        assert "sidecar_src_poll_pollonce" in callers(R, [hc]), "handleCommand callers"
        print("real-graph smoke ok:", len(R["by_id"]), "nodes")
    else:
        print("real graph absent: smoke skipped")
    print("mini_graph self-test ok")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if len(sys.argv) > 1 and sys.argv[1] != "-h":
        sys.exit(cli(sys.argv[1:]))
    print(__doc__)
