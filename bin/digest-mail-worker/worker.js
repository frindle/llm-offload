// digest-mail: the error digest's one daily message -> one email (Cloudflare Worker, 2026-11-16).
//
// POST /send  {app, ref, subject, text}  with  Authorization: Bearer <token>
//   200 {ok: true, id}      sent
//   400 / 401 / 404         bad body / wrong token / anything else
//   409 {ok: false}         this app + ref was already sent (the digest retries are harmless)
//   502 {ok: false, code}   Cloudflare refused the send (code is its E_* string)
//
// Bindings (set at upload, none of them in this file):
//   EMAIL         send_email binding, restricted to ONE destination address and ONE sender address
//   FROM, TO      the sender and the recipient address (plain text); send() needs an explicit "to" even with
//                 destination_address set on the binding
//   TOKEN_SHA256  sha256 hex of the bearer token (secret); the token itself lives only in the queue's .env on Unraid
//   APPS          comma list of allowed app names (plain text), e.g. "error-digest"
//   SENT          optional KV namespace: remembers app+ref for 3 days so one day's digest is mailed once
//
// Sending to a verified destination address of the account is free on every plan (Email Routing domain as sender).

const enc = new TextEncoder();

async function sha256hex(s) {
  const d = await crypto.subtle.digest("SHA-256", enc.encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function sameString(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

const reply = (status, obj) => Response.json(obj, { status });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/send") return reply(404, { ok: false });

    const auth = request.headers.get("authorization") || "";
    const tok = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
    if (tok.length < 24 || !sameString(await sha256hex(tok), String(env.TOKEN_SHA256 || "").trim().toLowerCase())) {
      return reply(401, { ok: false });
    }

    let b;
    try {
      b = await request.json();
    } catch {
      return reply(400, { ok: false, error: "body is not JSON" });
    }
    const apps = String(env.APPS || "").split(",").map((s) => s.trim()).filter(Boolean);
    const app = String(b.app || "");
    const ref = String(b.ref || "").slice(0, 64);
    const subject = String(b.subject || "").replace(/[\r\n\t]+/g, " ").trim().slice(0, 200);
    const text = String(b.text || "").slice(0, 200000);
    if (!apps.includes(app)) return reply(400, { ok: false, error: "unknown app" });
    if (!subject || !text.trim()) return reply(400, { ok: false, error: "subject and text are required" });

    const key = ref ? `${app}:${ref}` : "";
    if (key && env.SENT && (await env.SENT.get(key))) return reply(409, { ok: false, error: "already sent" });

    let r;
    try {
      r = await env.EMAIL.send({
        from: { email: env.FROM, name: "Unraid digest" },
        to: env.TO,
        subject,
        text,
      });
    } catch (e) {
      return reply(502, { ok: false, code: e && e.code, error: String((e && e.message) || e).slice(0, 200) });
    }
    // The mail is gone: from here on the answer is 200 whatever happens. A KV write that fails must not turn a sent
    // mail into a 502, which the sender retries (a second mail); it only loses the dedupe for this one key.
    let noted = true;
    if (key && env.SENT) {
      try {
        await env.SENT.put(key, new Date().toISOString(), { expirationTtl: 3 * 86400 });
      } catch (e) {
        noted = false;
      }
    }
    return reply(200, { ok: true, id: r && r.messageId, ...(noted ? {} : { deduped: false }) });
  },
};
