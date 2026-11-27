#!/usr/bin/env python3
"""Terminal records (done.json) and terminal events carry failure_class, terminal_reason and cancel_cause."""
import importlib.util as u, json, os, sys, tempfile
from pathlib import Path
os.environ["HOME"] = tempfile.mkdtemp(prefix="tf-")
HERE = Path(__file__).resolve().parent
sp = u.spec_from_file_location("q_tf", HERE / "ollama-queue.py")
q = u.module_from_spec(sp); sp.loader.exec_module(q)
td = Path(tempfile.mkdtemp()); q.LOG_DIR = td
fails = []
def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)

j = {"id": "tf0000000001", "label": "a", "status": "cancelled"}
q._ensure_terminal_fields(j)
chk("cancelled: reason/class/cause filled", (j["terminal_reason"], j["failure_class"], j["cancel_cause"]) == ("cancelled", "operator", "unspecified"))
j = {"id": "tf0000000002", "label": "b", "status": "cancelled", "cancel_cause": "api:delete", "failure_class": "x"}
q._ensure_terminal_fields(j)
chk("existing values are kept", j["cancel_cause"] == "api:delete" and j["failure_class"] == "x")
j = {"id": "tf0000000003", "label": "c", "status": "failed"}
q._ensure_terminal_fields(j)
chk("failed with nothing: never null", bool(j["terminal_reason"]) and bool(j["failure_class"]))
j = {"id": "tf0000000004", "label": "d", "status": "done"}
q._ensure_terminal_fields(j)
chk("done jobs get no terminal_reason", not j.get("terminal_reason"))
j = {"id": "tf0000000005", "label": "e", "status": "cancelled", "cancel_cause": "cli:cancel"}
q._persist_job_completion(j)
rec = json.loads((td / "tf0000000005.done.json").read_text())
chk("done.json records the three fields", rec.get("failure_class") == "operator" and rec.get("terminal_reason") == "cancelled" and rec.get("cancel_cause") == "cli:cancel")

# event log carries them
os.environ.update(QEL_DIR=tempfile.mkdtemp(), QEL_LOG_DIR=str(td), QEL_STATE_PATH=str(td / "nostate.json"),
                  QEL_STATUS_CMD="true")
sp = u.spec_from_file_location("qel", HERE / "queue-event-log.py")
e = u.module_from_spec(sp); sp.loader.exec_module(e)
e.poll_once(); (td / "tf0000000006.done.json").write_text(json.dumps(
    {"id": "tf0000000006", "label": "f", "status": "cancelled", "terminal_reason": "cancelled",
     "failure_class": "operator", "cancel_cause": "api:delete"}))
e.poll_once()
evs = [json.loads(l) for l in open(os.path.join(os.environ["QEL_DIR"], "queue-events.jsonl"))]
ev = [x for x in evs if x["id"] == "tf0000000006"]
chk("event has class/reason/cause", bool(ev) and ev[0].get("failure_class") == "operator" and ev[0].get("cancel_cause") == "api:delete" and ev[0].get("terminal_reason") == "cancelled")
print("FAIL: " + ", ".join(fails) if fails else "TERMINAL_FIELDS_OK")
sys.exit(1 if fails else 0)
