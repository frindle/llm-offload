#!/usr/bin/env python3
"""session-summary-index.py

Index past Claude Code session transcripts as compact, SEARCHABLE vault
notes. DETERMINISTIC extraction only (no LLM). One small note per main
session, written to Claude/SessionIndex/<YYYY-MM-DD>-<sessid[:8]>.md.

The digest is built from metadata only (prompts, file paths, commit subjects,
agent task descriptions, and an in-window handoff passdown). Raw command
OUTPUT is never copied, and any line matching a secret pattern is dropped.

SAFETY: writes to --out <dir> by default. Only touches the live vault
SessionIndex/ when --write is passed. Never edits settings.json or installs
hooks.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
PROJECT_DIR = os.path.join(
    HOME, ".claude", "projects", "-Users-user-Desktop-GitHub-Projects"
)
HANDOFF_DIR = os.path.join(HOME, ".claude", "handoff", "archive")
# --write target directory. Set VAULT_SESSIONINDEX (e.g. the SMB path
# /Volumes/data/Documents/Vault/Claude/SessionIndex) to write files there; unset, --write goes
# through the vault-shim (as --via-shim does, below).
VAULT_SESSIONINDEX = os.environ.get("VAULT_SESSIONINDEX") or ""
# --via-shim: notes are rendered into this local staging mirror (so the unchanged-skip
# logic still works) and every created/updated one is PUT to the vault-shim.
SHIM_STAGE = os.environ.get("VAULT_SESSIONINDEX_STAGE") or os.path.join(
    HOME, ".cache", "session-index-stage"
)
SHIM_NOTE_DIR = "Claude/SessionIndex"


def shim_put(fname: str, content: str, timeout: float = 20) -> None:
    """PUT one note through the shim (URL/token via vault_conf; token never printed)."""
    import urllib.parse
    import urllib.request
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import vault_conf
    tok = vault_conf.vault_token()
    if not tok:
        raise RuntimeError("no vault token (VAULT_TOKEN_FILE / ~/.config/vault/env)")
    url = f"{vault_conf.vault_url()}/vault/{urllib.parse.quote(SHIM_NOTE_DIR + '/' + fname)}"
    req = urllib.request.Request(
        url, data=content.encode(), method="PUT",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "text/markdown"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()

MAX_PROMPT_CHARS = 240
MAX_PROMPTS = 2
MAX_FILES = 12
MAX_COMMITS = 15
MAX_AGENTS = 10
MAX_HANDOFF_LINES = 6
MAX_HANDOFF_LINE_CHARS = 200


def shorten_path(p: str, cwd: str | None = None) -> str:
    """Collapse long, noisy absolute paths so notes stay compact.

    Deterministic string substitution only. Keeps paths searchable while
    dropping per-run temp prefixes.
    """
    if not p:
        return p
    s = p
    if cwd and s.startswith(cwd + "/"):
        s = s[len(cwd) + 1 :]
        return s
    s = re.sub(r"^/private/tmp/claude-[0-9]+/[^/]+/[^/]+/scratchpad/", "$SCRATCH/", s)
    s = re.sub(r"^/private/tmp/claude-[0-9]+/", "$TMP/", s)
    s = re.sub(r"^/(private/)?var/folders/[^/]+/[^/]+/T/", "$TMP/", s)
    s = re.sub(r"^/tmp/", "$TMP/", s)
    if s.startswith(HOME + "/"):
        s = "~/" + s[len(HOME) + 1 :]
    return s

# --- secret redaction -------------------------------------------------------
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"xox[bp]-[A-Za-z0-9-]{8,}"),
    re.compile(r"(?i)\bkeychain\b"),
    re.compile(r"(?i)\btokens?\b\s*[:=]"),
    re.compile(r"(?i)\bpassword\b"),
    re.compile(r"(?i)\bapi[_-]?key\b"),
    re.compile(r"(?i)\bsecret\b\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
]


def line_is_secret(line: str) -> bool:
    return any(p.search(line) for p in SECRET_PATTERNS)


def redact(text: str) -> str:
    """Drop any line matching a secret pattern. Return remaining lines."""
    out = []
    for ln in text.splitlines():
        if line_is_secret(ln):
            out.append("[redacted]")
        else:
            out.append(ln)
    return "\n".join(out)


# --- transcript parsing -----------------------------------------------------
SYSTEM_PREFIXES = (
    "<local-command", "<command-name", "<command-message", "<command-args",
    "<cross-session", "<system-reminder", "<user-", "<bash-", "<local-",
    "caveat:", "another claude session", "[request interrupted", "stop hook",
    "this session is being continued", "<session-", "<task", "<persistent",
)


def msg_text(message: dict) -> str:
    c = message.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def is_genuine_prompt(rec: dict) -> bool:
    if rec.get("type") != "user":
        return False
    if rec.get("isMeta") or rec.get("isSidechain"):
        return False
    c = rec.get("message", {}).get("content")
    # A tool_result-only turn is not a prompt.
    if isinstance(c, list) and not any(
        isinstance(b, dict) and b.get("type") == "text" for b in c
    ):
        return False
    t = msg_text(rec.get("message", {})).strip()
    if not t:
        return False
    low = t.lower()
    if low.startswith(SYSTEM_PREFIXES):
        return False
    return True


def truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
COMMIT_M_RE = re.compile(r"git\s+commit\b[^\n]*?-m\s+(['\"])(.+?)\1", re.DOTALL)


def extract_commit_subjects(cmd: str) -> list[str]:
    """Pull commit subject lines from a bash `git commit` command string.

    Only reads the command the model issued (not any output). Handles
    -m "msg" and `git commit -F - <<'EOF' ... EOF` heredoc forms.
    """
    subjects = []
    for m in COMMIT_M_RE.finditer(cmd):
        subjects.append(m.group(2).splitlines()[0].strip())
    # heredoc-fed commit: subject is first non-empty line after the marker
    if re.search(r"git\s+commit\b[^\n]*<<", cmd):
        hd = HEREDOC_RE.search(cmd)
        if hd:
            marker = hd.group(1)
            after = cmd.split("<<", 1)[1]
            # skip to end of the heredoc opener line
            after = after.split("\n", 1)[1] if "\n" in after else ""
            body_lines = []
            for ln in after.splitlines():
                if ln.strip() == marker:
                    break
                body_lines.append(ln)
            for ln in body_lines:
                cand = ln.strip()
                if not cand:
                    continue
                # skip shell artifacts leaking in from nested substitutions
                if cand.startswith(("$(", "`", "cat ")) or "<<" in cand:
                    continue
                subjects.append(cand)
                break
    # final guard: drop any subject that is clearly a shell fragment
    return [s for s in subjects if not s.startswith(("$(", "`")) and "<<" not in s]


def parse_transcript(path: str) -> dict:
    prompts: list[str] = []
    commits: list[str] = []
    files_touched: list[str] = []
    agents: list[str] = []
    timestamps: list[str] = []
    cwd = None
    session_id = os.path.splitext(os.path.basename(path))[0]

    seen_files = set()
    seen_commits = set()

    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("sessionId"):
                session_id = rec.get("sessionId")
            ts = rec.get("timestamp")
            if ts:
                timestamps.append(ts)
            if cwd is None and rec.get("cwd"):
                cwd = rec.get("cwd")

            if is_genuine_prompt(rec) and len(prompts) < MAX_PROMPTS:
                prompts.append(truncate(msg_text(rec["message"]), MAX_PROMPT_CHARS))

            if rec.get("type") == "assistant":
                content = rec.get("message", {}).get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name = b.get("name")
                    inp = b.get("input") or {}
                    if name in ("Write", "Edit", "NotebookEdit"):
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp and fp not in seen_files:
                            seen_files.add(fp)
                            files_touched.append(fp)
                    elif name == "Bash":
                        cmd = inp.get("command", "")
                        if "git commit" in cmd:
                            for subj in extract_commit_subjects(cmd):
                                if subj and subj not in seen_commits:
                                    seen_commits.add(subj)
                                    commits.append(subj)
                    elif name == "Agent":
                        desc = (inp.get("description") or "").strip()
                        sub = (inp.get("subagent_type") or "").strip()
                        if desc:
                            agents.append(f"{desc} [{sub}]" if sub else desc)

    first_ts = min(timestamps) if timestamps else None
    last_ts = max(timestamps) if timestamps else None
    project = os.path.basename(cwd) if cwd else "unknown"
    return {
        "session_id": session_id,
        "project": project,
        "cwd": cwd,
        "prompts": prompts,
        "commits": commits[:MAX_COMMITS],
        "files": files_touched[:MAX_FILES],
        "files_total": len(files_touched),
        "agents": agents[:MAX_AGENTS],
        "agents_total": len(agents),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


# --- handoff passdown matching ---------------------------------------------
HANDOFF_FN_RE = re.compile(r"^(\d{8}T\d{6}Z)-\d+\.md$")


def load_handoffs() -> list[tuple[datetime, str]]:
    out = []
    for p in glob.glob(os.path.join(HANDOFF_DIR, "*.md")):
        m = HANDOFF_FN_RE.match(os.path.basename(p))
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        out.append((dt, p))
    out.sort()
    return out


def parse_iso(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def matching_handoff(first_ts, last_ts, handoffs):
    a = parse_iso(first_ts)
    b = parse_iso(last_ts)
    if not a or not b:
        return None
    for dt, path in handoffs:
        if a <= dt <= b:
            return path
    return None


def handoff_quote(path: str) -> list[str]:
    lines = []
    with open(path, "r", errors="replace") as fh:
        for raw in fh:
            s = raw.rstrip("\n")
            if not s.strip():
                continue
            if s.lstrip().startswith(("---", "#")) and not lines:
                # skip leading frontmatter/heading noise but keep headings once content starts
                if s.lstrip().startswith("---"):
                    continue
            lines.append(s)
            if len(lines) >= MAX_HANDOFF_LINES:
                break
    return lines


# --- note rendering ---------------------------------------------------------
def source_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def note_date(first_ts, mtime) -> str:
    dt = parse_iso(first_ts)
    if dt:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d")


def render_note(data: dict, transcript_path: str, handoffs) -> tuple[str, str]:
    mtime = os.path.getmtime(transcript_path)
    mtime_iso = datetime.fromtimestamp(mtime, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    shash = source_hash(transcript_path)
    date = note_date(data["first_ts"], mtime)
    sid = data["session_id"]
    fname = f"{date}-{sid[:8]}.md"

    fm = [
        "---",
        f"date: {date}",
        f"session_id: {sid}",
        f"project: {data['project']}",
        f"transcript_mtime: {mtime_iso}",
        f"source_hash: {shash}",
        f"span_start: {data['first_ts'] or ''}",
        f"span_end: {data['last_ts'] or ''}",
        "tags: [session-index]",
        "---",
    ]

    body = [f"# Session {sid[:8]} — {date}", ""]

    if data["prompts"]:
        body.append("## Prompt(s)")
        for p in data["prompts"]:
            clean = redact(p).replace("\n", " ").strip()
            body.append(f"> {clean}")
        body.append("")

    hp = matching_handoff(data["first_ts"], data["last_ts"], handoffs)
    if hp:
        rel = os.path.basename(hp)
        body.append("## Handoff passdown (best summary)")
        body.append(f"[[{rel}]] — `{hp}`")
        for ln in handoff_quote(hp):
            r = truncate(redact(ln).strip(), MAX_HANDOFF_LINE_CHARS)
            if r:
                body.append(f"> {r}")
        body.append("")

    if data["commits"]:
        body.append("## Commits")
        for c in data["commits"]:
            r = redact(c).strip()
            if r:
                body.append(f"- {r}")
        body.append("")

    if data["agents"]:
        body.append(f"## Agents dispatched ({data['agents_total']})")
        for a in data["agents"]:
            r = redact(a).strip()
            if r:
                body.append(f"- {r}")
        if data["agents_total"] > len(data["agents"]):
            body.append(f"- … (+{data['agents_total'] - len(data['agents'])} more)")
        body.append("")

    if data["files"]:
        body.append(f"## Files written/edited ({data['files_total']})")
        for f in data["files"]:
            r = redact(shorten_path(f, data.get("cwd"))).strip()
            if r:
                body.append(f"- `{r}`")
        if data["files_total"] > len(data["files"]):
            body.append(f"- … (+{data['files_total'] - len(data['files'])} more)")
        body.append("")

    content = "\n".join(fm) + "\n\n" + "\n".join(body).rstrip() + "\n"
    # final safety pass: redact whole document line-by-line
    content = redact(content)
    return fname, content


def read_existing_meta(path: str) -> dict:
    meta = {}
    try:
        with open(path, "r", errors="replace") as fh:
            in_fm = False
            for ln in fh:
                s = ln.rstrip("\n")
                if s == "---":
                    if in_fm:
                        break
                    in_fm = True
                    continue
                if in_fm and ":" in s:
                    k, _, v = s.partition(":")
                    meta[k.strip()] = v.strip()
    except FileNotFoundError:
        return {}
    return meta


# --- main -------------------------------------------------------------------
def process(path: str, out_dir: str, handoffs, force: bool, on_write=None) -> str:
    """Returns one of: created, updated, skip."""
    data = parse_transcript(path)
    fname, content = render_note(data, path, handoffs)
    target = os.path.join(out_dir, fname)

    new_hash = None
    for ln in content.splitlines():
        if ln.startswith("source_hash:"):
            new_hash = ln.split(":", 1)[1].strip()
        if ln.startswith("transcript_mtime:"):
            new_mtime = ln.split(":", 1)[1].strip()

    if os.path.exists(target) and not force:
        old = read_existing_meta(target)
        if old.get("source_hash") == new_hash and old.get(
            "transcript_mtime"
        ) == new_mtime:
            return "skip"

    action = "updated" if os.path.exists(target) else "created"
    os.makedirs(out_dir, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(content)
    os.replace(tmp, target)
    if on_write:
        try:
            on_write(fname, content)
        except Exception:
            # Un-stage so the next run retries instead of skipping an un-uploaded note.
            try:
                os.remove(target)
            except OSError:
                pass
            raise
    return action


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="output directory (verification target)")
    ap.add_argument(
        "--write",
        action="store_true",
        help="write into the live vault SessionIndex/ (overrides --out); through the vault-shim "
        "unless VAULT_SESSIONINDEX names a directory",
    )
    ap.add_argument(
        "--via-shim",
        action="store_true",
        help="with --write: always go through the vault-shim, even with VAULT_SESSIONINDEX set "
        "(stage locally, then PUT created/updated notes)",
    )
    ap.add_argument("--session", help="index only this session id (or filename)")
    ap.add_argument(
        "--force", action="store_true", help="re-write even if unchanged"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report create/update counts without writing",
    )
    ap.add_argument(
        "--project-dir",
        default=PROJECT_DIR,
        help="transcript directory (default: github-projects)",
    )
    args = ap.parse_args()

    on_write = None
    if args.write and (args.via_shim or not VAULT_SESSIONINDEX):
        out_dir = SHIM_STAGE
        on_write = shim_put
    elif args.write:
        out_dir = VAULT_SESSIONINDEX
    elif args.out:
        out_dir = os.path.expanduser(args.out)
    elif args.dry_run:
        # compare against the write target for an accurate count (the shim's staging mirror if unset)
        out_dir = VAULT_SESSIONINDEX or SHIM_STAGE
    else:
        ap.error("one of --out, --write, or --dry-run is required")

    transcripts = sorted(glob.glob(os.path.join(args.project_dir, "*.jsonl")))
    if args.session:
        want = args.session
        transcripts = [
            t
            for t in transcripts
            if want in os.path.basename(t)
        ]
        if not transcripts:
            print(f"no transcript matches session {want!r}", file=sys.stderr)
            return 2

    handoffs = load_handoffs()
    counts = {"created": 0, "updated": 0, "skip": 0}

    for t in transcripts:
        if os.path.getsize(t) == 0:
            continue
        if args.dry_run:
            data = parse_transcript(t)
            fname, content = render_note(data, t, handoffs)
            target = os.path.join(out_dir, fname)
            new_hash = new_mtime = None
            for ln in content.splitlines():
                if ln.startswith("source_hash:"):
                    new_hash = ln.split(":", 1)[1].strip()
                elif ln.startswith("transcript_mtime:"):
                    new_mtime = ln.split(":", 1)[1].strip()
            if not os.path.exists(target):
                counts["created"] += 1
            else:
                old = read_existing_meta(target)
                if old.get("source_hash") == new_hash and old.get(
                    "transcript_mtime"
                ) == new_mtime and not args.force:
                    counts["skip"] += 1
                else:
                    counts["updated"] += 1
            continue

        action = process(t, out_dir, handoffs, args.force, on_write)
        counts[action] += 1
        if action != "skip":
            print(f"{action}: {os.path.basename(t)}")

    mode = "DRY-RUN" if args.dry_run else ("LIVE VAULT" if args.write else "TEMP")
    print(
        f"\n[{mode}] out={out_dir}\n"
        f"created={counts['created']} updated={counts['updated']} "
        f"skipped(no-op)={counts['skip']} total={sum(counts.values())}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
