#!/usr/bin/env python3
"""Is this model USABLE in a schema pipeline? A gross-fitness probe, not a score.

Written 2026-08-31. The OpenResearcher episode is the motivation: a model with a
strong published benchmark (54.8% BrowseComp-Plus) that could not hold the schema
contract at all -- it emitted search-query strings where sub-questions were asked
for, reproducibly. No headline number predicted that, and the research bench could
not either: with four scoring bugs found in a single three-question comparison, it
cannot adjudicate gaps under ~0.15. It is an instrument for GROSS FITNESS.

So test fitness directly, and cheaply, before spending a bench run:
  1. honours a JSON schema at all
  2. respects enum constraints
  3. respects maxItems
  4. answers the field ASKED FOR rather than a plausible neighbour  <- the OR failure
  5. returns an EMPTY collection instead of inventing filler
  6. stays greedy-fast (a collapse to empty in <1s is the latency tell)

Usage: model-fitness.py --model M [--host URL] [--num-ctx N]
Exit 0 = fit for schema pipelines, 1 = not fit.
"""
import argparse, json, sys, time, urllib.request

def chat(host, model, system, user, schema, num_ctx, num_predict=800):
    body = {"model": model, "stream": False, "think": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "options": {"temperature": 0, "num_ctx": num_ctx,
                        "num_predict": num_predict},
            "format": schema, "keep_alive": "10m"}
    req = urllib.request.Request(f"{host.rstrip('/')}/api/chat",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    return data.get("message", {}).get("content", ""), time.time() - t0


CHECKS = []
def check(name, critical=False):
    def deco(fn):
        CHECKS.append((name, fn, critical)); return fn
    return deco


@check("returns parseable JSON for a schema", critical=True)
def c1(host, model, ctx):
    sch = {"type": "object", "properties": {"answer": {"type": "string"}},
           "required": ["answer"]}
    raw, el = chat(host, model, "Answer in JSON.", "What is the capital of France?", sch, ctx)
    d = json.loads(raw)
    return ("answer" in d and "paris" in str(d["answer"]).lower(),
            f"{el:.1f}s -> {str(d)[:60]}")


@check("respects an enum constraint", critical=True)
def c2(host, model, ctx):
    sch = {"type": "object", "properties": {
        "verdict": {"type": "string", "enum": ["YES", "NO"]}}, "required": ["verdict"]}
    raw, el = chat(host, model, "Answer with the verdict field only.",
                   "Is 7 greater than 3?", sch, ctx)
    v = json.loads(raw).get("verdict")
    return (v in ("YES", "NO"), f"{el:.1f}s -> verdict={v!r}")


@check("respects maxItems", critical=False)
def c3(host, model, ctx):
    sch = {"type": "object", "properties": {"items": {
        "type": "array", "maxItems": 3, "items": {"type": "string"}}},
        "required": ["items"]}
    raw, el = chat(host, model, "Return JSON.",
                   "List 10 primary colours used in web design.", sch, ctx)
    n = len(json.loads(raw).get("items", []))
    return (n <= 3, f"{el:.1f}s -> {n} items (cap 3)")


@check("answers the FIELD ASKED FOR, not a plausible neighbour", critical=True)
def c4(host, model, ctx):
    # The exact OpenResearcher failure: asked for sub-QUESTIONS, it emitted search
    # QUERIES -- same topic, wrong artifact. A schema pipeline that files the answer
    # under the wrong key silently loses it.
    sch = {"type": "object", "properties": {"sub_questions": {
        "type": "array", "maxItems": 3,
        "items": {"type": "string"}}}, "required": ["sub_questions"]}
    raw, el = chat(host, model,
                   "Decompose the task into SUB-QUESTIONS. Each MUST be a complete "
                   "interrogative sentence ending in '?'. Do NOT emit search keywords.",
                   "Research question: how much power does an RTX 3080 draw?", sch, ctx)
    qs = json.loads(raw).get("sub_questions", [])
    ok = bool(qs) and all(str(q).strip().endswith("?") for q in qs)
    return (ok, f"{el:.1f}s -> {sum(1 for q in qs if str(q).strip().endswith('?'))}"
                f"/{len(qs)} are questions | e.g. {str(qs[0])[:52] if qs else '-'!r}")


@check("returns EMPTY rather than inventing filler", critical=True)
def c5(host, model, ctx):
    sch = {"type": "object", "properties": {"findings": {
        "type": "array", "items": {"type": "string"}}}, "required": ["findings"]}
    raw, el = chat(host, model,
                   "Report only defects literally present. If none, return an empty array.",
                   "Diff:\n```\n-# spelling: recieve\n+# spelling: receive\n```", sch, ctx)
    f = json.loads(raw).get("findings", [])
    return (len(f) == 0, f"{el:.1f}s -> {len(f)} finding(s) on a comment typo fix")


@check("no instant-empty collapse (latency tell)", critical=False)
def c6(host, model, ctx):
    sch = {"type": "object", "properties": {"steps": {
        "type": "array", "items": {"type": "string"}}}, "required": ["steps"]}
    raw, el = chat(host, model, "Return JSON.",
                   "List the steps to safely rotate an API key in production.", sch, ctx)
    n = len(json.loads(raw).get("steps", []))
    return (not (el < 1.0 and n == 0), f"{el:.1f}s -> {n} step(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--host", default="http://198.51.100.145:11434")  # Unraid; Studio Ollama is retired
    ap.add_argument("--num-ctx", type=int, default=8192)
    a = ap.parse_args()
    print(f"=== fitness: {a.model} on {a.host}\n")
    hard = soft = 0
    for name, fn, crit in CHECKS:
        try:
            ok, detail = fn(a.host, a.model, a.num_ctx)
        except Exception as e:
            ok, detail = False, f"ERROR {type(e).__name__}: {str(e)[:70]}"
        tag = "PASS" if ok else ("FAIL" if crit else "warn")
        print(f"  [{tag}] {name}\n         {detail}")
        if not ok:
            if crit: hard += 1
            else: soft += 1
    print()
    if hard:
        print(f"NOT FIT for schema pipelines: {hard} critical check(s) failed.")
        print("Use it in a ReAct loop if you want it at all -- do not wire it to a schema.")
        return 1
    print(f"FIT for schema pipelines ({soft} soft warning(s)).")
    print("Fitness is a floor, not a ranking: it says the model can hold the")
    print("contract, not that it is the best available.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
