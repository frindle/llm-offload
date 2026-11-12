#!/usr/bin/env python3
"""darkbloom-field-probe.py -- which request fields does the LIVE Darkbloom server honor?

Stdlib only. One reusable probe for the assumptions the dispatch harness depends on
(model_profiles.yaml sampling/thinking fields, tool-call parsing, MTP, presence_penalty).
Each probe is designed so an IGNORED field is distinguishable from a honored one
(seed determinism, top_k=1 vs 40 divergence, huge penalties visible in the output ...).

Usage
  darkbloom-field-probe.py run [--out results.json] [--probes 1,2,3,4,5] [--budget 150]
                               [--n-tools 10] [--n-tools-9b 3] [--n-pairs 10]
                               [--models qwen3.6-35b-a3b-vl-mtp-mxfp8,Qwen3.5-9B]
  darkbloom-field-probe.py report results.json     # markdown tables (claim/result/evidence/confidence)
  darkbloom-field-probe.py plan                    # planned request count per probe, no network
  darkbloom-field-probe.py --selftest              # stub server, asserts the verdict logic

GPU etiquette: run `run` as a queue job (see the deliverable doc), never ad hoc.
The API key is read from ~/.darkbloom/local.json (or $DARKBLOOM_LOCAL_JSON) in code only and
is redacted from every string this tool prints or stores.
"""
import argparse
import ast
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BIG = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
SMALL = "Qwen3.5-9B"
DEFAULT_BUDGET = 160
N_TOOLS, N_TOOLS_9B, N_PAIRS = 10, 2, 10


