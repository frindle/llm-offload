#!/usr/bin/env python3
"""Stage models Unraid -> local SSD, and evict them again, so Ollama always
serves from local disk.

THE ARCHITECTURE THIS RESTORES
------------------------------
Unraid is the STORE. The Mac holds only what it is about to use. Check local
first, pull from Unraid if absent, evict when done.

That was always the intent -- `ensure_model_cached()` in the worker implements
it -- but it has been dead code, because `OLLAMA_MODELS` pointed at the SMB share
itself. Every model was therefore "already visible" to the local server, the
cache check short-circuited on its first line, and Ollama served every model
straight off the network.

WHY IT MATTERS MORE THAN SPEED
------------------------------
Measured 2026-08-23: SMB 299 MB/s vs local SSD 2927 MB/s, ~10x. But the real
argument is RELIABILITY, and it is in the worker's own docstring: *Ollama's model
load path over SMB hangs indefinitely; a plain file copy from the same share does
not.* We watched exactly that happen -- qwen3-coder-next (51.7GB) failed to
finish loading inside the 900s warmup budget while the host thrashed at 2GB
available, and the positive control died with TimeoutError on its first task.

Copy-then-load-local converts an unbounded, thrash-prone network load into a
bounded file copy plus a fast local load.

SPACE
-----
The boot volume cannot hold everything (47GB free against ~70GB of models still
needed). So staging evicts: least-recently-used models that are not currently
required get removed to make room. `--keep` marks models that must never be
evicted during a run.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

LOCAL = Path.home() / ".ollama" / "models"
SMB = Path("/Volumes/data/ollama-models")
REGISTRY = "registry.ollama.ai"
MANIFEST_ROOT = f"manifests/{REGISTRY}/library"   # kept: library-only default path
DEFAULT_NAMESPACE = "library"

# Filesystem types a model must never be SERVED from. Loading over SMB hangs
# indefinitely (see module docstring); staging exists precisely to avoid it.
NETWORK_FS = {"smbfs", "nfs", "afpfs", "webdav", "ftp", "cifs"}


def split_ref(model: str):
    """'ns/name:tag' -> (namespace, name, tag). Bare names default to library.

    Ollama lays manifests out as manifests/<registry>/<namespace>/<name>/<tag>.
    The original code hardcoded the 'library' namespace, so any model published
    under a user namespace (benhaotang/, yasserrmd/, MFDoom/ are all present on
    this box today) resolved to a path that does not exist -- it silently
    reported NOT STAGED, and, worse, was invisible to local_models(), which is
    what evict() consults to decide a blob is unreferenced. See evict().
    """
    ref, _, tag = model.partition(":")
    if "/" in ref:
        namespace, _, name = ref.rpartition("/")
    else:
        namespace, name = DEFAULT_NAMESPACE, ref
    return namespace, name, (tag or "latest")


def canonical(model: str) -> str:
    """Normalised 'name:tag' / 'ns/name:tag' so --keep and eviction agree.

    Without this, --keep qwen3:8b would not protect a model that local_models()
    happened to report as library/qwen3:8b, and vice versa.
    """
    ns, name, tag = split_ref(model)
    return f"{name}:{tag}" if ns == DEFAULT_NAMESPACE else f"{ns}/{name}:{tag}"


def manifest_path(root: Path, model: str) -> Path:
    ns, name, tag = split_ref(model)
    return root / "manifests" / REGISTRY / ns / name / tag


def manifest_digests(mpath: Path):
    """Every blob this model references, config included."""
    m = json.loads(mpath.read_text())
    digests = []
    cfg = (m.get("config") or {}).get("digest")
    if cfg:
        digests.append(cfg)
    for layer in m.get("layers") or []:
        d = layer.get("digest")
        if d:
            digests.append(d)
    return [d.replace(":", "-") for d in digests]


def model_size(root: Path, model: str) -> int:
    mp = manifest_path(root, model)
    if not mp.is_file():
        return 0
    total = 0
    for d in manifest_digests(mp):
        b = root / "blobs" / d
        if b.is_file():
            total += b.stat().st_size
    return total


def free_bytes() -> int:
    st = os.statvfs(str(LOCAL))
    return st.f_bavail * st.f_frsize


def _fstype(path: Path) -> str:
    """Filesystem type backing `path`, via the real mount table."""
    try:
        out = subprocess.run(["df", str(path)], capture_output=True, text=True,
                             timeout=20).stdout.strip().splitlines()
        if len(out) < 2:
            return "unknown"
        dev = out[-1].split()[0]
        for line in subprocess.run(["mount"], capture_output=True, text=True,
                                   timeout=20).stdout.splitlines():
            if line.startswith(dev + " ") and "(" in line:
                return line.rsplit("(", 1)[1].split(",")[0].strip(") ")
    except Exception:
        pass
    return "unknown"


def check_mounts(need_smb: bool) -> bool:
    """Guard both ends of the copy before a single byte moves.

    Two distinct failure modes, both seen on this box:

    1. LOCAL silently sitting on a network mount. This is the original sin the
       module docstring describes -- OLLAMA_MODELS pointed at the SMB share, so
       'staging' was a no-op and every model was served over the wire. If that
       ever comes back, staging is worse than useless: it reports success while
       changing nothing.

    2. A mountpoint DIRECTORY that exists while the share behind it is not
       mounted. macOS leaves these behind after an unclean unmount, and reads
       return an empty directory rather than an error -- so stage() would report
       the honest-looking but wrong 'not present on Unraid' for every model.
       Checking the mount table, not just is_dir(), is what separates the two.
    """
    ok = True

    lfs = _fstype(LOCAL)
    if lfs in NETWORK_FS:
        print(f"  FATAL: local model store {LOCAL} is on a NETWORK filesystem "
              f"({lfs}). Models must never be served from here.", file=sys.stderr)
        ok = False

    env_models = os.environ.get("OLLAMA_MODELS")
    if env_models and Path(env_models).resolve() != LOCAL.resolve():
        print(f"  FATAL: OLLAMA_MODELS={env_models} does not match the staging "
              f"target {LOCAL}. Ollama would serve from somewhere else entirely.",
              file=sys.stderr)
        ok = False

    if need_smb:
        if not SMB.is_dir():
            print(f"  FATAL: source share {SMB} does not exist -- the share is "
                  f"not mounted.", file=sys.stderr)
            ok = False
        else:
            # Which mount actually backs this path? Deliberately NOT a
            # substring walk up the parents: every path on the system is under
            # '/', which is always mounted, so a parent walk can never fail and
            # would rubber-stamp a stale mountpoint. Ask df where the path
            # really lives instead, and require it to be a mount of its own.
            try:
                lines = subprocess.run(["df", str(SMB)], capture_output=True,
                                       text=True, timeout=20).stdout.strip().splitlines()
                mount_point = lines[-1].split(None, 8)[-1] if len(lines) >= 2 else "/"
            except Exception:
                mount_point = "/"
            if Path(mount_point) not in (SMB, *SMB.parents) or mount_point == "/":
                print(f"  FATAL: {SMB} is a plain directory on {mount_point}, not a "
                      f"mounted share -- stale mountpoint left by an unclean "
                      f"unmount. Reads would silently return nothing.", file=sys.stderr)
                ok = False
            elif not any(SMB.iterdir()):
                print(f"  FATAL: source share {SMB} is mounted but empty.", file=sys.stderr)
                ok = False
            elif not (SMB / "manifests").is_dir():
                print(f"  FATAL: {SMB} has no manifests/ -- this is not an Ollama "
                      f"model store.", file=sys.stderr)
                ok = False

    return ok


def verify_blobs(root: Path, model: str, quiet=False) -> bool:
    """Re-hash every blob and assert it matches the digest it is filed under.

    A blob's filename IS its sha256, so a mismatch means the bytes are not what
    the manifest says they are -- a truncated or corrupted copy. Ollama does not
    check this on load; it will happily serve a damaged model, and the failure
    surfaces later as garbage output or a load error attributed to the model
    rather than to the copy. On a benchmark that is a silently wrong row.
    """
    mp = manifest_path(root, model)
    if not mp.is_file():
        print(f"  {model}: no manifest at {mp}", file=sys.stderr)
        return False
    ok = True
    for d in manifest_digests(mp):
        b = root / "blobs" / d
        if not b.is_file():
            print(f"  {model}: MISSING blob {d}", file=sys.stderr)
            ok = False
            continue
        h = hashlib.sha256()
        with open(b, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 24), b""):
                h.update(chunk)
        want = d.replace("sha256-", "")
        if h.hexdigest() != want:
            print(f"  {model}: HASH MISMATCH {d}\n"
                  f"      on disk: sha256-{h.hexdigest()}", file=sys.stderr)
            ok = False
    if ok and not quiet:
        print(f"  {model}: hash-verified {len(manifest_digests(mp))} blobs")
    return ok


def is_staged(model: str) -> bool:
    mp = manifest_path(LOCAL, model)
    if not mp.is_file():
        return False
    # A manifest without all of its blobs is worse than no manifest -- Ollama
    # will list the model and then fail to load it.
    for d in manifest_digests(mp):
        if not (LOCAL / "blobs" / d).is_file():
            return False
    return True


def local_models(root: Path = None):
    """Every staged model, ACROSS ALL NAMESPACES, as (canonical_ref, mtime).

    Namespace-aware. The previous version walked only .../library, which made
    every user-namespace model invisible. That is a data-loss bug, not just a
    reporting gap: evict() subtracts the digests of *other* staged models to
    decide which blobs are safe to delete, so a blob shared with an unlisted
    user-namespace model looked unreferenced and was unlinked -- silently
    corrupting a model that was never named on the command line.
    """
    root = LOCAL if root is None else root
    base = root / "manifests" / REGISTRY
    out = []
    if not base.is_dir():
        return out
    for ns_dir in base.iterdir():
        if not ns_dir.is_dir():
            continue
        for name_dir in ns_dir.iterdir():
            if not name_dir.is_dir():
                continue
            for tag in name_dir.iterdir():
                if tag.is_file():
                    ref = (f"{name_dir.name}:{tag.name}"
                           if ns_dir.name == DEFAULT_NAMESPACE
                           else f"{ns_dir.name}/{name_dir.name}:{tag.name}")
                    out.append((ref, tag.stat().st_mtime))
    return out


def evict(model: str, dry=False) -> int:
    """Remove a model's manifest and any blob it alone references."""
    mp = manifest_path(LOCAL, model)
    if not mp.is_file():
        return 0
    mine = set(manifest_digests(mp))
    # Never delete a blob another staged model still needs. Compared on the
    # canonical ref so 'qwen3:8b' and 'library/qwen3:8b' are the same model and
    # we do not mistake a model for its own neighbour.
    me = canonical(model)
    for other, _ in local_models():
        if canonical(other) == me:
            continue
        omp = manifest_path(LOCAL, other)
        if omp.is_file():
            mine -= set(manifest_digests(omp))
    freed = 0
    for d in mine:
        b = LOCAL / "blobs" / d
        if b.is_file():
            freed += b.stat().st_size
            if not dry:
                b.unlink()
    if not dry:
        mp.unlink()
    return freed


