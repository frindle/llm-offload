#!/usr/bin/env python3
"""Queue-allowlisted runner for Pet Portrait Studio image jobs.
The ollama queue invokes it with exactly: --model --host --num-ctx --cwd --task-file
(so the render serialises on a GPU lane, VRAM-guarded, like the research/code-review runners).
--task-file points at a render-job JSON. We just re-exec it under the torch venv on render_exec.py."""
import argparse, subprocess, sys, os
from pathlib import Path
VENV_PY = "/Volumes/NVMe-Models/image-models/.difvenv/bin/python"
RENDER_EXEC = "/Volumes/NVMe-Models/pet-portrait-studio/backend/render_exec.py"
def main():
    ap = argparse.ArgumentParser()
    for f in ("--model","--host","--num-ctx","--cwd","--task-file"):
        ap.add_argument(f)
    a = ap.parse_args()
    env = dict(os.environ,
               HF_HOME="/Volumes/NVMe-Models/image-models/.hfcache",
               HF_TOKEN=subprocess.run(["security","find-generic-password","-s","huggingface","-w"],
                                       capture_output=True, text=True).stdout.strip())
    sys.exit(subprocess.run([VENV_PY, RENDER_EXEC, a.task_file], env=env).returncode)
if __name__=="__main__":
    main()
