"""Generic tool-layer guards for local-model coding runs (2026-10-13; diagnosis of A/B job
psab-C1-a465e147cf00: 8 identical web_search calls for a question about the repo's own Prisma schema,
zero verify runs, 30-iteration cap).  Three guards, profile/mode-agnostic, default ON, kill switch
WORKER_LOCAL_GUARDS=0 (restores the pre-guard behaviour byte-for-byte):

  1. repeated_search  -- the same (normalised) web_search query a 3rd time (N=2 allowed) returns a tool
                         ERROR telling the model the question concerns THIS repository, naming files.
  2. local_redirect   -- a web_search whose query names an identifier/file that exists in the worktree
                         gets a 'found locally at <path>: read_file it' hint prepended (search still runs).
  3. verify_nudge     -- K=2 consecutive successful edits/writes with no run of the task's verify command
                         -> ONE firm nudge appended to the next tool result (per edit streak).
                         task_complete without a verify since the last edit is already gated by the
                         worker's claim gate (which runs the verify itself); here it is only COUNTED
                         (event complete_unverified) so the A/B report sees it.

Every firing is a named event: logged `[worker] LOCAL-GUARD event=<name> ...` and counted in
Guards.events (the worker copies it into _dispatch_metrics["local_guard_events"])."""
import os
import re
import subprocess

EDIT_STREAK_K = 2
SEARCH_REPEAT_N = 2
_STOP = {"the", "and", "for", "with", "that", "this", "from", "how", "what", "does", "have", "has",
         "not", "are", "use", "using", "when", "why", "error", "typescript", "javascript", "python"}
_SKIP_DIRS = ("node_modules", ".git", "dist", "build", ".next", "__pycache__", "venv", ".venv")


def enabled(env=None):
    return (env if env is not None else os.environ).get("WORKER_LOCAL_GUARDS", "1").strip() != "0"


def normalize_query(q):
    toks = re.findall(r"[a-z0-9_.]+", str(q).lower())
    return " ".join(sorted(set(t for t in toks if t not in _STOP)))


def _identifiers(query):
    """Code-like tokens only: camelCase / snake_case / dotted-or-pathed file names. Plain words never qualify."""
    out = []
    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{3,}", str(query)):
        t = t.strip("./-")
        if len(t) < 4 or t.lower() in _STOP:
            continue
        if (re.search(r"[a-z][A-Z]", t) or "_" in t or re.search(r"\.[A-Za-z]{1,5}$", t) or "/" in t):
            if t not in out:
                out.append(t)
    return out[:4]


def _find_local(cwd, ident, timeout=2.0):
    """Cheap bounded lookup: a path whose basename matches, else files containing the word."""
    hits = []
    base = os.path.basename(ident)
    try:
        r = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=cwd,
                           capture_output=True, text=True, timeout=timeout)
        files = r.stdout.splitlines()[:50000] if r.returncode == 0 else []
    except Exception:
        files = []
    if files:
        # A path-shaped ident ("app/api/import/route.ts") must match by PATH: matching its
        # basename returned every route.ts in the repo (psc-C4, 2026-10-13). Basename match
        # is only for a bare filename ident.
        if "/" in ident:
            hits += [f for f in files if f == ident or f.endswith("/" + ident)][:3]
        else:
            hits += [f for f in files if os.path.basename(f) == base or f == ident][:3]
    if not hits and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ident):
        cmd = ["grep", "-rIlw", "-m", "1"] + [a for d in _SKIP_DIRS for a in ("--exclude-dir=" + d,)] + [ident, "."]
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
            hits += [x[2:] if x.startswith("./") else x for x in r.stdout.splitlines()[:3]]
        except Exception:
            pass
    return hits


def local_hits(cwd, query):
    """[(identifier, [paths])] for identifiers in the query that resolve in the worktree, plus schema files."""
    found = []
    q = str(query).lower()
    if "prisma" in q:
        for cand in ("prisma/schema.prisma", "schema.prisma"):
            if os.path.isfile(os.path.join(cwd, cand)):
                found.append(("prisma", [cand]))
                break
    for ident in _identifiers(query):
        h = _find_local(cwd, ident)
        if h:
            found.append((ident, h))
    return found


def _hint(found):
    return "; ".join(f"{i} found locally at {', '.join(p)}" for i, p in found)


class Guards:
    def __init__(self, verify=None, env=None, log=None):
        self.on = enabled(env)
        self.verify = verify
        self.log = log or (lambda s: None)
        self.events = {}
        self.queries = {}
        self.streak = 0
        self.pending = False
        self.nudged = False
        self.unverified = False          # an edit happened with no verify since

    def _ev(self, name, detail=""):
        self.events[name] = self.events.get(name, 0) + 1
        self.log(f"[worker] LOCAL-GUARD event={name} n={self.events[name]} {detail}".rstrip())

    # -- guards 1 + 2 -------------------------------------------------------------------------
    def web_search(self, cwd, args):
        """-> (blocked_result_or_None, hint_prefix). Call before running the tool."""
        if not self.on or not isinstance(args, dict) or not args.get("query"):
            return None, ""
        q = args["query"]
        key = normalize_query(q)
        self.queries[key] = self.queries.get(key, 0) + 1
        try:
            found = local_hits(cwd, q)
        except Exception:
            found = []
        if self.queries[key] > SEARCH_REPEAT_N:
            self._ev("repeated_search_blocked", f"query={str(q)[:80]!r} count={self.queries[key]}")
            files = ("Likely files: " + "; ".join(f"{i} -> {', '.join(p)}" for i, p in found) + ". ") if found else ""
            return (f"ERROR: you have already issued this web_search {self.queries[key] - 1} times. This question "
                    f"concerns THIS repository, not the internet -- web search cannot answer it. Use read_file "
                    f"(or run_bash grep -rn) on the repo's own files instead. {files}"
                    f"Read the real file, then act on what it says."), ""
        if found:
            self._ev("local_redirect", _hint(found)[:120])
            return None, f"[LOCAL HINT: {_hint(found)}: read_file it before relying on web results.]\n"
        return None, ""

    # -- guard 3 ------------------------------------------------------------------------------
    def after_tool(self, name, result, is_own_verify, verify_cmd=None):
        """Call after every executed tool; returns a nudge string to append to THIS result, or ''."""
        if not self.on:
            return ""
        r = str(result)
        nudge = ""
        if self.pending and not self.nudged and not is_own_verify and name not in ("write_file", "edit_file"):
            self.nudged = True
            self.pending = False
            cmd = verify_cmd or self.verify
            self._ev("verify_nudge", f"streak={self.streak}")
            nudge = (f"\n\n[HARNESS: you have made {self.streak} edits without running the verify. STOP editing. "
                     f"Run `{cmd}` NOW with run_bash and fix what it reports before any more edits.]")
        if is_own_verify:
            self.streak, self.pending, self.nudged, self.unverified = 0, False, False, False
        elif name in ("write_file", "edit_file") and r.startswith("OK"):
            self.unverified = True
            self.streak += 1
            if self.streak >= EDIT_STREAK_K and not self.nudged and (verify_cmd or self.verify):
                self.pending = True
        return nudge

    def on_task_complete(self):
        if self.on and self.unverified and self.verify:
            self._ev("complete_unverified", "task_complete with no verify run since the last edit (claim gate runs it)")
