#!/usr/bin/env python3
"""DRAFT queue-allowlisted runner for Pet Portrait Studio "Make it move" jobs.

Mirrors pet-portrait-render.py exactly: the ollama queue invokes it with
  --model --host --num-ctx --cwd --task-file
so the clip render serialises on the Studio GPU lane (one at a time, no MPS
memory spillover -- same lane the SDXL renders use). --task-file points at an
img2vid-job JSON; we re-exec it under the torch/diffusers venv on
img2vid_exec.py.

Deploy target (after review): ~/bin/img2vid-render.py, and add it to the
ollama-queue runner allowlist next to pet-portrait-render.py / txt2img-render.py.
"""
import argparse, subprocess, sys, os

VENV_PY = "/Volumes/NVMe-Models/image-models/.difvenv/bin/python"
# Sits next to render_exec.py in the PPS backend once finalised.
EXEC = "/Volumes/NVMe-Models/pet-portrait-studio/backend/img2vid_exec.py"


def main():
    ap = argparse.ArgumentParser()
    for f in ("--model", "--host", "--num-ctx", "--cwd", "--task-file"):
        ap.add_argument(f)
    a = ap.parse_args()
    env = dict(
        os.environ,
        HF_HOME="/Volumes/NVMe-Models/image-models/.hfcache",
        HF_TOKEN=subprocess.run(
            ["security", "find-generic-password", "-s", "huggingface", "-w"],
            capture_output=True, text=True).stdout.strip(),
        # LTX rotary embeddings historically tripped an MPS float64 assert; let
        # unsupported ops fall back to CPU rather than crash the clip.
        PYTORCH_ENABLE_MPS_FALLBACK="1",
    )
    sys.exit(subprocess.run([VENV_PY, EXEC, a.task_file], env=env).returncode)


if __name__ == "__main__":
    main()
