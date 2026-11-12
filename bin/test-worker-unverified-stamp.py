#!/usr/bin/env python3
"""Regression test for the research "unverified provenance" harness stamp.

Guards the false-signal fix (job 8d690b764cec): a --task-kind research dispatch
whose real sources are LOCAL repo files uses read_file/grep, correctly makes zero
web_fetch calls, and must NOT be stamped "claims of verification can't be trusted."
The stamp is reserved for a research answer that shows NO verification of any kind.

Tests the REAL decision function (should_stamp_unverified) and the REAL local-read
classifier (_command_is_local_read) imported from ollama-worker.py -- not a mirror.

Run: python3 test-worker-unverified-stamp.py
"""
import importlib.util
import sys

WORKER = "/Users/user/bin/ollama-worker.py"


def _load():
    spec = importlib.util.spec_from_file_location("ollama_worker_undertest", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    m = _load()
    stamp = m.should_stamp_unverified
    is_read = m._command_is_local_read

    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"  FAIL {name}: got {got!r}, want {want!r}")
        else:
            print(f"  ok   {name}")

    print("== primary property: research + 0 web_fetch + local reads ==")
    # (a) research, zero web_fetch, >=1 local read -> NO warning (the false-signal case)
    check("(a) research, 0 fetch, 1 local read -> no stamp",
          stamp("research", web_fetch_succeeded=False, local_read_count=1, facts_provided=False),
          False)
    check("(a') research, 0 fetch, many local reads -> no stamp",
          stamp("research", web_fetch_succeeded=False, local_read_count=9, facts_provided=False),
          False)

    # (b) research, zero web_fetch, zero local reads -> warning PRESENT (the real signal)
    check("(b) research, 0 fetch, 0 local read -> stamp",
          stamp("research", web_fetch_succeeded=False, local_read_count=0, facts_provided=False),
          True)

    # (c) coding kind unaffected -- never stamped by this path
    check("(c) coding, 0 fetch, 0 local read -> no stamp",
          stamp("coding", web_fetch_succeeded=False, local_read_count=0, facts_provided=False),
          False)
    check("(c') coding, 0 fetch, 1 local read -> no stamp",
          stamp("coding", web_fetch_succeeded=False, local_read_count=1, facts_provided=False),
          False)

    print("== web-research path stays intact ==")
    # A genuine web-research task that DID fetch is never stamped.
    check("research, web_fetch succeeded, 0 local -> no stamp",
          stamp("research", web_fetch_succeeded=True, local_read_count=0, facts_provided=False),
          False)
    # A genuine web-research task that fetched nothing AND read nothing local is STILL flagged.
    check("research, 0 fetch, 0 local (web task, no sources) -> stamp",
          stamp("research", web_fetch_succeeded=False, local_read_count=0, facts_provided=False),
          True)
    # facts_provided (pass-2 synthesis) is exempt by design even with zero of everything.
    check("research, facts_provided, 0 fetch, 0 local -> no stamp",
          stamp("research", web_fetch_succeeded=False, local_read_count=0, facts_provided=True),
          False)

    print("== _command_is_local_read classifier ==")
    for cmd in ["grep -rn foo .", "  rg 'pattern' src/", "cat app/page.tsx",
                "sed -n '1,40p' file.py", "find . -name '*.ts'", "git log --oneline",
                "git grep TODO", "head -50 README.md", "ls -la", "sudo grep x y"]:
        check(f"read-cmd: {cmd!r}", is_read(cmd), True)
    for cmd in ["rm -rf build", "git commit -m x", "npm run build",
                "echo done", "python3 script.py", "mv a b",
                "rm foo && grep bar baz", ""]:
        check(f"non-read-cmd: {cmd!r}", is_read(cmd), False)

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failing)")
        print("\n".join(failures))
        sys.exit(1)
    print("RESULT: PASS (all checks green)")
    sys.exit(0)


if __name__ == "__main__":
    main()
