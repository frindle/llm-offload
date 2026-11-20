"""Tests for the PR-review backtest harness. Fixtures are synthetic and built in a temp dir; no corpus, no queue, no GPU."""
import json, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_corpus, pr_review_integrity, pr_review_score, pr_review_submit


def sh(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], check=True,
                   capture_output=True)


def make_repo(tmp):
    r = tmp / "repo"
    r.mkdir()
    sh(r, "init", "-q")
    (r / "calc.py").write_text("def total(xs):\n    return sum(xs)\n")
    sh(r, "add", "."); sh(r, "commit", "-qm", "base")
    (r / "calc.py").write_text("def total(xs):\n    # sum them\n    s = 0\n    for x in xs[1:]:\n        s += x\n    return s\n")
    sh(r, "commit", "-qam", "Rewrite total as a loop")
    intro = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (r / "calc.py").write_text("def total(xs):\n    # sum them\n    s = 0\n    for x in xs:\n        s += x\n    return s\n")
    sh(r, "commit", "-qam", "Fix total skipping first element")
    fix = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (r / "notes.md").write_text("hello\n")
    sh(r, "add", "."); sh(r, "commit", "-qm", "Add notes")
    clean = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return r, intro, fix, clean


def test_self_check():
    assert pr_review_score.self_check() == 0


def test_code_runs_drops_comments_and_blanks():
    src = ["def f():", "    # note", "    x = 1", "", "    y = 2"]
    assert build_corpus.code_runs(src, 1, 5) == [[1, 1], [3, 3], [5, 5]]


def test_build_score_integrity_end_to_end(tmp_path):
    r, intro, fix, clean = make_repo(tmp_path)
    cur = {"regressions": [{"repo": str(r), "intro": intro, "fix": fix, "class": "boundary",
                            "ranges": {"calc.py": [[2, 4]]}, "summary": "Fix total skipping first element"}],
           "clean": [{"repo": str(r), "commit": clean, "tag": "docs"}]}
    cf = tmp_path / "cur.json"
    cf.write_text(json.dumps(cur))
    out = tmp_path / "corpus"
    build_corpus.main([str(cf), "--out", str(out)])
    items, keys = json.loads((out / "items.json").read_text()), json.loads((out / "keys.json").read_text())
    assert len(items) == 2 and (os.stat(out).st_mode & 0o777) == 0o700
    reg = next(i for i, k in keys.items() if k["type"] == "regression")
    assert keys[reg]["keys"] == [{"file": "calc.py", "lines": [3, 4]}]       # the comment line 2 is not a key line
    assert set(items[reg]) == {"intent", "chars"}                              # nothing key-like reaches the runner
    probs, _ = pr_review_integrity.check(out)
    assert probs == []
    items[reg]["intent"] = "this is the bug here"
    (out / "items.json").write_text(json.dumps(items))
    assert any("answer-key" in p for p in pr_review_integrity.check(out)[0])


def test_same_intro_two_fixes_merge(tmp_path):
    r, intro, fix, clean = make_repo(tmp_path)
    base = {"repo": str(r), "intro": intro, "fix": fix, "class": "x", "summary": "s"}
    cur = {"regressions": [dict(base, ranges={"calc.py": [[3, 3]]}), dict(base, ranges={"calc.py": [[4, 4]]})], "clean": []}
    cf = tmp_path / "c.json"
    cf.write_text(json.dumps(cur))
    build_corpus.main([str(cf), "--out", str(tmp_path / "o")])
    k = json.loads((tmp_path / "o" / "keys.json").read_text())
    assert len(k) == 1 and len(next(iter(k.values()))["keys"]) == 2


def test_plan_is_model_major_and_labels_are_neutral():
    jobs = pr_review_submit.plan(["i000001", "i000002"], ["qwen3:8b", "phi4:14b"], 3)
    assert len(jobs) == 12
    assert [j["model"] for j in jobs[:6]] == ["qwen3:8b"] * 6               # no model swap inside a model's block
    assert all(len(j["label"]) <= 60 and j["label"].startswith("bakeoff-prr-") for j in jobs)
    assert pr_review_submit.ctx_for("phi4:14b") == 16384 and pr_review_submit.ctx_for("qwen3:8b") == 32768
    assert pr_review_submit.ctx_for("x:8b", {"x:8b": 8192}) == 8192


if __name__ == "__main__":   # no pytest needed: run every test_* with a fresh tmp dir
    import inspect, tempfile, traceback
    bad = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td)) if inspect.signature(fn).parameters else fn()
                print("ok  ", name)
            except Exception:
                bad += 1; print("FAIL", name); traceback.print_exc()
    sys.exit(1 if bad else 0)
