"""Let this process (and every child it spawns) MATERIALIZE iCloud-evicted files.

Root cause (2026-10-02, Rivian s1 054f6829bd98 / aec13ee90411): "Optimize Mac
Storage" evicts files under ~/Desktop, including the Desktop repos' .git metadata
(worktree HEAD/commondir, packed-refs, ...). A process whose
IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES policy is not ON gets EDEADLK ("Resource
deadlock avoided") instead of a download when it reads one, so git in the queue's
context died with "not a git repository: (null)" / "couldn't read
.git/packed-refs: Resource deadlock avoided" -- while the same command from an
interactive Claude shell (policy inherited ON) worked. That split is what made the
authoring self-check and a clean re-measure disagree.

The policy is per-process and inherited across fork/exec, so setting it once at the
top of a long-lived daemon or a launcher covers every git / verify / node child.
`enable()` is idempotent, never raises, and is a no-op off macOS."""
import sys

IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3
IOPOL_SCOPE_PROCESS = 0
IOPOL_MATERIALIZE_DATALESS_FILES_ON = 2


def current():
    """The process's policy (2 == ON), or None when it cannot be read."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        return libc.getiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                                   IOPOL_SCOPE_PROCESS)
    except Exception:
        return None


def enable():
    """Turn materialization ON for this process. True when it is ON afterwards."""
    if sys.platform != "darwin":
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        libc.setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                            IOPOL_SCOPE_PROCESS, IOPOL_MATERIALIZE_DATALESS_FILES_ON)
    except Exception:
        return False
    return current() == IOPOL_MATERIALIZE_DATALESS_FILES_ON


def enable_from(bin_dir):
    """For scripts that are not on sys.path: import this module from bin_dir and
    enable(). Never raises."""
    try:
        import importlib.util
        from pathlib import Path
        p = Path(bin_dir) / "dataless_policy.py"
        spec = importlib.util.spec_from_file_location("dataless_policy", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.enable()
    except Exception:
        return False


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        # Start from a process whose policy is OFF (as a launchd job's can be),
        # enable() there, and prove a grandchild inherits ON.
        import subprocess
        here = __file__
        code = ("import ctypes,subprocess,sys,importlib.util\n"
                "c=ctypes.CDLL(None); c.setiopolicy_np(3,0,1); a=c.getiopolicy_np(3,0)\n"
                "s=importlib.util.spec_from_file_location('dp',%r); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
                "ok=m.enable()\n"
                "g=subprocess.run([sys.executable,'-c','import ctypes;print(ctypes.CDLL(None).getiopolicy_np(3,0))'],capture_output=True,text=True).stdout.strip()\n"
                "print(a, ok, g)\n") % here
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.split()
        print("policy before / enable() / grandchild:", out)
        sys.exit(0 if out == ["1", "True", "2"] else 1)
    print(current())