# ----------------------------------------------------------------------------- transport
class Client:
    def __init__(self, base_url, key, budget=DEFAULT_BUDGET, timeout=300):
        self.base = base_url.rstrip("/")
        self.key = key
        self.budget = budget
        self.used = 0
        self.timeout = timeout
        self.log = []   # (n, model, status) -- never contains the key

    def redact(self, s):
        s = str(s)
        return s.replace(self.key, "***") if self.key else s

    def _req(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Authorization": "Bearer " + self.key}
        if data is not None:
            h["Content-Type"] = "application/json"
        return urllib.request.Request(self.base + path, data=data, headers=h,
                                      method="POST" if data is not None else "GET")

    def get_text(self, path):
        """GET a non-inference path (/metrics, /props). Not counted against the budget."""
        root = self.base[:-3] if self.base.endswith("/v1") else self.base
        r = urllib.request.Request(root + path, headers={"Authorization": "Bearer " + self.key})
        try:
            return urllib.request.urlopen(r, timeout=20).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return "ERR " + self.redact(e)

    def chat(self, model, messages, stream=False, **extra):
        """One chat completion; returns a normalised dict. Retries only transient 429/503."""
        body = {"model": model, "messages": messages}
        body.update(extra)
        if stream:
            body["stream"] = True
        out = {"ok": False, "status": None, "error": None, "content": "", "reasoning": "",
               "tool_calls": [], "finish": None, "usage": None, "nchunks": 0, "tc_chunks": 0,
               "elapsed": 0.0, "msg_keys": [], "first_deltas": []}
        for attempt in range(3):
            if self.used >= self.budget:
                out["error"] = "BUDGET_EXHAUSTED"
                return out
            self.used += 1
            t0 = time.time()
            try:
                resp = urllib.request.urlopen(self._req("/chat/completions", body), timeout=self.timeout)
                out["status"] = resp.status
                if stream:
                    self._read_stream(resp, out)
                else:
                    d = json.loads(resp.read())
                    ch = (d.get("choices") or [{}])[0]
                    msg = ch.get("message") or {}
                    out["msg_keys"] = sorted(msg.keys())
                    out["content"] = msg.get("content") or ""
                    out["reasoning"] = msg.get("reasoning_content") or msg.get("reasoning") or ""
                    out["tool_calls"] = [{"name": (t.get("function") or {}).get("name"),
                                          "arguments": (t.get("function") or {}).get("arguments")}
                                         for t in msg.get("tool_calls") or []]
                    out["finish"] = ch.get("finish_reason")
                    out["usage"] = d.get("usage")
                out["ok"] = True
                out["elapsed"] = round(time.time() - t0, 2)
                return out
            except urllib.error.HTTPError as e:
                out["status"] = e.code
                out["error"] = self.redact("HTTP %s %s" % (e.code, e.read()[:300].decode("utf-8", "replace")))
                out["elapsed"] = round(time.time() - t0, 2)
                if e.code in (429, 502, 503, 504) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                return out
            except Exception as e:  # noqa: BLE001
                out["error"] = self.redact("%s: %s" % (type(e).__name__, e))
                out["elapsed"] = round(time.time() - t0, 2)
                return out
        return out

    @staticmethod
    def _read_stream(resp, out):
        calls = {}
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
            except ValueError:
                continue
            out["nchunks"] += 1
            if d.get("usage"):
                out["usage"] = d["usage"]
            ch = (d.get("choices") or [{}])[0]
            if ch.get("finish_reason"):
                out["finish"] = ch["finish_reason"]
            delta = ch.get("delta") or {}
            if len(out["first_deltas"]) < 3 and delta:
                out["first_deltas"].append({k: (v if not isinstance(v, str) else v[:40])
                                            for k, v in delta.items() if k != "tool_calls"})
            if delta.get("content"):
                out["content"] += delta["content"]
            r = delta.get("reasoning_content") or delta.get("reasoning")
            if r:
                out["reasoning"] += r
            for t in delta.get("tool_calls") or []:
                out["tc_chunks"] += 1
                c = calls.setdefault(t.get("index", 0), {"name": None, "arguments": ""})
                f = t.get("function") or {}
                if f.get("name"):
                    c["name"] = f["name"]
                if f.get("arguments"):
                    c["arguments"] += f["arguments"]
        out["tool_calls"] = [calls[k] for k in sorted(calls)]


# ----------------------------------------------------------------------------- helpers
NOTHINK = {"chat_template_kwargs": {"enable_thinking": False}}


def U(p):
    return [{"role": "user", "content": p}]


def ctoks(r):
    return ((r.get("usage") or {}).get("completion_tokens"))


def row(claim, result, evidence, confidence):
    return {"claim": claim, "result": result, "evidence": evidence, "confidence": confidence}


def short(s, n=70):
    s = (s or "").replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + "..."


def texts(c, model, n, **kw):
    msgs = kw.pop("messages", None) or U(kw.pop("prompt"))
    return [c.chat(model, msgs, **kw) for _ in range(n)]


def contents(rs):
    return [r["content"] for r in rs]


def distinct(rs):
    return len(set(contents(rs)))


def errs(rs):
    return [r["error"] for r in rs if not r["ok"]]


CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def artefacts(text):
    """Cheap repeated-token / degeneration detectors. Returns a list of reason strings."""
    out = []
    if CJK.search(text):
        out.append("cjk")
    if re.search(r"(\b\S+\b)(\s+\1){7,}", text):
        out.append("word_loop")
    if re.search(r"([^\w\s])\1{9,}", text):
        out.append("char_run")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    run = 1
    for a, b in zip(lines, lines[1:]):
        run = run + 1 if a == b else 1
        if run >= 5:
            out.append("line_loop")
            break
    return out


def strip_fence(t):
    m = re.search(r"```[a-zA-Z0-9_-]*\n(.*?)```", t, re.S)
    if m:
        return m.group(1)
    m = re.search(r"```[a-zA-Z0-9_-]*\n(.*)$", t, re.S)
    return m.group(1) if m else t


def think_markers(r):
    c = r["content"]
    return {"open_in_content": "<think>" in c, "close_in_content": "</think>" in c,
            "reasoning_len": len(r["reasoning"]), "content_len": len(c)}



# ----------------------------------------------------------------------------- shared sub-probes
THINK_HEAD = re.compile(r"^\s*(here'?s a thinking process|here is a thinking process|thinking process)", re.I)


def thinks(r):
    """True when a response carries thinking: a reasoning field, think markers, or a CoT header
    (the Qwen3.x template opens <think> in the PROMPT, so the opening marker never appears)."""
    c = r["content"]
    return bool(r["reasoning"] or "</think>" in c or "<think>" in c or THINK_HEAD.match(c))


def p_thinking(c, model, tag, rows, raw):
    Q = "A bat and a ball cost 1.10 total. The bat costs 1.00 more than the ball. How much is the ball?"
    on = c.chat(model, U(Q), max_tokens=600, temperature=0.6, chat_template_kwargs={"enable_thinking": True})
    off = c.chat(model, U(Q), max_tokens=600, temperature=0.6, chat_template_kwargs={"enable_thinking": False})
    ton, toff = thinks(on), thinks(off)
    res = "INCONCLUSIVE" if errs([on, off]) else ("HONORED" if ton and not toff else ("IGNORED (thinks when false)" if toff else "INCONCLUSIVE (no thinking signature when true)"))
    rows.append(row(tag + "chat_template_kwargs.enable_thinking (neutral prompt)", res,
                    "true: tokens=%s thinks=%s reasoning_field_len=%d head=%r | false: tokens=%s thinks=%s head=%r" % (
                        ctoks(on), ton, len(on["reasoning"]), short(on["content"], 45), ctoks(off), toff, short(off["content"], 45)), "high"))
    raw[tag + "think"] = {"on": on["content"][:300], "off": off["content"][:300]}


def p_preserve(c, model, tag, rows, raw):
    q = {"role": "user", "content": "What was the exact code word in your earlier private reasoning? Reply with only the word, or UNKNOWN."}
    first = {"role": "user", "content": "Pick a secret code word and keep it private."}
    variants = {
        "reasoning_content field": [first, {"role": "assistant", "reasoning_content": "My private code word is ZEBRA7731.", "content": "Done, I picked one."}, q],
        "inline </think> in content": [first, {"role": "assistant", "content": "My private code word is ZEBRA7731.\n</think>\n\nDone, I picked one."}, q],
    }
    for name, hist in variants.items():
        pt = c.chat(model, hist, max_tokens=40, temperature=0, chat_template_kwargs={"enable_thinking": False, "preserve_thinking": True})
        pf = c.chat(model, hist, max_tokens=40, temperature=0, chat_template_kwargs={"enable_thinking": False, "preserve_thinking": False})
        st, sf = "ZEBRA7731" in pt["content"], "ZEBRA7731" in pf["content"]
        res = "INCONCLUSIVE" if errs([pt, pf]) else ("HONORED" if st and not sf else ("IGNORED/always visible" if st and sf else "NOT-EFFECTIVE (history reasoning never visible)"))
        rows.append(row(tag + "preserve_thinking via %s" % name, res,
                        "true -> %r ; false -> %r" % (short(pt["content"], 40), short(pf["content"], 40)), "medium"))


def p_effort(c, model, tag, rows, raw):
    Q2 = "What is 17 * 23? Work it out, then answer."
    kw = dict(max_tokens=1500, temperature=0.6, chat_template_kwargs={"enable_thinking": True})
    lo = [c.chat(model, U(Q2), reasoning_effort="low", **kw) for _ in range(2)]
    hi = [c.chat(model, U(Q2), reasoning_effort="high", **kw) for _ in range(2)]
    mlo = sum(ctoks(r) or 0 for r in lo) / 2.0
    mhi = sum(ctoks(r) or 0 for r in hi) / 2.0
    capped = sum(1 for r in lo + hi if r["finish"] == "length")
    res = "INCONCLUSIVE" if errs(lo + hi) else ("HONORED" if mhi > 0 and mlo < 0.7 * mhi else "IGNORED (no visible effect)")
    rows.append(row(tag + "reasoning_effort", res, "mean completion tokens low=%.0f high=%.0f (n=2 each, cap 1500, %d/4 hit the cap)" % (mlo, mhi, capped), "low-medium"))


def p_penalty(c, model, tag, t0, rows, raw):
    """presence/repetition penalty: greedy/seeded baselines make any text change attributable."""
    RP = "Output the word apple exactly 30 times separated by single spaces, and nothing else."
    rb = dict(NOTHINK, max_tokens=90, temperature=0)
    a0 = c.chat(model, U(RP), **rb)
    a2 = c.chat(model, U(RP), presence_penalty=2.0, **rb)
    a10 = c.chat(model, U(RP), presence_penalty=10.0, **rb)
    r3 = c.chat(model, U(RP), repetition_penalty=3.0, **rb)
    n = lambda r: r["content"].lower().count("apple")  # noqa: E731
    O = "Write a 60-word description of a lighthouse keeper's morning."
    ob = dict(NOTHINK, max_tokens=120)
    o0 = c.chat(model, U(O), temperature=0, **ob)
    o2 = c.chat(model, U(O), temperature=0, presence_penalty=2.0, **ob)
    O2 = "Explain in about 50 words why the sky is blue."
    s0 = c.chat(model, U(O2), temperature=0, **ob)
    s15 = c.chat(model, U(O2), temperature=0, presence_penalty=1.5, **ob)
    allr = [a0, a2, a10, r3, o0, o2, s0, s15]
    if errs(allr):
        res, conf = "INCONCLUSIVE", "low"
    else:
        changed = [o0["content"] != o2["content"], s0["content"] != s15["content"], n(a10) != n(a0) or a10["content"] != a0["content"], n(a2) != n(a0)]
        res = "HONORED" if any(changed) else "IGNORED"
        conf = "high" if (changed[0] or changed[1]) else "medium"
    rows.append(row(tag + "presence_penalty", res,
                    "text changes vs greedy baseline (greedy is deterministic, see temperature row): greedy prompt1 pp2=%s ; greedy prompt2 pp1.5=%s ; apple-count pp0=%d pp2=%d pp10=%d (apple test alone is weak: a confident repeat survives pp2)" % (
                        o0["content"] != o2["content"], s0["content"] != s15["content"], n(a0), n(a2), n(a10)), conf))
    res = "INCONCLUSIVE" if errs([a0, r3]) else ("HONORED" if r3["content"] != a0["content"] else "IGNORED")
    rows.append(row(tag + "repetition_penalty", res, "'apple' count rp=1.0(omitted):%d vs rp=3.0:%d; rp3 sample=%r" % (n(a0), n(r3), short(r3["content"], 60)),
                    "high" if res == "HONORED" else "medium"))
    raw[tag + "penalties"] = {"apple_base": a0["content"], "pp2": a2["content"], "pp10": a10["content"], "rp3": r3["content"],
                              "open0": o0["content"], "open_pp2": o2["content"], "open2": s0["content"], "open2_pp15": s15["content"]}


# ----------------------------------------------------------------------------- probe 1
def probe1(c, models):
    rows, raw = [], {}
    for model in models:
        big = model == BIG
        tag = "[%s] " % ("35B" if big else "9B")
        base = dict(NOTHINK, max_tokens=60, top_p=1.0)
        P = "Write one unusual sentence of about 15 words about a lighthouse."

        # temperature
        t0 = texts(c, model, 3 if big else 2, prompt=P, temperature=0, **base)
        t1 = texts(c, model, 3 if big else 2, prompt=P, temperature=1.3, **base)
        okr = not errs(t0 + t1)
        if not okr:
            res, conf = "INCONCLUSIVE", "low"
        elif distinct(t0) == 1 and distinct(t1) > 1:
            res, conf = "HONORED", "high"
        elif distinct(t0) == 1 and distinct(t1) == 1:
            res, conf = "IGNORED or forced-greedy", "medium"
        else:
            res, conf = "INCONCLUSIVE", "low"
        rows.append(row(tag + "temperature", res,
                        "temp0 distinct=%d/%d, temp1.3 distinct=%d/%d %s" % (
                            distinct(t0), len(t0), distinct(t1), len(t1), errs(t0 + t1)[:1]), conf))
        raw[tag + "temp"] = {"t0": contents(t0), "t1": contents(t1)}
        if big:
            om = texts(c, model, 1, prompt=P, **base)
            rows.append(row(tag + "omitted temperature behaves greedy (docs: omitted = 0.0)",
                            "CONFIRMED" if distinct(om) == 1 and om[0]["content"] == t0[0]["content"] else
                            ("identical-to-each-other-but-not-to-temp0" if distinct(om) == 1 else "NOT greedy"),
                            "omitted x1 distinct=%d; equals temp0 text=%s" % (distinct(om), om[0]["content"] == t0[0]["content"]),
                            "medium"))

        # seed
        ka = dict(base, temperature=1.3)
        s1 = texts(c, model, 2, prompt=P, seed=1234, **ka)
        s2 = texts(c, model, 1, prompt=P, seed=999, **ka)
        un = texts(c, model, 1, prompt=P, **ka)
        if errs(s1 + s2 + un):
            res, conf = "INCONCLUSIVE", "low"
        elif distinct(s1) == 1 and s1[0]["content"] != s2[0]["content"]:
            res, conf = "HONORED", "high" if distinct(t1) > 1 else "medium"
        elif distinct(s1) > 1:
            res, conf = "IGNORED", "medium"
        else:
            res, conf = "INCONCLUSIVE", "low"
        rows.append(row(tag + "seed", res,
                        "seed=1234 x2 identical=%s; seed=999 differs=%s; unseeded x%d distinct=%d" % (
                            distinct(s1) == 1, s1[0]["content"] != s2[0]["content"], len(un), distinct(un)), conf))
        raw[tag + "seed"] = {"s1234": contents(s1), "s999": contents(s2), "unseeded": contents(un)}

        # top_k
        k1 = texts(c, model, 3 if big else 2, prompt=P, top_k=1, temperature=1.3, **dict(base))
        k40 = texts(c, model, 3 if big else 2, prompt=P, top_k=40, temperature=1.3, **dict(base))
        if errs(k1 + k40):
            res, conf = "INCONCLUSIVE", "low"
        elif distinct(k1) == 1 and distinct(k40) > 1:
            res = "HONORED"
            conf = "high" if k1[0]["content"] == t0[0]["content"] else "medium"
        elif distinct(k1) > 1:
            res, conf = "IGNORED", "high"
        else:
            res, conf = "INCONCLUSIVE", "low"
        rows.append(row(tag + "top_k", res,
                        "top_k=1 @temp1.3 distinct=%d/%d (equals greedy=%s); top_k=40 distinct=%d/%d" % (
                            distinct(k1), len(k1), k1[0]["content"] == t0[0]["content"], distinct(k40), len(k40)), conf))
        if not big:
            continue

        # top_p
        tp = texts(c, model, 3, prompt=P, top_p=0.01, temperature=1.3, **{k: v for k, v in base.items() if k != "top_p"})
        res = "INCONCLUSIVE" if errs(tp) else ("HONORED" if distinct(tp) == 1 and distinct(t1) > 1 else
                                                ("IGNORED" if distinct(tp) > 1 else "INCONCLUSIVE"))
        rows.append(row(tag + "top_p", res, "top_p=0.01 @temp1.3 distinct=%d/3 (control temp1.3 distinct=%d)" % (
            distinct(tp), distinct(t1)), "high" if res != "INCONCLUSIVE" else "low"))

        # min_p
        mp = texts(c, model, 3, prompt=P, min_p=0.95, temperature=1.3, **base)
        res = "INCONCLUSIVE" if errs(mp) else ("HONORED" if distinct(mp) == 1 and distinct(t1) > 1 else
                                                ("IGNORED" if distinct(mp) > 1 else "INCONCLUSIVE"))
        rows.append(row(tag + "min_p", res, "min_p=0.95 @temp1.3 distinct=%d/3 (docs say ignored)" % distinct(mp),
                        "high" if res != "INCONCLUSIVE" else "low"))

        p_penalty(c, model, tag, t0, rows, raw)

        # max_tokens
        m = c.chat(model, U("Write a long story about a dragon."), max_tokens=7, **NOTHINK)
        okm = m["ok"] and m["finish"] == "length" and (ctoks(m) or 99) <= 8
        rows.append(row(tag + "max_tokens", "HONORED" if okm else ("INCONCLUSIVE" if not m["ok"] else "IGNORED"),
                        "max_tokens=7 -> finish=%s completion_tokens=%s" % (m["finish"], ctoks(m)), "high"))

        # stop
        S = "Count from 1 to 10 as digits separated by commas, nothing else."
        s_no = c.chat(model, U(S), max_tokens=60, temperature=0, **NOTHINK)
        s_st = c.chat(model, U(S), max_tokens=60, temperature=0, stop=["5"], **NOTHINK)
        ok = "5" in s_no["content"] and "5" not in s_st["content"] and s_st["finish"] == "stop"
        rows.append(row(tag + "stop", "HONORED" if ok else ("INCONCLUSIVE" if "5" not in s_no["content"] else "IGNORED"),
                        "no-stop=%r ; stop=['5'] -> %r finish=%s" % (short(s_no["content"], 30), short(s_st["content"], 30), s_st["finish"]), "high"))

        # unknown-field control
        u = c.chat(model, U("Say hi."), max_tokens=10, zz_unknown_field=1, **NOTHINK)
        rows.append(row(tag + "unknown top-level field", "silently accepted" if u["ok"] else "REJECTED",
                        "status=%s %s" % (u["status"], u["error"] or ""), "high"))

    # thinking switches (both models)
    for model in models:
        tag = "[%s] " % ("35B" if model == BIG else "9B")
        p_thinking(c, model, tag, rows, raw)
        if model == BIG:
            p_preserve(c, model, tag, rows, raw)
            p_effort(c, model, tag, rows, raw)
    return {"rows": rows, "raw": raw}


# ----------------------------------------------------------------------------- probe 2
def probe2(c, models):
    rows, raw = [], {}
    Hard = "Prove carefully that there are infinitely many primes congruent to 3 mod 4. Think step by step in great detail."
    Easy = "What is 17 * 23? Think it through, then answer."
    for model in models:
        tag = "[%s] " % ("35B" if model == BIG else "9B")
        for name, prompt, cap in (("capped-open", Hard, 150), ("closed", Easy, 1500)):
            res = {}
            for stream in (False, True):
                r = c.chat(model, U(prompt), stream=stream, max_tokens=cap, temperature=0.6,
                           chat_template_kwargs={"enable_thinking": True})
                m = think_markers(r)
                m.update(finish=r["finish"], tokens=ctoks(r), err=r["error"], head=short(r["content"], 50),
                         first_deltas=r["first_deltas"][:2])
                res["stream" if stream else "nonstream"] = m
            raw[tag + name] = res
            ns, st = res["nonstream"], res["stream"]

            def where(m):
                if m.get("err"):
                    return "ERR"
                if m["reasoning_len"] and not m["close_in_content"]:
                    return "reasoning field"
                if m["close_in_content"] or m["open_in_content"]:
                    return "content (markers inline)"
                if m["content_len"] and not m["reasoning_len"]:
                    return "content (no markers)"
                return "empty"
            rows.append(row("%s%s: where does thinking land" % (tag, name),
                            "nonstream=%s | stream=%s" % (where(ns), where(st)),
                            "nonstream open<think>=%s </think>=%s reason_len=%s content_len=%s finish=%s | stream open<think>=%s </think>=%s reason_len=%s content_len=%s finish=%s head(ns)=%r head(st)=%r" % (
                                ns["open_in_content"], ns["close_in_content"], ns["reasoning_len"], ns["content_len"], ns["finish"],
                                st["open_in_content"], st["close_in_content"], st["reasoning_len"], st["content_len"], st["finish"],
                                ns["head"], st["head"]),
                            "high"))
    return {"rows": rows, "raw": raw}


# ----------------------------------------------------------------------------- probe 3
TOOLS = [{"type": "function", "function": {
    "name": "write_file", "description": "Write a file to disk with the given text content.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                   "required": ["path", "content"]}}}]
LONG_P = ("Call the write_file tool exactly once. path: inventory.py. content: a complete Python module of about "
          "100 lines (no more than 120) implementing an Inventory class with add_item, remove_item, "
          "find_item, total_value, to_json, from_json, and low_stock methods, each with a one-line docstring and "
          "real logic, plus a small demo under if __name__ == '__main__'. Include double quotes and backslashes "
          "in some strings. Respond with only the tool call.")
SHORT_P = "Call the write_file tool exactly once with path notes.txt and content: the single line ok. Respond with only the tool call."
RAW_MARK = re.compile(r"<tool_call>|<function=|<parameter=|\"name\"\s*:\s*\"write_file\"|\"arguments\"\s*:")


def classify_tool(r):
    """-> (label, detail). label in ok|no_tool_calls|raw_markup_in_content|bad_json|missing_keys|truncated|error"""
    if not r["ok"]:
        return "error", r["error"]
    markup = bool(RAW_MARK.search(r["content"] + r["reasoning"]))
    if not r["tool_calls"]:
        if r["finish"] == "length":
            return "truncated", "finish=length no tool_calls"
        return ("raw_markup_in_content" if markup else "no_tool_calls"), short(r["content"], 80)
    tc = r["tool_calls"][0]
    if r["finish"] == "length":
        return "truncated", "finish=length with partial tool call"
    try:
        a = json.loads(tc["arguments"] or "")
    except ValueError:
        return "bad_json", short(tc["arguments"], 80)
    if not isinstance(a, dict) or not isinstance(a.get("content"), str) or not isinstance(a.get("path"), str):
        return "missing_keys", short(json.dumps(a), 80)
    if markup and tc["name"] and len(r["tool_calls"]) == 1 and RAW_MARK.search(r["content"]):
        return "ok_but_markup_also_in_content", short(r["content"], 80)
    return "ok", "%d lines" % (a["content"].count("\n") + 1)


def probe3(c, models, n_big, n_small):
    rows, raw = [], {}
    for model in models:
        tag = "[%s] " % ("35B" if model == BIG else "9B")
        n = n_big if model == BIG else n_small
        for plabel, prompt, cap in (("long", LONG_P, 1500), ("short", SHORT_P, 300)):
            for stream in (False, True):
                labels, lines, chunks = {}, [], []
                for _ in range(n):
                    r = c.chat(model, U(prompt), stream=stream, tools=TOOLS, max_tokens=cap, temperature=0.6,
                               top_p=0.95, top_k=20, **NOTHINK)
                    lab, det = classify_tool(r)
                    labels[lab] = labels.get(lab, 0) + 1
                    if lab.startswith("ok"):
                        lines.append(int(det.split()[0]))
                    chunks.append(r["tc_chunks"])
                    raw.setdefault(tag + "%s-%s" % (plabel, "stream" if stream else "nonstream"), []).append(
                        {"label": lab, "detail": det, "finish": r["finish"], "tokens": ctoks(r)})
                good = labels.get("ok", 0) + labels.get("ok_but_markup_also_in_content", 0)
                bad = sum(v for k, v in labels.items() if k not in ("ok", "ok_but_markup_also_in_content", "truncated", "error"))
                res = "%d/%d ok, %d malformed" % (good, n, bad)
                rows.append(row("%s%s args, %s" % (tag, plabel, "streaming" if stream else "non-streaming"), res,
                                "labels=%s; ok arg lines(min/max)=%s; tool-call delta chunks per response=%s" % (
                                    labels, (min(lines), max(lines)) if lines else "-", chunks[:5] if stream else "n/a"),
                                "medium" if n >= 10 else "low"))
    return {"rows": rows, "raw": raw}


# ----------------------------------------------------------------------------- probe 4
def parse_metrics(txt, model):
    out = {}
    for ln in txt.splitlines():
        m = re.match(r'(mtp_[a-z_]+)\{model="%s"\}\s+([0-9.eE+-]+)' % re.escape(model), ln)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def probe4(c, models, pre, post, tool_rows):
    """pre/post: {model: parsed metrics} taken around the whole run (diff covers all probe traffic)."""
    rows = []
    for model in models:
        tag = "[%s] " % ("35B" if model == BIG else "9B")
        a, b = pre.get(model, {}), post.get(model, {})
        d = {k: b.get(k, 0) - a.get(k, 0) for k in ("mtp_rounds_total", "mtp_tokens_proposed_total", "mtp_tokens_accepted_total")}
        act = b.get("mtp_active")
        if not b:
            rows.append(row(tag + "MTP active (/metrics)", "UNKNOWN", "no mtp_* lines in /metrics", "low"))
            continue
        acc = (d["mtp_tokens_accepted_total"] / d["mtp_tokens_proposed_total"]) if d["mtp_tokens_proposed_total"] else None
        used = d["mtp_rounds_total"] > 0
        rows.append(row(tag + "MTP active and used", "ACTIVE and USED" if (act == 1 and used) else ("ENABLED but NOT USED by this traffic" if act == 1 else "OFF"),
                        "mtp_enabled=%s mtp_active=%s; delta over probe run: rounds=%d proposed=%d accepted=%d accept-rate=%s (delta includes any concurrent traffic on the server)" % (
                            b.get("mtp_enabled"), act, d["mtp_rounds_total"], d["mtp_tokens_proposed_total"], d["mtp_tokens_accepted_total"],
                            "n/a" if acc is None else "%.3f" % acc),
                        "high" if used else "medium"))
    return {"rows": rows, "raw": {"pre": pre, "post": post}}


# ----------------------------------------------------------------------------- probe 5
CODE_TASKS = [
    "a function slugify(s) that lowercases, strips punctuation and joins words with hyphens",
    "a function merge_intervals(list_of_pairs) returning merged sorted intervals",
    "a class LRUCache(capacity) with get and put in O(1)",
    "a function parse_csv_line(line) handling quoted fields with commas",
    "a function flatten(nested) that flattens arbitrarily nested lists",
    "a function roman_to_int(s)",
    "a function is_balanced(s) for ()[]{} brackets",
    "a function top_k_words(text, k) returning the k most common words",
    "a function binary_search(arr, x) returning the index or -1",
    "a function chunked(iterable, n) yielding lists of length n",
]
JSON_TASKS = [
    "8 products, each with keys id, name, price, tags (array of 3 strings)",
    "8 employees, each with keys id, name, department, skills (array of 3 strings)",
    "8 books, each with keys id, title, author, year, genres (array of 2 strings)",
    "8 cities, each with keys id, name, country, population, landmarks (array of 3 strings)",
    "8 recipes, each with keys id, name, minutes, ingredients (array of 4 strings)",
    "8 songs, each with keys id, title, artist, seconds, moods (array of 2 strings)",
    "8 tickets, each with keys id, title, status, labels (array of 3 strings), assignee",
    "8 sensors, each with keys id, location, unit, readings (array of 5 numbers)",
    "8 courses, each with keys id, name, credits, prerequisites (array of 2 strings)",
    "8 pets, each with keys id, name, species, age, vaccines (array of 3 strings)",
]


def eval_code(t):
    code = strip_fence(t)
    try:
        ast.parse(code)
        syn = True
    except SyntaxError:
        syn = False
    return syn, artefacts(t)


def eval_json(t):
    body = strip_fence(t).strip()
    try:
        j = json.loads(body)
    except ValueError:
        return False, False, artefacts(t)
    items = j if isinstance(j, list) else next((v for v in j.values() if isinstance(v, list)), []) if isinstance(j, dict) else []
    full = len(items) == 8 and all(isinstance(i, dict) and len(i) >= 4 for i in items)
    return True, full, artefacts(t)


def probe5(c, model, n_pairs):
    rows, raw = [], {"code": [], "json": []}
    arms = (0.0, 1.5)
    res = {"code": {a: {"syntax_fail": 0, "artefact": 0, "tokens": 0, "n": 0, "err": 0} for a in arms},
           "json": {a: {"parse_fail": 0, "incomplete": 0, "artefact": 0, "tokens": 0, "n": 0, "err": 0} for a in arms}}
    for i in range(n_pairs):
        for kind, tasks in (("code", CODE_TASKS), ("json", JSON_TASKS)):
            prompt = ("Write %s. Output only one fenced python code block." % tasks[i]) if kind == "code" else \
                     ("Return ONLY a JSON array (no prose, no code fence) of %s." % tasks[i])
            for pp in arms:
                r = c.chat(model, U(prompt), max_tokens=700, temperature=0.7, top_p=0.8, top_k=20,
                           presence_penalty=pp, seed=1000 + i, **NOTHINK)
                s = res[kind][pp]
                s["n"] += 1
                if not r["ok"]:
                    s["err"] += 1
                    continue
                s["tokens"] += ctoks(r) or 0
                if kind == "code":
                    syn, art = eval_code(r["content"])
                    s["syntax_fail"] += (not syn)
                    s["artefact"] += bool(art)
                    raw["code"].append({"pp": pp, "i": i, "syntax": syn, "art": art, "finish": r["finish"]})
                else:
                    ok, full, art = eval_json(r["content"])
                    s["parse_fail"] += (not ok)
                    s["incomplete"] += (ok and not full)
                    s["artefact"] += bool(art)
                    raw["json"].append({"pp": pp, "i": i, "parse": ok, "full": full, "art": art, "finish": r["finish"],
                                        "head": None if ok else r["content"][:160], "tail": None if ok else r["content"][-100:]})
    for kind in ("code", "json"):
        a, b = res[kind][0.0], res[kind][1.5]
        key = "syntax_fail" if kind == "code" else "parse_fail"
        rows.append(row("[35B] presence_penalty 1.5 vs 0 on %s turns (non-thinking, card non-thinking sampling, seed-paired, n=%d pairs)" % (kind, n_pairs),
                        "%s malformed: pp0=%d pp1.5=%d ; artefact turns: pp0=%d pp1.5=%d" % (key, a[key], b[key], a["artefact"], b["artefact"]),
                        "details pp0=%s | pp1.5=%s (indicative only, tiny sample, one seed per pair)" % (a, b),
                        "low"))
    return {"rows": rows, "raw": raw, "res": {k: {str(p): v for p, v in d.items()} for k, d in res.items()}}



# ----------------------------------------------------------------------------- probe 6 (follow-ups)
SHORT_VARIANTS = {
    "A original ('Respond with only the tool call')": (SHORT_P, None),
    "B plain ask ('Use the write_file tool to create ...')": ("Use the write_file tool to create notes.txt containing the single line: ok", None),
    "C original + system 'always answer with a tool call, never bare JSON'": (SHORT_P, "You are a tool-calling agent. Always respond by calling a provided tool through the tool-call interface, never with plain JSON text."),
}


def probe6(c, models, n=4):
    """Corrected / deepened follow-ups for what the first run could not settle."""
    rows, raw = [], {}
    for model in models:
        tag = "[%s] " % ("35B" if model == BIG else "9B")
        t0 = texts(c, model, 2, prompt="Write one unusual sentence of about 15 words about a lighthouse.", temperature=0, max_tokens=60, top_p=1.0, **NOTHINK)
        p_thinking(c, model, tag, rows, raw)
        if model != BIG:
            continue
        p_preserve(c, model, tag, rows, raw)
        p_penalty(c, model, tag, t0, rows, raw)
        p_effort(c, model, tag, rows, raw)
        # replay the JSON items that failed to parse in run 1 (same seeds -> same outputs) and keep the evidence
        for i in (0, 2, 5):
            for pp in (0.0, 1.5):
                r = c.chat(model, U("Return ONLY a JSON array (no prose, no code fence) of %s." % JSON_TASKS[i]), max_tokens=700,
                           temperature=0.7, top_p=0.8, top_k=20, presence_penalty=pp, seed=1000 + i, **NOTHINK)
                ok, full, art = eval_json(r["content"]) if r["ok"] else (False, False, [])
                rows.append(row("%sJSON replay item %d pp=%s" % (tag, i, pp), "parses" if ok else "PARSE FAIL",
                                "finish=%s tokens=%s head=%r tail=%r" % (r["finish"], ctoks(r), short(r["content"], 90), short(r["content"][-80:], 80)), "medium"))
        for name, (prompt, system) in SHORT_VARIANTS.items():
            for stream in ((False, True) if name.startswith("B") else (False,)):
                k = n if not stream else max(2, n - 1)
                labels, heads = {}, []
                for _ in range(k):
                    msgs = ([{"role": "system", "content": system}] if system else []) + U(prompt)
                    r = c.chat(model, msgs, stream=stream, tools=TOOLS, max_tokens=300, temperature=0.6, top_p=0.95, top_k=20, **NOTHINK)
                    lab, det = classify_tool(r)
                    labels[lab] = labels.get(lab, 0) + 1
                    if lab != "ok" and len(heads) < 2:
                        heads.append(det)
                ok = labels.get("ok", 0)
                rows.append(row("%sshort tool prompt variant %s, %s" % (tag, name, "streaming" if stream else "non-streaming"),
                                "%d/%d structured tool_calls" % (ok, k), "labels=%s; failing content heads=%s" % (labels, heads), "low-medium"))
    return {"rows": rows, "raw": raw}


# ----------------------------------------------------------------------------- orchestration
class DryClient(Client):
    """Counts requests, makes none. Used by `plan` so the numbers can never drift from the probes."""
    def __init__(self):
        super().__init__("http://dry", "dry", budget=10 ** 6)

    def chat(self, model, messages, stream=False, **extra):
        self.used += 1
        return {"ok": True, "status": 200, "error": None, "content": "", "reasoning": "", "tool_calls": [], "finish": "stop",
                "usage": {"completion_tokens": 1}, "nchunks": 0, "tc_chunks": 0, "elapsed": 0, "msg_keys": [], "first_deltas": []}


def plan_counts(models, n_big, n_small, n_pairs):
    """Planned inference requests per probe (upper bound without retries)."""
    out = {}
    for pid, fn in (("1", lambda c: probe1(c, models)), ("2", lambda c: probe2(c, models)),
                    ("3", lambda c: probe3(c, models, n_big, n_small)),
                    ("5", lambda c: probe5(c, BIG, n_pairs) if BIG in models else None),
                    ("6", lambda c: probe6(c, models))):
        d = DryClient()
        fn(d)
        out[pid] = d.used
    return out


def snapshot(c, models):
    txt = c.get_text("/metrics")
    return {m: parse_metrics(txt, m) for m in models}


def run(args):
    local = json.load(open(args.local_json))
    c = Client(args.base_url or local["base_url"], local["api_key"], budget=args.budget)
    models = [m for m in args.models.split(",") if m]
    probes = set(args.probes.split(","))
    out = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "models": models, "budget": args.budget,
           "plan": plan_counts(models, args.n_tools, args.n_tools_9b, args.n_pairs), "probes": {}}
    if not (probes - {"4"}):
        pass
    try:
        out["props_head"] = c.get_text("/props")[:300]
    except Exception:  # noqa: BLE001
        pass
    pre = snapshot(c, models)
    steps = [("1", lambda: probe1(c, models)), ("2", lambda: probe2(c, models)),
             ("3", lambda: probe3(c, models, args.n_tools, args.n_tools_9b)),
             ("5", lambda: probe5(c, BIG, args.n_pairs) if BIG in models else {"rows": [], "raw": {}}),
             ("6", lambda: probe6(c, models))]
    for pid, fn in steps:
        if pid in probes:
            print("[probe] running probe %s (requests used so far %d/%d)" % (pid, c.used, c.budget), flush=True)
            out["probes"][pid] = fn()
            json.dump(out, open(args.out, "w"), indent=1)
    post = snapshot(c, models)
    if "4" in probes:
        out["probes"]["4"] = probe4(c, models, pre, post, None)
        # static facts from the on-disk checkpoint configs (read-only)
        out["probes"]["4"]["rows"] += static_model_rows(args.cache_dir, models)
    out["requests_used"] = c.used
    out["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    json.dump(out, open(args.out, "w"), indent=1)
    print("[probe] done, %d requests, wrote %s" % (c.used, args.out), flush=True)
    return 0


def static_model_rows(cache_dir, models):
    rows = []
    for m in models:
        p = Path(cache_dir) / ("models--" + m) / "snapshots" / "local" / "config.json"
        if not p.exists():
            rows.append(row("[%s] checkpoint model_type" % m, "UNKNOWN", "no config.json at %s" % p, "low"))
            continue
        cfg = json.load(open(p))
        mt = cfg.get("model_type")
        tmt = (cfg.get("text_config") or {}).get("model_type")
        parser = "Qwen35ToolCallParser (XML first, framed-JSON fallback)" if str(mt).startswith("qwen3_5") else "JSONToolCallParser (WRONG for Qwen XML)"
        rows.append(row("[%s] config.json model_type -> parser (docs: prefix qwen3_5 => Qwen35ToolCallParser)" % m,
                        "model_type=%s text_config.model_type=%s => %s" % (mt, tmt, parser),
                        "%s; archs=%s; mtp_num_hidden_layers=%s" % (p, cfg.get("architectures"), (cfg.get("text_config") or {}).get("mtp_num_hidden_layers")),
                        "high (static; the runtime parse is tested in probe 3)"))
    return rows


def report(path):
    d = json.load(open(path))
    print("# Darkbloom field probe -- %s -> %s, requests used %s/%s\n" % (d.get("started"), d.get("finished"), d.get("requests_used"), d.get("budget")))
    for pid in sorted(d["probes"]):
        print("## Probe %s\n" % pid)
        print("| claim | result | evidence | confidence |\n|---|---|---|---|")
        for r in d["probes"][pid]["rows"]:
            print("| %s |" % " | ".join(str(r[k]).replace("|", "/").replace("\n", " ") for k in ("claim", "result", "evidence", "confidence")))
        print()
    return 0


# ----------------------------------------------------------------------------- selftest
class Stub(BaseHTTPRequestHandler):
    """Minimal OpenAI-ish server. cls.honor: set of fields it honors. Others are ignored."""
    honor = set()
    key = "TESTKEY-SECRET"
    metrics_rounds = 0
    count = 0

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + self.key:
            return self._send(401, {"error": "auth"})
        if self.path == "/metrics":
            Stub.metrics_rounds += 0
            return self._send(200, ('mtp_enabled{model="%s"} 1\nmtp_active{model="%s"} 1\nmtp_rounds_total{model="%s"} %d\n'
                                    'mtp_tokens_proposed_total{model="%s"} %d\nmtp_tokens_accepted_total{model="%s"} %d\n' % (
                                        BIG, BIG, BIG, Stub.count * 5, BIG, Stub.count * 10, BIG, Stub.count * 9)).encode(), "text/plain")
        if self.path == "/props":
            return self._send(200, {"routes": []})
        self._send(404, {})

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + self.key:
            return self._send(401, {"error": "auth"})
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.count += 1
        import random
        h = self.honor
        msgs = b["messages"]
        prompt = msgs[-1]["content"]
        tk = b.get("chat_template_kwargs") or {}
        temp = b.get("temperature")
        greedy = (temp is None or temp == 0) or ("top_k" in h and b.get("top_k") == 1) or \
                 ("top_p" in h and b.get("top_p", 1) <= 0.05) or ("min_p" in h and b.get("min_p", 0) >= 0.9)
        if "seed" in h and b.get("seed") is not None:
            rng = random.Random(b["seed"])
        elif greedy:
            rng = random.Random(0)
        else:
            rng = random.Random()
        words = "lamp tide gull rock salt fog beam wave keel mast storm reef".split()
        if "apple" in prompt:
            pp = b.get("presence_penalty", 0) if "presence_penalty" in h else 0
            rp = b.get("repetition_penalty", 1) if "repetition_penalty" in h else 1
            text = " ".join(["apple"] * (30 if pp < 1 and rp < 1.5 else 3))
        elif prompt.startswith("Count from 1"):
            text = ",".join(str(i) for i in range(1, 11))
            if "stop" in h and b.get("stop"):
                text = text[:text.index(b["stop"][0])]
        elif "code word" in prompt:
            ok = "preserve_thinking" in h and tk.get("preserve_thinking") is True
            text = "ZEBRA7731" if ok else "UNKNOWN"
        elif b.get("tools"):
            long_ = "inventory.py" in prompt
            args = json.dumps({"path": "inventory.py" if long_ else "notes.txt",
                               "content": "\n".join("line %d = 'x'" % i for i in range(100 if long_ else 1))})
            return self._tool_reply(b, args)
        elif prompt.startswith("Write ") and "code block" in prompt:
            text = "```python\ndef f(x):\n    return x\n```"
        elif prompt.startswith("Return ONLY a JSON"):
            text = json.dumps([{"id": i, "name": "n%d" % i, "a": 1, "b": [1, 2]} for i in range(8)])
        else:
            n = b.get("max_tokens", 60) if "max_tokens" in h else 60
            text = " ".join(rng.choice(words) for _ in range(min(12, n)))
            if "max_tokens" in h and b.get("max_tokens", 99) <= 7:
                text = " ".join(text.split()[:b["max_tokens"]])
        if "Prove carefully" in prompt or "bat and a ball" in prompt or "17 * 23" in prompt or "n^2 + n + 41" in prompt:
            think = "enable_thinking" in h and tk.get("enable_thinking") is True
            if think:
                text = "<think>\nlet me think\n</think>\n\nanswer" if b.get("max_tokens", 0) > 200 else "<think>\nlet me think about this..."
                if "reasoning_effort" in h and b.get("reasoning_effort") == "low":
                    text = "<think>\nok"
            else:
                text = "answer"
        fin = "length" if (b.get("max_tokens", 999) <= 7 and "max_tokens" in h) else "stop"
        if "stop" in h and prompt.startswith("Count from 1") and b.get("stop"):
            fin = "stop"
        toks = len(text.split())
        if b.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for w in [text[:5], text[5:]]:
                self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": w}}]}) + "\n\n").encode())
            self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": fin}], "usage": {"completion_tokens": toks}}) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": fin}],
                         "usage": {"completion_tokens": toks}})

    def _tool_reply(self, b, args):
        if b.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            half = len(args) // 2
            for i, part in enumerate((args[:half], args[half:])):
                f = {"arguments": part}
                if i == 0:
                    f["name"] = "write_file"
                self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": f}]}}]}) + "\n\n").encode())
            self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": "write_file", "arguments": args}}]}, "finish_reason": "tool_calls"}],
            "usage": {"completion_tokens": 50}})


