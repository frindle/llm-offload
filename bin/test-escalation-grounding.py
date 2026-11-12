#!/usr/bin/env python3
"""test-escalation-grounding.py -- the GROUNDING rule for escalation reviews (policy v1).

Positive / negative / near-miss for escalation_grounding.py, plus the self-heal wiring:
an UNVERIFIED reviewer b/c/d verdict is NOT acted on (fresh re-diagnosis once, then needs-opus
with the UNVERIFIED label), a grounded one drives the ladder exactly as before, and
legacy (unstamped) / mechanical / (a) reviews are untouched. Hermetic: temp dirs only.
Usage: test-escalation-grounding.py [--bin DIR]      prints ESC_GROUNDING_TEST_OK
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
os.environ.pop("ESC_ACTIONS", None)
os.environ.pop("ESC_GROUNDING", None)
RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "\n     got=%r\n    want=%r" % (got, want)))


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=str(Path.home() / "bin"))
    a = ap.parse_args()
    BIN = Path(a.bin)
    sys.path.insert(0, str(BIN))
    ev = load(BIN / "escalation_verdict.py", "ev_g")
    sys.modules["escalation_verdict"] = ev
    eg = load(BIN / "escalation_grounding.py", "eg_g")
    sys.modules["escalation_grounding"] = eg
    heal = load(BIN / "dispatch-self-heal.py", "dsh_g")
    td = Path(tempfile.mkdtemp(prefix="escground-test-"))
    ev.KILL_FILE = td / "ACTIONS-OFF"
    ev.CONFIG_PATH = td / "cfg.json"
    wt = td / "wt"
    wt.mkdir()
    # the file the reviewer cites: the REAL defect of job 2f4d18945f75 (nonexistent target.session)
    (wt / "refimpl.py").write_text("\n".join([
        "import os", "def maintain(session=None):", "    try:",
        "        s = session or target.session", "        return s.get()",
        "    except Exception:", "        return (0, [])", "", "def hashLink(x):", "    return x",
        "", "", "", "", "", "# far away marker line: totally_unrelated_text()"]) + "\n")
    ctx = td / "ctx.md"
    ctx.write_text("- worktree (TASK.md, verify.sh live here): `%s`\n" % wt)
    ctx_text = ctx.read_text()
    REPRO = ("REPRO CMD: python3 auto-harness-check.py\nREPRO OUTPUT:\n```\nTraceback (most recent call last):\n"
             "  File \"refimpl.py\", line 4, in maintain\nNameError: name 'target' is not defined\n```\n")
    ESC = 'ESC_RESULT: {"verdict": "%s", "confidence": "high", "why": "x"}\n'

    def rv(verdict, claim_lines, repro=REPRO, stamped=True):
        t = "VERDICT: %s -- cause\n\n%s\n%s%s" % (verdict, "\n".join(claim_lines), repro, ESC % verdict)
        return eg.stamp(t) if stamped else t

    GOOD = "- refimpl.py:4 `s = session or target.session` -- reads an attribute that does not exist (NameError)"
    BAD = "- refimpl.py:4 `bare names resolve to the original function` -- all cases return (0, [])"

    # ------------------------------------------------------------ module: positive
    r = eg.assess(rv("b", [GOOD]), ctx_text)
    check("P1 quote at the cited line, repro names the exception -> GROUNDED", (r["status"], r["grounded"]), ("GROUNDED", 1))
    r = eg.assess(rv("b", ["- refimpl.py:6 `s = session or target.session` -- two lines off is tolerated (NameError)"]), ctx_text)
    check("P2 quote within +-2 lines of the cited line is grounded", r["status"], "GROUNDED")
    j = ('VERDICT: c -- x\nESC_RESULT: {"verdict": "c", "claims": [{"claim": "k", "file": "refimpl.py", "line": 4, '
         '"quote": "session or target.session"}], "repro": {"cmd": "python3 x.py", "output": "NameError: boom"}}\n')
    r = eg.assess(j, ctx_text)
    check("P3 structured ESC_RESULT claims + repro object are accepted", r["status"], "GROUNDED")
    check("P4 absolute path citation works", eg.assess(rv("b", ["- %s:4 `target.session` -- NameError" % (wt / "refimpl.py")]), "")["status"], "GROUNDED")
    # ------------------------------------------------------------ module: negative
    r = eg.assess(rv("b", [BAD]), ctx_text)
    check("N1 a quote that is not in the file is dropped -> UNVERIFIED", (r["status"], r["claims"][0]["code"]), ("UNVERIFIED", "quote-absent"))
    check("N2 prose with no file:line+quote at all -> UNVERIFIED",
          eg.assess("VERDICT: b -- spec\nESC_RESULT: {\"verdict\": \"b\"}\n", ctx_text, require_repro=False)["status"], "UNVERIFIED")
    r = eg.assess(rv("b", ["- nowhere.py:4 `target.session` -- file does not exist"]), ctx_text)
    check("N3 cited file missing -> no-file, UNVERIFIED", (r["status"], r["claims"][0]["code"]), ("UNVERIFIED", "no-file"))
    r = eg.assess(rv("b", ["- /etc/pf.conf:1 `Default PF configuration` -- outside HOME"]), ctx_text)
    check("N4 a path outside HOME/tmp is never opened", (r["status"], r["claims"][0]["code"]), ("UNVERIFIED", "no-file"))
    r = eg.assess(rv("b", [GOOD], repro=""), ctx_text)
    check("N5 no REPRO recorded -> UNVERIFIED even with a grounded claim", (r["status"], r["repro_ok"]), ("UNVERIFIED", False))
    r = eg.assess(rv("b", [GOOD], repro="REPRO CMD: bash verify.sh\nREPRO OUTPUT:\n```\nTraceback\nNameError: name 'target' is not defined\n```\n").replace("NameError", "NameError", 1).replace(" (NameError)", ""), ctx_text)
    check("N6 repro's first exception NameError not addressed by any claim -> UNVERIFIED",
          (r["status"], r["repro_ok"]), ("UNVERIFIED", False))
    # ------------------------------------------------------------ module: near-miss
    r = eg.assess(rv("b", ["- refimpl.py:9 `hasLink(x)` -- hasLink vs hashLink precedent"]), ctx_text)
    check("M1 hasLink vs hashLink: a one-letter-off quote is NOT grounded", r["status"], "UNVERIFIED")
    r = eg.assess(rv("b", ["- refimpl.py:4 `totally_unrelated_text()` -- right text, wrong line"]), ctx_text)
    check("M2 quote exists in the file but far from the cited line -> wrong-line", (r["status"], r["claims"][0]["code"]), ("UNVERIFIED", "wrong-line"))
    r = eg.assess(rv("b", ["- refimpl.py:2 `i  =  1       ` -- trivial quote"]), ctx_text)
    check("M3 a trivially short quote proves nothing", (r["status"], r["claims"][0]["code"]), ("UNVERIFIED", "short-quote"))
    r = eg.assess(rv("b", [GOOD, BAD]), ctx_text)
    check("M4 one grounded + one fabricated claim: verdict stands, fabricated one dropped",
          (r["status"], r["grounded"], r["ungrounded"]), ("GROUNDED", 1, 1))
    san = eg.sanitize(rv("b", [GOOD, BAD]), r)
    check("M5 sanitize drops only the ungrounded claim line", ("[dropped: ungrounded" in san, "target.session" in san, "resolve to the original" in san), (True, True, False))
    check("M6 mechanical / (a) / no-verdict reviews are EXEMPT",
          [eg.assess(t, ctx_text)["status"] for t in ("VERDICT: a -- done", "no verdict at all")], ["EXEMPT", "EXEMPT"])
    # the two real wrong reviews (frozen text): must be UNVERIFIED
    wrong1 = ('VERDICT: b -- The task spec/intent is under-specified: the refimpl calls deluge_login() and get_all_torrents() as bare '
              'module-level names, but the test fixture patches them on target. The fixture sets target.get_all_torrents = stub.get_all_torrents '
              "but the refimpl's bare name resolves to the original function that returns None, causing all cases to return (0, []).\n\n"
              'ESC_RESULT: {"verdict": "b", "confidence": "high", "why": "refimpl uses bare deluge_login()/get_all_torrents() but fixture patches on target"}\n')
    check("R1 job 2f4d18945f75 review (real, wrong) is UNVERIFIED", eg.assess(eg.stamp(wrong1), ctx_text)["status"], "UNVERIFIED")
    wrong2 = ('VERDICT: b -- The harness\'s must_contain literal check incorrectly requires the byte-literal `aria-label="payment me` '
              'while the spec asks for a regex; the literal is still demanded by TASK.md.\nESC_RESULT: {"verdict": "b", "confidence": "high", "why": "x"}\n')
    check("R2 cardlink s2 review (stale byte-literal claim) is UNVERIFIED", eg.assess(eg.stamp(wrong2), ctx_text)["status"], "UNVERIFIED")

    # ------------------------------------------------------------ wiring: self-heal
    (wt / "AUTO-TASK.md").write_text("task\n")
    led = td / "self-heal.json"
    dec = td / "decisions.jsonl"
    heal.DECISIONS = dec
    runs = td / "slice-runs"
    runs.mkdir()
    esc = td                       # ledger_path.parent == td: review files + watcher ledger live here
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    enq, alerts = [], []

    def stub_enqueue(argv):
        enq.append(argv)
        return "bbbb%08d" % len(enq), "enqueued"

    def row(jid="aaaa11112222", cls=None):
        return {"id": jid, "label": "auto-author-smoke-x-c1", "status": "needs_opus", "model": "qwen-test",
                "host_pref": "studio", "cwd": str(wt), "task_file": str(wt / "AUTO-TASK.md"),
                "verify": "python3 auto-harness-check.py", "max_iters": 24, "num_ctx": 65536, "bundle": "smoke-x",
                "failure_class": cls, "failure_detail": "x", "terminal_reason": "nonconvergence",
                "escalation": {"escalated_at": iso(now - 300), "reason": "authoring stuck"}}

    def common():
        return dict(ledger_path=led, slice_runs=runs, enqueue=stub_enqueue,
                    notifier=lambda t, m, dedupe_key=None, dedupe_s=0: alerts.append(m),
                    decisions=dec, log_dir=td / "logs", verdict_cache=td / ("vc-%d.json" % time.time_ns()), ctx=ctx)

    def reset(jid):
        led.unlink(missing_ok=True)
        enq.clear()
        alerts.clear()
        (td / "ledger.json").write_text(json.dumps({"announced": ["job:%s:sig" % jid, "job:other:sig"], "records": {}}))
        for f in td.glob("*-job-%s*.md" % jid):
            f.unlink()
        rp = td / ("20261011T000000Z-job-%s.review.md" % jid)
        return rp

    # S1 grounded -> acts exactly as before
    r = row("aaaa00000001")
    rp = reset("aaaa00000001")
    text = rv("b", [GOOD])
    rp.write_text(text)
    got = heal.heal_job(r, text, jobs=[r], **common())
    check("S1 GROUNDED reviewer (b) verdict drives the ladder as before (continuation enqueued)",
          (got.startswith("heal-continuation:"), len(enq), rp.exists()), (True, 1, True))
    # S2 UNVERIFIED -> wait + re-diagnosis once
    r = row("aaaa00000002")
    rp = reset("aaaa00000002")
    text = rv("b", [BAD])
    rp.write_text(text)
    got = heal.heal_job(r, text, jobs=[r], **common())
    ann = json.loads((td / "ledger.json").read_text())["announced"]
    check("S2 UNVERIFIED (b): NOT acted on, no enqueue, wait:UNVERIFIED", (got.startswith("wait:UNVERIFIED"), len(enq)), (True, 0))
    check("S2 stale review set aside, watcher key re-armed (other keys untouched)",
          (rp.exists(), (td / (rp.name[:-3] + ".unverified-1.md")).exists(), ann), (False, True, ["job:other:sig"]))
    # S3 second UNVERIFIED -> park with the label, still no action
    rp.write_text(text)
    got = heal.heal_job(r, text, jobs=[r], **common())
    check("S3 UNVERIFIED again after the re-diagnosis -> parked for a human, still no enqueue",
          (got, len(enq)), ("park:final-rung", 0))
    check("S3 the human alert carries the UNVERIFIED label", any("UNVERIFIED" in m for m in alerts), True)
    # S4 grandfathered: unstamped review with fabricated claims behaves exactly as before
    r = row("aaaa00000004")
    rp = reset("aaaa00000004")
    text = rv("b", [BAD], stamped=False)
    got = heal.heal_job(r, text, jobs=[r], **common())
    check("S4 legacy (unstamped) review is NOT gated: continuation as before", (got.startswith("heal-continuation:"), len(enq)), (True, 1))
    # S5 kill switch
    os.environ["ESC_GROUNDING"] = "off"
    r = row("aaaa00000005")
    reset("aaaa00000005")
    got = heal.heal_job(r, rv("b", [BAD]), jobs=[r], **common())
    os.environ.pop("ESC_GROUNDING")
    check("S5 ESC_GROUNDING=off restores the old behaviour", got.startswith("heal-continuation:"), True)
    # S6 mechanical failure_class is not a reviewer claim
    r = row("aaaa00000006", cls="spec")
    reset("aaaa00000006")
    got = heal.heal_job(r, rv("b", [BAD]), jobs=[r], **common())
    check("S6 mechanical failure_class verdict ignores the reviewer's ungrounded text", got.startswith("heal-continuation:"), True)
    # S7 (c) and (d) are gated like (b)
    for v in ("c", "d"):
        jid = "aaaa0000007%s" % v
        r = row(jid)
        reset(jid)
        got = heal.heal_job(r, rv(v, [BAD]), jobs=[r], **common())
        check("S7 UNVERIFIED (%s) not acted on" % v, (got.startswith("wait:UNVERIFIED"), len(enq)), (True, 0))
    # S8 (a) is never gated (verified_close proves it independently)
    r = row("aaaa00000008")
    reset("aaaa00000008")
    got = heal.heal_job(r, rv("a", [BAD]), jobs=[r], **common())
    check("S8 verdict (a) is exempt from the grounding gate", got.startswith("wait:UNVERIFIED"), False)

    # ------------------------------------------------------------ wiring: watcher prompt + stamp
    wsrc = (BIN / "dispatch-escalation-watcher.py").read_text()
    check("W1 REVIEW_PROMPT demands file:line+quote and a REPRO block",
          all(t in wsrc for t in ("GROUNDING (mandatory", "REPRO CMD:", "REPRO OUTPUT:")), True)
    check("W2 the watcher (not the model) stamps the policy and records the check",
          all(t in wsrc for t in ("_eg.stamp(review)", "_eg.report_md(_gr)", "_eg.label(_gr)")), True)
    check("W3 stamp is idempotent", eg.stamp(eg.stamp("x")).count(eg.POLICY_FOOTER), 1)
    check("W4 enforced() needs the stamp and honours the kill switch",
          (eg.enforced("x"), eg.enforced(eg.stamp("x")), eg.enforced(eg.stamp("x"), env={"ESC_GROUNDING": "off"})), (False, True, False))
    n = sum(RESULTS)
    print("\n%d/%d checks passed" % (n, len(RESULTS)))
    if n == len(RESULTS):
        print("ESC_GROUNDING_TEST_OK")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
