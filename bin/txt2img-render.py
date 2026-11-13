#!/usr/bin/env python3
"""Queue-allowlisted runner for pure text-to-image jobs.
Mirrors pet-portrait-render.py: the ollama queue invokes it with
--model --host --num-ctx --cwd --task-file so the render serialises on a GPU
lane (VRAM-guarded). --task-file points at a txt2img-job JSON; we re-exec it
under the torch venv on txt2img_exec.py."""
import argparse, subprocess, sys, os
VENV_PY = "/Volumes/NVMe-Models/image-models/.difvenv/bin/python"
EXEC = str(__import__("pathlib").Path(__file__).resolve().parent / "txt2img_exec.py")
def main():
    ap = argparse.ArgumentParser()
    for f in ("--model","--host","--num-ctx","--cwd","--task-file"):
        ap.add_argument(f)
    a = ap.parse_args()
    env = dict(os.environ,
               HF_HOME="/Volumes/NVMe-Models/image-models/.hfcache",
               HF_TOKEN=subprocess.run(["security","find-generic-password","-s","huggingface","-w"],
                                       capture_output=True, text=True).stdout.strip())
    sys.exit(subprocess.run([VENV_PY, EXEC, a.task_file], env=env).returncode)
if __name__=="__main__":
    main()
