#!/usr/bin/env python3
"""handoff-triage.py -- triage the Ollama handoff panel and clear routine items.

Pipeline per completed handoff item (from `handoff-emit.py --json`):
  1. Deterministic rules (status/gate/flags/live bundle/known-obsolete/git landed).
  2. Gemma (Darkbloom, direct, one request at a time) as a CLASSIFIER only, with a
     strict JSON answer and a GROUNDING check: evidence_quote must be a verbatim
     substring of the page, else the answer is treated as needs-action.
  3. Auto-clear only when BOTH the deterministic rule says safe AND gemma says
     routine-done/landed/obsolete with a grounded quote. Anything else is listed.

Default is --dry-run. --apply clears via `handoff-emit.py --acted ... --reason ...`
in small batches. Results are appended to a JSONL (resumable; re-run skips done ids).

Usage:
  handoff-triage.py                  # dry run: classify + print plan
  handoff-triage.py --apply          # clear what the plan says is safe
  handoff-triage.py --no-llm         # deterministic only (gemma not called)
  handoff-triage.py --sanity ID...   # classify given ids, print gemma raw verdicts
  handoff-triage.py --no-llm --apply # deterministic-only clearing (only obsolete set)

Docs: handoff panel = `handoff-emit.py` (pages under ollama/ollama-handoff/{pending,complete},
state in ollama-handoff/acted.json). Clear tools are NOT unified -- see memory note
reference_ollama_pipeline_clear_tools_not_unified.md.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
BIN = HOME / "bin"
EMIT = BIN / "handoff-emit.py"
STATE = BIN / "ollama-queue-state.json"
RESULTS = HOME / ".ollama-dispatch" / "triage" / "handoff-triage.jsonl"
BASE = "http://127.0.0.1:8000/v1"
MODEL = "gemma-4-26b-qat-4bit"
SAMPLING = {"temperature": 0.3, "top_p": 0.9, "max_tokens": 2048}

OBSOLETE_PATTERNS = ("rt-egift-link-s1", "rt-order-mismatch-port",
                     "rt-order-buyerid-patchable-port", "rt-costco-login-confirm-port",
                     "rt-bfmr-tls-fingerprint", "resell-bfmr-link-feedback",
                     "bfmr-link-sync-feedback")
OBSOLETE_REASON = ("superseded: completed by hand and pushed to resell-tracker main "
                   "(9a97d88 7bd1411 c3581f8 661749a 8300b24 b6d12e8)")
SAFE_GATES = ("PASS", "preflight-verify-ok (baseline fails as designed)")
CLASSES = ("routine-done", "landed", "obsolete", "needs-action")
MERGE_WORDS = ("landed", "land in", "in main", "to main", "deployed", "shipped to", "merg")


# ---------------------------------------------------------------- deterministic
def is_obsolete(item):
    hay = (item.get("label") or "") + " " + (item.get("cwd") or "")
    return any(p in hay for p in OBSOLETE_PATTERNS)


def live_bundles(state):
    """Bundles with any non-terminal job (queued/running/needs_opus/...)."""
    jobs = state.get("jobs", [])
    jobs = list(jobs.values()) if isinstance(jobs, dict) else jobs
    live = set()
    for j in jobs:
        if j.get("bundle") and j.get("status") not in ("done", "failed", "cancelled", "superseded"):
            live.add(j["bundle"])
    return live, {j["id"]: j.get("bundle") for j in jobs}


try:
    from docsum_private import is_private_label
except ImportError:  # script copied elsewhere (tests): same definition, kept in step by test_docsum_ui_consumers
    import re as _re_dp

    def is_private_label(label):
        return _re_dp.fullmatch(r"docsum-[0-9a-f]{8}", str(label or "")) is not None


def hard_exclusion(item, bundle, live):
    if is_private_label(item.get("label")):
        return "private docsum job"
    if item.get("awaiting_signoff"):
        return "awaiting sign-off"
    if item.get("code_high"):
        return "code-high findings"
    if item.get("untrusted"):
        return "untrusted"
    if item.get("status") != "done":
        return "status " + str(item.get("status"))
    g = item.get("gate") or ""
    if g not in SAFE_GATES:
        return "gate: " + g
    if item.get("not_checked"):
        return "not_checked>0"
    if bundle and bundle in live:
        return "live bundle " + bundle
    return None


def git(*args, cwd=None):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def repo_of(cwd):
    g = Path(cwd) / ".git"
    if not g.exists():
        return None
    if g.is_file():
        m = re.match(r"gitdir:\s*(.+)", g.read_text().strip())
        if not m:
            return None
        gd = Path(m.group(1))
        # <repo>/.git/worktrees/<name>
        return str(gd.parent.parent.parent) if gd.parent.name == "worktrees" else None
    return str(cwd)


def landed_fraction(page_text, cwd):
    """Read-only: fraction of the diff's added lines present in the default branch's
    version of each touched file. None if it cannot be determined."""
    repo = repo_of(cwd) if cwd and os.path.isdir(cwd) else None
    if not repo:
        return None
    ref = None
    for cand in ("main", "master"):
        if git("rev-parse", "--verify", "-q", cand, cwd=repo)[0] == 0:
            ref = cand
            break
    if not ref:
        return None
    diff = page_text.split("```diff", 1)
    if len(diff) < 2:
        return None
    cur, adds = None, {}
    for ln in diff[1].splitlines():
        m = re.match(r"diff --git a/(.+) b/(.+)", ln)
        if m:
            cur = m.group(2)
            adds.setdefault(cur, [])
        elif cur and ln.startswith("+") and not ln.startswith("+++"):
            s = ln[1:].strip()
            if len(s) >= 6:
                adds[cur].append(s)
    total = hit = 0
    for f, lines in adds.items():
        rc, out = git("show", f"{ref}:{f}", cwd=repo)
        have = {x.strip() for x in out.splitlines()} if rc == 0 else set()
        for s in lines:
            total += 1
            hit += s in have
    return (hit / total) if total else None


# ------------------------------------------------------------------- Darkbloom
def api_key():
    try:                      # W2: sticky-cached key via the one adapter; fall through to the direct read
        import darkbloom_chat as _dbk
        _k = _dbk.api_key()
        if _k:
            return _k
    except Exception:         # noqa: BLE001
        pass
    d = json.load(open(HOME / ".darkbloom" / "local.json"))
    for k in ("api_key", "key", "token", "apiKey"):
        if d.get(k):
            return d[k]
    for v in d.values():
        if isinstance(v, str) and len(v) > 16:
            return v
    raise SystemExit("no key in local.json")


def http(path, body=None, retries=8):
    key = api_key()
    for n in range(retries):
        req = urllib.request.Request(
            BASE + path, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 502, 500):
                time.sleep(min(60, 3 * (n + 1)))
                continue
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(min(60, 5 * (n + 1)))  # Darkbloom may be restarting
    raise RuntimeError("Darkbloom unavailable after retries")


def model_served():
    try:
        ids = [m["id"] for m in http("/models", retries=2).get("data", [])]
    except Exception:
        return False
    return MODEL in ids


SYSTEM = (
    "You triage finished local-model dispatch reports. You are a CLASSIFIER. "
    "Answer with ONE JSON object and nothing else: "
    '{"class": "routine-done|landed|obsolete|needs-action", "evidence_quote": "...", "reason": "..."}. '
    "Classes: routine-done = a normal completed job whose report shows a clean finished result "
    "and nothing asking for human action; landed = the report itself states the change was "
    "already applied/merged elsewhere; obsolete = the report itself says it is superseded/stale; "
    "needs-action = ANY warning, TODO, failure, unverified/unchecked item, open question, error, "
    "suspicious or incomplete diff, or you are unsure. When unsure choose needs-action. "
    "evidence_quote MUST be copied CHARACTER-FOR-CHARACTER from the report: ONE single line only "
    "(never span a line break), under 160 chars, keeping every markdown symbol exactly as written "
    "(asterisks, backticks, pipes). Example of a valid quote: **status**: `done`  (exit 0). "
    "Do not paraphrase. Do not invent text. Do not think aloud.")


def parse_json(txt):
    txt = txt.strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def clip(page):
    return page if len(page) <= 14000 else page[:9000] + "\n...[clipped]...\n" + page[-4000:]


def classify(page):
    body = {"model": MODEL, **SAMPLING,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "REPORT:\n<<<\n" + clip(page) + "\n>>>"}]}
    resp = http("/chat/completions", body)
    txt = resp["choices"][0]["message"].get("content") or ""
    return txt


def grounded(ans, page):
    if not isinstance(ans, dict) or ans.get("class") not in CLASSES:
        return {"class": "needs-action", "grounded": False, "reason": "unparseable/invalid class"}
    q = ans.get("evidence_quote")
    ok = isinstance(q, str) and len(q.strip()) >= 8 and q in page and "\n" not in q
    out = {"class": ans["class"], "grounded": ok, "quote": q, "reason": ans.get("reason", "")}
    if not ok:
        out["class"] = "needs-action"
        out["reason"] = "ungrounded quote; model said " + str(ans.get("class"))
    return out


# ------------------------------------------------------------------------ main
def load_done(path):
    done = {}
    if path.exists():
        for ln in path.read_text().splitlines():
            try:
                r = json.loads(ln)
                done[r["id"]] = r
            except Exception:
                pass
    return done


def emit_json():
    return json.loads(subprocess.run([sys.executable, str(EMIT), "--json"], capture_output=True,
                                     text=True, check=True).stdout)


def clear(ids, reason):
    r = subprocess.run([sys.executable, str(EMIT), "--acted", *ids, "--reason", reason],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="default")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--sanity", nargs="+", metavar="ID")
    ap.add_argument("--results", default=str(RESULTS))
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--pace", type=float, default=0.5)
    ap.add_argument("--json-out", help="write the final plan here")
    a = ap.parse_args()
    results = Path(a.results)
    results.parent.mkdir(parents=True, exist_ok=True)

    data = emit_json()
    out_dir = Path(data["out_dir"])
    items = data["complete"]
    state = json.load(open(STATE))
    live, bundle_of = live_bundles(state)
    by_cwd_pass = {}
    for i in items:
        if i.get("gate") == "PASS" and i.get("status") == "done":
            by_cwd_pass.setdefault(i["cwd"], []).append(i["id"])

    def page_of(i):
        return (out_dir / i["page"]).read_text(errors="replace")

    if a.sanity:
        if not model_served():
            raise SystemExit(f"{MODEL} not served; stop")
        for i in items:
            if i["id"] in a.sanity:
                page = page_of(i)
                raw = classify(page)
                print(i["id"], i["label"], "|", i["gate"])
                print("  ->", json.dumps(grounded(parse_json(raw), page))[:400])
                time.sleep(a.pace)
        return 0

    use_llm = not a.no_llm
    if use_llm and not model_served():
        raise SystemExit(f"{MODEL} is not served by Darkbloom; stopping (use --no-llm)")
    prior = load_done(results)

    plan = {"obsolete": [], "clear": [], "left": []}
    for i in items:
        iid = i["id"]
        bundle = bundle_of.get(iid)
        if is_obsolete(i):
            if i.get("awaiting_signoff"):
                plan["left"].append((i, "obsolete-pattern but awaiting sign-off"))
            else:
                plan["obsolete"].append(i)
            continue
        why = hard_exclusion(i, bundle, live)
        if why:
            plan["left"].append((i, why))
            continue
        try:
            page = page_of(i)
        except OSError:
            plan["left"].append((i, "handoff page file missing (tooling gap)"))
            continue
        frac = landed_fraction(page, i["cwd"])
        det = "ok"
        if i["gate"] != "PASS":
            # author-phase artifact: need a downstream PASS gate row or git evidence
            downstream = [x for x in by_cwd_pass.get(i["cwd"], []) if x != iid]
            if not downstream and not (frac is not None and frac >= 0.95):
                plan["left"].append((i, "author-phase: no downstream gate row / not in default branch"))
                continue
        if not use_llm:
            plan["left"].append((i, "deterministic-ok but no LLM confirmation"))
            continue
        rec = prior.get(iid)
        if not rec or rec.get("model") != MODEL:
            raw = classify(page)
            g = grounded(parse_json(raw), page)
            rec = {"id": iid, "model": MODEL, "ts": time.time(), "verdict": g, "landed_frac": frac}
            with results.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            time.sleep(a.pace)
        v = rec["verdict"]
        if v["class"] in ("routine-done", "landed", "obsolete") and v.get("grounded"):
            if v["class"] == "landed" and not (frac is not None and frac >= 0.9):
                plan["left"].append((i, "gemma=landed but git shows not in default branch"))
            else:
                plan["clear"].append((i, v))
        else:
            plan["left"].append((i, f"gemma={v['class']}: {v.get('reason','')[:100]}"))

    print(f"obsolete={len(plan['obsolete'])} clear={len(plan['clear'])} left={len(plan['left'])}")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps({
            "obsolete": [i["id"] for i in plan["obsolete"]],
            "clear": [(i["id"], v) for i, v in plan["clear"]],
            "left": [(i["id"], i["label"], w) for i, w in plan["left"]]}, indent=1))
    if not a.apply:
        print("(dry run; --apply to clear)")
        return 0

    def run(ids, reason):
        for k in range(0, len(ids), a.batch):
            rc, out = clear(ids[k:k + a.batch], reason)
            print(f"[{rc}] {len(ids[k:k+a.batch])} cleared" if rc == 0 else out)

    run([i["id"] for i in plan["obsolete"]], OBSOLETE_REASON)
    groups = {}
    for i, v in plan["clear"]:
        r = ("triage: gemma-4-26b classified routine, gate " +
             ("PASS" if i["gate"] == "PASS" else "preflight-verify-ok with downstream gate/default-branch evidence") +
             f" ({v['class']})")
        assert not any(w in r.lower() for w in MERGE_WORDS), r
        groups.setdefault(r, []).append(i["id"])
    for r, ids in groups.items():
        run(ids, r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
