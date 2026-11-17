#!/usr/bin/env python3
"""Tests for digest-mail-worker/worker.js (the error digest's mail Worker), 2026-11-17.

Runs the Worker under node with a fake env (fake send_email binding, fake KV): no network, no Cloudflare.
  python3 test-digest-mail-worker.py      prints ALL PASS / N FAILED (rc 0 / 1)
DIGEST_MAIL_WORKER=<path to a worker.js> runs the same checks against another copy (e.g. an older commit's).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

HARNESS = r"""
import { webcrypto, createHash } from "node:crypto";
import { pathToFileURL } from "node:url";
if (!globalThis.crypto) globalThis.crypto = webcrypto;
const worker = (await import(pathToFileURL(process.argv[2]).href)).default;

const TOKEN = "test-token-not-real-".padEnd(32, "x");   // the Worker wants 24+ characters
const out = [];
const chk = (name, ok, info) => out.push({ name, ok: !!ok, info: ok ? null : info });

function fakeEnv(opt = {}) {
  const kv = new Map(), sent = [], puts = [];
  const env = {
    TOKEN_SHA256: createHash("sha256").update(TOKEN).digest("hex"),
    APPS: "error-digest",
    FROM: "from@example.invalid",
    TO: "to@example.invalid",
    EMAIL: {
      send: async (m) => {
        sent.push(m);
        if (opt.sendFails) throw Object.assign(new Error("refused"), { code: "E_TEST" });
        return { messageId: "m" + sent.length };
      },
    },
    SENT: {
      get: async (k) => (kv.has(k) ? kv.get(k) : null),
      put: async (k, v) => {
        puts.push(k);
        if (opt.putFails) throw new Error("kv down");
        kv.set(k, v);
      },
    },
  };
  return { env, kv, sent, puts };
}

const body = { app: "error-digest", ref: "2026-11-16", subject: "Unraid: 1 thing for you", text: "one line" };
const post = (b = body, tok = TOKEN, method = "POST") =>
  new Request("https://digest.example.invalid/send", {
    method,
    headers: { authorization: "Bearer " + tok, "content-type": "application/json" },
    body: method === "POST" ? JSON.stringify(b) : undefined,
  });
async function call(req, env) {
  const r = await worker.fetch(req, env);
  let j = null;
  try { j = await r.json(); } catch {}
  return { status: r.status, j };
}

let f = fakeEnv();
let r = await call(post(body, "wrong-token-0123456789abcdef"), f.env);
chk("a wrong token: 401, nothing sent", r.status === 401 && f.sent.length === 0, r);
r = await call(post(body, TOKEN, "GET"), f.env);
chk("anything but POST /send: 404", r.status === 404, r);
r = await call(post({ ...body, app: "other" }), f.env);
chk("an app not in APPS: 400, nothing sent", r.status === 400 && f.sent.length === 0, r);
r = await call(post(), f.env);
chk("a good request: 200, one mail, the day remembered", r.status === 200 && r.j.ok && f.sent.length === 1
    && f.kv.has("error-digest:2026-11-16") && !("deduped" in r.j), r);
r = await call(post(), f.env);
chk("the same app and day again: 409, no second mail", r.status === 409 && f.sent.length === 1, r);

f = fakeEnv({ putFails: true });
r = await call(post(), f.env);
chk("the mail went but remembering it failed (KV down): still 200, so the sender does not send again",
    r.status === 200 && r.j.ok && f.sent.length === 1 && f.puts.length === 1 && r.j.deduped === false, r);

f = fakeEnv({ sendFails: true });
r = await call(post(), f.env);
chk("Cloudflare refuses the send: 502 with its code, the day not remembered", r.status === 502
    && r.j.code === "E_TEST" && f.puts.length === 0, r);

console.log(JSON.stringify(out));
"""


def main():
    worker = os.environ.get("DIGEST_MAIL_WORKER") or os.path.join(HERE, "digest-mail-worker", "worker.js")
    node = shutil.which("node")
    if not node:
        print("FAIL node is not on PATH (the gate has it in ~/gate-tools/bin)")
        return 1
    tmp = tempfile.mkdtemp(prefix="dmw-test-")
    try:
        # .mjs copies: worker.js is an ES module without a package.json saying so
        shutil.copy(worker, os.path.join(tmp, "worker.mjs"))
        with open(os.path.join(tmp, "harness.mjs"), "w") as f:
            f.write(HARNESS)
        p = subprocess.run([node, os.path.join(tmp, "harness.mjs"), os.path.join(tmp, "worker.mjs")],
                           capture_output=True, text=True, timeout=60, cwd=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        res = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print("FAIL the harness did not finish (rc %s)\n     %s" % (p.returncode, (p.stdout + p.stderr)[-1500:]))
        return 1
    fails = [c["name"] for c in res if not c["ok"]]
    for c in res:
        print(("ok   " if c["ok"] else "FAIL ") + c["name"] + ("" if c["ok"] else "\n     " + json.dumps(c["info"])))
    print("\n%d/%d passed" % (len(res) - len(fails), len(res)))
    print("ALL PASS" if not fails else "%d FAILED: %s" % (len(fails), "; ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
