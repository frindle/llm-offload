#!/usr/bin/env python3
"""Read-only replay of scope_creep over landed dispatch commits (integrate:/Job:/qagent) and qagent fix metas.
Usage: scope-creep-replay.py [--days 60] [--json]   Nothing is modified; nothing is logged."""
import argparse, json, os, re, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["SCOPE_CREEP_LOG"] = "/dev/null"
import scope_creep as sc

HOME = Path.home(); PROJ = HOME / "Desktop" / "GitHub Projects"
LOGS = HOME / "bin" / "ollama-queue-logs"
ACTED = PROJ / "ollama" / "ollama-handoff" / "acted.json"

def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True).stdout

def repos():
    seen, out = set(), []
    for d in sorted(PROJ.iterdir()):
        if not d.is_dir() or " 2" in d.name or not (d / ".git").exists(): continue
        rp = d.resolve()
        if rp in seen: continue
        seen.add(rp); out.append(d)
    return out

def acted_map():
    try: d = json.loads(ACTED.read_text())
    except Exception: return {}
    m = {}
    for jid, v in (d.items() if isinstance(d, dict) else []):
        if isinstance(v, dict) and v.get("commit"): m[v["commit"][:7]] = jid
    return m

def job_spec_gate(jid):
    spec, gate = "", None
    if not jid: return spec, gate
    try: gate = json.loads((LOGS / f"{jid}.gate.json").read_text())
    except Exception: pass
    try:
        dj = json.loads((LOGS / f"{jid}.done.json").read_text())
        tf = dj.get("task_file")
        if tf and Path(tf).exists(): spec += Path(tf).read_text()
        wt = dj.get("cwd")
        if wt and (Path(wt) / "TASK.md").exists(): spec += Path(wt, "TASK.md").read_text()
    except Exception: pass
    if gate: spec += str(gate.get("task_intent") or "")
    return spec, gate

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=60); ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    am = acted_map(); rows = []
    for r in repos():
        log = git(r, "log", f"--since={a.days} days ago", "--format=%H%x1f%s%x1f%b%x1e")
        for rec in log.split("\x1e"):
            rec = rec.strip()
            if not rec: continue
            h, subj, body = (rec.split("\x1f") + ["", ""])[:3]
            jm = re.search(r"^Job:\s*(\w{12})", body, re.M)
            if not (subj.startswith("integrate:") or jm or "Dispatched fix" in body or h[:7] in am): continue
            parents = git(r, "rev-list", "--parents", "-n1", h).split()
            if len(parents) > 2: continue          # merge commit
            jid = jm.group(1) if jm else am.get(h[:7])
            spec, gate = job_spec_gate(jid)
            diff = git(r, "show", "--format=", "--patch", h)
            res = sc.evaluate(diff, spec, sc.gate_unsigned(gate) if gate else None)
            rows.append({"repo": r.name.replace(".nosync", ""), "sha": h[:7], "subj": subj[:70], "job": jid,
                         "gate": bool(gate), "spec_found": bool(spec), "added": res["added_code_lines"],
                         "flag": res["flag"], "reasons": res["reasons"], "evidence": res["evidence"]})
    # qagent fix metas that landed
    for p in sorted((HOME / ".qagent" / "fix").glob("*.json")):
        try: m = json.loads(p.read_text())
        except Exception: continue
        if not m.get("landed"): continue
        try: diff = Path(m["report"]["patch"]).read_text()
        except Exception: continue
        gate = None
        try: gate = json.loads((LOGS / f"{m['job']}.gate.json").read_text())
        except Exception: pass
        res = sc.evaluate(diff, m.get("problem") or "", sc.gate_unsigned(gate) if gate is not None else sc.gate_unsigned({"verdict": "missing"}))
        rows.append({"repo": Path(m["repo"]).name.replace(".nosync", ""), "sha": m["landed"]["sha"][:7], "subj": "qagent " + m["job"],
                     "job": m["job"], "gate": gate is not None, "spec_found": True, "added": res["added_code_lines"],
                     "flag": res["flag"], "reasons": res["reasons"], "evidence": res["evidence"]})
    seen, uniq = set(), []
    for x in rows:
        k = (x["repo"], x["sha"])
        if k in seen: continue
        seen.add(k); uniq.append(x)
    if a.json: print(json.dumps(uniq, indent=1)); return 0
    print(f"replayed {len(uniq)} landed dispatch commits; flagged {sum(x['flag'] for x in uniq)}")
    from collections import Counter
    def cls(x):
        if not x["gate"]: return "no-gate-record"
        try: g = json.loads((LOGS / f"{x['job']}.gate.json").read_text()).get("verdict")
        except Exception: g = "?"
        return f"gate:{g}"
    c = Counter((cls(x), x["flag"]) for x in uniq)
    for (k, f), n in sorted(c.items()): print(f"  {k:24} {'FLAGGED' if f else 'clean':8} {n}")
    print(f"  spec text found for {sum(x['spec_found'] for x in uniq)}/{len(uniq)} (UNREQUESTED_* abstain without a spec)")
    for x in uniq:
        if x["flag"]: print(f"FLAG {x['repo']} {x['sha']} {x['subj']} +{x['added']} job={x['job']} {x['reasons']}\n     " + "\n     ".join(x["evidence"][:3]))
    return 0
if __name__ == "__main__": sys.exit(main())
