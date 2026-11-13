#!/usr/bin/env python3
"""docsum-ui in the container: its upload and delete routes refuse in standby and while draining; unchanged otherwise.
Runs the real server on loopback with made-up data and a fake enqueue (nothing reaches a queue)."""
import http.client, json, os, sys, tempfile, threading

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
bad = 0


def check(label, ok):
    global bad
    print(("ok   " if ok else "FAIL ") + label)
    bad += 0 if ok else 1


def post(port, path, body=b"{}"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, body=body, headers={"Origin": "http://127.0.0.1:%d" % port, "Host": "127.0.0.1:%d" % port,
                                                  "Content-Type": "application/json", "Content-Length": str(len(body))})
    r = c.getresponse()
    d = r.read()
    c.close()
    try:
        return r.status, json.loads(d)
    except Exception:
        return r.status, {}


with tempfile.TemporaryDirectory() as t:
    os.environ["QUEUE_STATE_DIR"] = os.path.join(t, "state")
    os.makedirs(os.environ["QUEUE_STATE_DIR"])
    os.environ.pop("QUEUE_STANDBY", None)
    import docsum_server
    import queue_config
    enq = []
    cfg = docsum_server.Config(root=os.path.join(t, "priv"), rows=lambda: [], enqueue=lambda j, m: enq.append(j) or True,
                               resolve=lambda *a, **k: enq.append("resolve"))
    srv = docsum_server.make_server(cfg, "127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    check("default (Mac today): upload is not frozen", post(port, "/api/upload")[0] != 503)
    check("default: delete is not frozen", post(port, "/api/delete/0123abcd")[0] != 503)
    os.environ["QUEUE_STANDBY"] = "1"
    s, b = post(port, "/api/upload")
    check("standby: upload 503 E_STANDBY", s == 503 and b.get("error") == "E_STANDBY")
    s, b = post(port, "/api/delete/0123abcd")
    check("standby: delete 503 E_STANDBY", s == 503 and b.get("error") == "E_STANDBY")
    check("standby: nothing enqueued or resolved", enq == [])
    check("standby: rename is not a queue write (not frozen)", post(port, "/api/rename/0123abcd")[0] != 503)
    del os.environ["QUEUE_STANDBY"]
    queue_config.drain_marker().write_text("{}")
    s, b = post(port, "/api/upload")
    check("draining: upload 503 E_DRAINING", s == 503 and b.get("error") == "E_DRAINING")
    queue_config.drain_marker().unlink()
    check("drain marker removed: upload not frozen", post(port, "/api/upload")[0] != 503)
    srv.shutdown()
    os.environ["DOCSUM_PRIVATE_ROOT"] = os.path.join(t, "elsewhere")
    import importlib
    importlib.reload(docsum_server)
    check("DOCSUM_PRIVATE_ROOT overrides the private root", docsum_server.PRIVATE_ROOT == os.path.join(t, "elsewhere"))
import configparser
c = configparser.RawConfigParser(strict=True)
c.read(os.path.join(HERE, "queue-image", "supervisord.conf"))
progs = [s for s in c.sections() if s.startswith("program:")]
check("supervisord has 9 programs incl. docsum-ui", len(progs) == 9 and "program:docsum-ui" in progs)
d = c["program:docsum-ui"]
check("docsum-ui autostarts in standby and binds loopback", d["autostart"] == "true" and "--host 127.0.0.1" in d["command"])
sys.path.insert(0, os.path.join(HERE, "..", "qd-standby-dash", "src"))
try:
    import docsum_proxy
    os.environ.pop("QUEUE_DOCSUM_UPSTREAM", None)
    check("proxy default upstream is the in-container docsum-ui", docsum_proxy.current_upstream() == ("127.0.0.1", 7690))
except ImportError:
    pass
print("%d failed" % bad)
sys.exit(1 if bad else 0)