def selftest():
    import io
    import tempfile
    import contextlib
    failures = []

    def check(name, cond):
        if not cond:
            failures.append(name)
        print(("ok   " if cond else "FAIL ") + name)

    def serve(honor):
        Stub.honor = set(honor)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def verdict(rows, claim_part):
        for r in rows:
            if claim_part in r["claim"]:
                return r["result"]
        return None

    ALL = {"seed", "top_k", "top_p", "min_p", "presence_penalty", "repetition_penalty", "max_tokens", "stop",
           "enable_thinking", "preserve_thinking", "reasoning_effort"}
    # pure helper checks
    check("artefacts: word loop", "word_loop" in artefacts("a " * 3 + "go " * 12))
    check("artefacts: clean code", artefacts("def f():\n    return 1\n") == [])
    check("eval_json: fenced", eval_json("```json\n" + json.dumps([{"a": 1, "b": 2, "c": 3, "d": 4}] * 8) + "\n```")[:2] == (True, True))
    check("eval_json: broken", eval_json("[{")[0] is False)
    check("classify_tool: raw markup", classify_tool({"ok": True, "tool_calls": [], "finish": "stop", "content": "<tool_call><function=write_file>", "reasoning": ""})[0] == "raw_markup_in_content")
    check("classify_tool: bad json", classify_tool({"ok": True, "tool_calls": [{"name": "w", "arguments": "{\"path\": \"a"}], "finish": "tool_calls", "content": "", "reasoning": ""})[0] == "bad_json")
    pc = plan_counts([BIG, SMALL], N_TOOLS, N_TOOLS_9B, N_PAIRS)
    check("default plan (probes 1,2,3,5) within budget: %s" % pc, sum(v for k, v in pc.items() if k != "6") <= DEFAULT_BUDGET)

    for label, honor in (("all-honored", ALL), ("none-honored", set())):
        srv = serve(honor)
        c = Client("http://127.0.0.1:%d/v1" % srv.server_address[1], Stub.key, budget=1000, timeout=20)
        pre = snapshot(c, [BIG])
        r1 = probe1(c, [BIG, SMALL])["rows"]
        r2 = probe2(c, [BIG])["rows"]
        r3 = probe3(c, [BIG], 2, 2)["rows"]
        r5 = probe5(c, BIG, 2)
        r6 = probe6(c, [BIG, SMALL], 2)["rows"]
        check(label + ": probe6 rows + short tool variants structured", len(r6) >= 9 and all(x["result"].startswith("2/2") or x["result"].startswith("1/1") or x["result"].startswith("2/") for x in r6 if "short tool prompt" in x["claim"]))
        r4 = probe4(c, [BIG], pre, snapshot(c, [BIG]), None)["rows"]
        h = lambda part: verdict(r1, part)  # noqa: E731
        if label == "all-honored":
            check(label + ": seed honored", h("[35B] seed") == "HONORED")
            check(label + ": top_k honored", h("[35B] top_k") == "HONORED")
            check(label + ": top_p honored", h("[35B] top_p") == "HONORED")
            check(label + ": min_p honored", h("[35B] min_p") == "HONORED")
            check(label + ": presence honored", h("[35B] presence_penalty") == "HONORED")
            check(label + ": rep honored", h("[35B] repetition_penalty") == "HONORED")
            check(label + ": max_tokens honored", h("[35B] max_tokens") == "HONORED")
            check(label + ": stop honored", h("[35B] stop") == "HONORED")
            check(label + ": thinking honored", (h("[35B] chat_template_kwargs.enable_thinking") or "") == "HONORED")
            check(label + ": preserve honored", (h("preserve_thinking") or "").startswith("HONORED"))
            check(label + ": effort honored", h("reasoning_effort") == "HONORED")
        else:
            check(label + ": seed ignored", h("[35B] seed") == "IGNORED")
            check(label + ": top_k ignored", h("[35B] top_k") == "IGNORED")
            check(label + ": top_p ignored", h("[35B] top_p") == "IGNORED")
            check(label + ": min_p ignored", h("[35B] min_p") == "IGNORED")
            check(label + ": presence ignored", h("[35B] presence_penalty") == "IGNORED")
            check(label + ": max_tokens ignored", h("[35B] max_tokens") == "IGNORED")
            check(label + ": stop ignored", h("[35B] stop") == "IGNORED")
            check(label + ": preserve ineffective", (h("preserve_thinking") or "").startswith("NOT-EFFECTIVE"))
            check(label + ": effort ignored", (h("reasoning_effort") or "").startswith("IGNORED"))
        check(label + ": temperature honored by greedy stub", h("[35B] temperature") in ("HONORED", "IGNORED or forced-greedy"))
        check(label + ": tools 2/2 ok everywhere", all(r["result"].startswith("2/2 ok, 0 malformed") for r in r3))
        check(label + ": stream tool chunks counted", any("tool-call delta chunks per response=[2" in r["evidence"] for r in r3))
        check(label + ": probe2 rows", len(r2) == 2)
        check(label + ": probe5 counted", r5["res"]["code"]["0.0"]["n"] == 2 and r5["res"]["json"]["1.5"]["parse_fail"] == 0)
        check(label + ": mtp rows", "ACTIVE and USED" in r4[0]["result"])
        # key never appears in any stored/printed text
        blob = json.dumps([r1, r2, r3, r4, r5["rows"]])
        check(label + ": key not in output", Stub.key not in blob)
        srv.shutdown()

    # error redaction: wrong port -> error text must not carry the key
    c = Client("http://127.0.0.1:9/v1", Stub.key, budget=3, timeout=2)
    r = c.chat("m", U("hi"))
    check("connection error handled + redacted", (not r["ok"]) and Stub.key not in (r["error"] or ""))
    c = Client("http://127.0.0.1:9/v1", Stub.key, budget=1, timeout=2)
    c.chat("m", U("hi"))
    check("budget cap enforced", c.chat("m", U("hi"))["error"] == "BUDGET_EXHAUSTED")

    # full run + report path through CLI functions
    srv = serve(ALL)
    with tempfile.TemporaryDirectory() as td:
        lj = os.path.join(td, "local.json")
        json.dump({"base_url": "http://127.0.0.1:%d/v1" % srv.server_address[1], "api_key": Stub.key}, open(lj, "w"))
        outp = os.path.join(td, "out.json")
        ns = argparse.Namespace(local_json=lj, base_url=None, budget=1000, models=BIG, probes="1,2,3,4,5,6",
                                n_tools=2, n_tools_9b=2, n_pairs=2, out=outp, cache_dir=td)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run(ns)
            rep = io.StringIO()
        check("run() exit 0", rc == 0)
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            report(outp)
        txt = buf2.getvalue()
        check("report has tables for 6 probes", all(("## Probe %s" % i) in txt for i in "123456"))
        check("report omits key", Stub.key not in txt and Stub.key not in open(outp).read())
    srv.shutdown()
    print("SELFTEST %s (%d failures)" % ("PASS" if not failures else "FAIL", len(failures)))
    for f in failures:
        print("  failed:", f)
    return 1 if failures else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--out", default="darkbloom-probe-results.json")
    r.add_argument("--probes", default="1,2,3,4,5", help="comma list; 6 = follow-up/deepening probes (opt-in)")
    r.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    r.add_argument("--n-tools", type=int, default=N_TOOLS)
    r.add_argument("--n-tools-9b", type=int, default=N_TOOLS_9B)
    r.add_argument("--n-pairs", type=int, default=N_PAIRS)
    r.add_argument("--models", default="%s,%s" % (BIG, SMALL))
    r.add_argument("--local-json", default=os.environ.get("DARKBLOOM_LOCAL_JSON", str(Path.home() / ".darkbloom" / "local.json")))
    r.add_argument("--base-url", default=None)
    r.add_argument("--cache-dir", default="/Volumes/NVMe-Models/hf-cache")
    rp = sub.add_parser("report")
    rp.add_argument("results")
    pl = sub.add_parser("plan")
    pl.add_argument("--n-tools", type=int, default=N_TOOLS)
    pl.add_argument("--n-tools-9b", type=int, default=N_TOOLS_9B)
    pl.add_argument("--n-pairs", type=int, default=N_PAIRS)
    pl.add_argument("--with-followups", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.cmd == "run":
        return run(a)
    if a.cmd == "report":
        return report(a.results)
    if a.cmd == "plan":
        p = plan_counts([BIG, SMALL], a.n_tools, a.n_tools_9b, a.n_pairs)
        p.pop("6", None) if not getattr(a, "with_followups", False) else None
        print(p, "total", sum(p.values()))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
