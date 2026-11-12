#!/usr/bin/env python3
"""vault-log-append against a local fake shim: appends, rolls to -part2 on 413, never sends the token in output, kill switch,
default note is this month's dispatch log, an explicit --base-note is honoured."""
import importlib.machinery, importlib.util, io, os, tempfile, threading, time
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
here = Path(__file__).resolve().parent
ld = importlib.machinery.SourceFileLoader("v", str(here / "vault-log-append")); sp = importlib.util.spec_from_loader("v", ld); V = importlib.util.module_from_spec(sp); ld.exec_module(V)
B = time.strftime("Claude/Agent-Dispatch-Log-%Y-%m")
seen, full = [], {f"/vault/{B}.md"}
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        assert self.headers["Authorization"] == "Bearer SECRET123"
        self.send_response(413 if self.path in full else 200); self.end_headers(); seen.append((self.path, body))
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
tok = Path(tempfile.mkdtemp()) / "tok"; tok.write_text("SECRET123\n")
os.environ.update(VAULT_URL=f"http://127.0.0.1:{srv.server_port}", VAULT_TOKEN_FILE=str(tok))
buf = io.StringIO()
with redirect_stdout(buf):
    assert V.main(["hello note"]) == 0
assert buf.getvalue().strip() == f"{B}-part2.md" and "SECRET123" not in buf.getvalue(), buf.getvalue()
assert seen[-1] == (f"/vault/{B}-part2.md", "hello note\n") and len(seen) == 2, seen
full.add(f"/vault/{B}-part2.md")
buf = io.StringIO()
with redirect_stdout(buf):
    V.main(["x"])
assert "part3" in buf.getvalue()
buf = io.StringIO()
with redirect_stdout(buf):
    assert V.main(["--base-note", "Claude/Projects/demo", "z"]) == 0
assert buf.getvalue().strip() == "Claude/Projects/demo.md" and seen[-1] == ("/vault/Claude/Projects/demo.md", "z\n"), (buf.getvalue(), seen[-1])
n = len(seen); os.environ["VAULT_LOG_APPEND"] = "off"; V.main(["y"]); assert len(seen) == n
print("test-vault-log-append ok")