def stage(model: str, keep, headroom_gb=6.0) -> bool:
    if is_staged(model):
        print(f"  {model}: already staged locally")
        return True

    smp = manifest_path(SMB, model)
    if not smp.is_file():
        print(f"  ERROR {model}: not present on Unraid at {smp}", file=sys.stderr)
        return False

    need = model_size(SMB, model)
    want = need + int(headroom_gb * 1024**3)
    print(f"  {model}: {need/1024**3:.1f}GB to copy, {free_bytes()/1024**3:.1f}GB free")

    # Evict least-recently-used models until it fits. `keep` protects the roster
    # entries we still need this session.
    if free_bytes() < want:
        cands = sorted((m for m, mt in local_models()
                        if canonical(m) not in keep and canonical(m) != canonical(model)),
                       key=lambda m: manifest_path(LOCAL, m).stat().st_mtime)
        for c in cands:
            if free_bytes() >= want:
                break
            freed = evict(c)
            print(f"    evicted {c}, freed {freed/1024**3:.1f}GB")

    if free_bytes() < want:
        print(f"  ERROR {model}: cannot free enough space "
              f"({free_bytes()/1024**3:.1f}GB free, need {want/1024**3:.1f}GB)", file=sys.stderr)
        return False

    t0 = time.time()
    (LOCAL / "blobs").mkdir(parents=True, exist_ok=True)
    for d in manifest_digests(smp):
        src, dst = SMB / "blobs" / d, LOCAL / "blobs" / d
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            continue
        # Copy to a temp name, HASH IT, and only then rename into place. Size
        # equality was the old check and it is not enough -- a copy interrupted
        # at exactly the right byte count, or a silently corrupted read off the
        # share, both pass a size check. The digest is the filename, so we can
        # verify against the name we are about to file it under.
        tmp = dst.with_suffix(".partial")
        shutil.copyfile(src, tmp)
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 24), b""):
                h.update(chunk)
        if h.hexdigest() != d.replace("sha256-", ""):
            tmp.unlink(missing_ok=True)
            print(f"  ERROR {model}: HASH MISMATCH copying {d} -- copy discarded",
                  file=sys.stderr)
            return False
        tmp.rename(dst)
    mp = manifest_path(LOCAL, model)
    mp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(smp, mp)
    el = time.time() - t0
    print(f"  {model}: staged {need/1024**3:.1f}GB in {el:.0f}s "
          f"({need/1024**2/max(el,1):.0f} MB/s)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["stage", "evict", "list", "check", "verify", "mounts"])
    ap.add_argument("models", nargs="*")
    ap.add_argument("--keep", default="", help="comma-separated models never to evict")
    args = ap.parse_args()
    # Canonicalised so --keep 'ornith-1.5:9b' protects the same entry that
    # local_models() reports, regardless of which side carries the namespace.
    keep = {canonical(k.strip()) for k in args.keep.split(",") if k.strip()}

    if args.action == "mounts":
        return 0 if check_mounts(need_smb=True) else 1

    if args.action == "verify":
        bad = 0
        for m in args.models:
            bad += 0 if verify_blobs(LOCAL, m) else 1
        return 1 if bad else 0

    if args.action == "list":
        print(f"  local cache: {LOCAL}   free on volume: {free_bytes()/1024**3:.1f}GB")
        for m, mt in sorted(local_models(), key=lambda x: -x[1]):
            sz = model_size(LOCAL, m) / 1024**3
            ok = "ok " if is_staged(m) else "INCOMPLETE"
            print(f"    {ok} {m:<44} {sz:6.1f}GB")
        return 0

    if args.action == "check":
        bad = 0
        for m in args.models:
            s = is_staged(m)
            print(f"  {m:<44} {'staged' if s else 'NOT STAGED'}")
            bad += 0 if s else 1
        return 1 if bad else 0

    if args.action == "evict":
        for m in args.models:
            print(f"  evicted {m}, freed {evict(m)/1024**3:.1f}GB")
        return 0

    # Mount check gates staging. Everything staging does is destructive on one
    # side (eviction) and trusting on the other (the share); neither is safe if
    # we are wrong about where those two ends actually are.
    needs_copy = [m for m in args.models if not is_staged(m)]
    if not check_mounts(need_smb=bool(needs_copy)):
        return 1

    ok = all(stage(m, keep) for m in args.models)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
