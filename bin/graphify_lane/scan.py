"""scan.py PROJECT_ROOT CACHE_ROOT -- runs under graphify's own python (the uv tool venv) on the Mac.
Prints JSON: {"version", "root", "files": {rel: [category, mtime_ns, size]}, "skipped_sensitive": [rel names]}.
category: detect()'s (code, document, image...) or "resolution" for its resolution_inputs (import-resolution files).
File list = graphify's own detect() (so .gitignore / .graphifyignore / sensitive-file rules match a real update).
cache_root keeps detect's stat cache out of the project (no stray graphify-out/ dirs)."""
import json
import os
import sys
from pathlib import Path

import graphify
from graphify.detect import detect


def main():
    root = Path(sys.argv[1]).resolve()
    cache = Path(sys.argv[2])
    cache.mkdir(parents=True, exist_ok=True)
    r = detect(root, cache_root=cache)
    files = {}

    def add(p, cat):
        try:
            rel = Path(p).resolve().relative_to(root)
        except (ValueError, OSError):
            return
        if not rel.parts or rel.parts[0] == "graphify-out":
            return
        try:
            st = os.stat(p)
        except OSError:
            return
        files[rel.as_posix()] = [cat, st.st_mtime_ns, st.st_size]

    for cat, lst in (r.get("files") or {}).items():
        cat = getattr(cat, "value", cat)
        for f in lst:
            p = Path(f)
            if p.is_dir():
                for dp, dns, fns in os.walk(p):
                    dns[:] = [d for d in dns if d not in (".git", "graphify-out", "node_modules")]
                    for n in fns:
                        add(os.path.join(dp, n), str(cat))
            else:
                add(p, str(cat))
    # package.json / tsconfig / lock files / go.mod...: not graph nodes, but update() reads them to resolve imports
    for f in r.get("resolution_inputs") or []:
        try:
            rel = Path(f).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        if rel not in files and Path(f).is_file():
            add(f, "resolution")
    sens = []
    for s in r.get("skipped_sensitive") or []:
        try:
            sens.append(Path(s).resolve().relative_to(root).as_posix())
        except (ValueError, OSError):
            sens.append(Path(str(s)).name)
    ver = getattr(graphify, "__version__", None)
    if not ver:
        from importlib.metadata import version
        ver = version("graphifyy")
    print(json.dumps({"version": ver, "root": str(root), "files": files, "skipped_sensitive": sens}))


if __name__ == "__main__":
    main()
