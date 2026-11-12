import os
import subprocess
import sys

import bloomgauge_lease_guard as G


def test_live_processes_are_alive():
    assert G.pid_alive(os.getpid()) is True
    assert G.pid_alive(str(os.getpid())) is True
    assert G.pid_alive(1) is True          # launchd: exists even when signalling it is not permitted


def test_dead_or_bad_pids_are_not_alive():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert G.pid_alive(p.pid) is False
    assert G.pid_alive(0) is False and G.pid_alive(-5) is False
    assert G.pid_alive(None) is False and G.pid_alive("x") is False
