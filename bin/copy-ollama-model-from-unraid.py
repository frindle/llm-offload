#!/usr/bin/env python3
"""Copy one Ollama model's manifest + blobs between Mac Studio's local Ollama
storage (~/.ollama/models) and Unraid's mounted shared store
(/Volumes/data/ollama-models), skipping any blob already present at the
destination with a matching size.

Usage:
  copy-ollama-model-from-unraid.py pull <model>   # Unraid -> local (default direction)
  copy-ollama-model-from-unraid.py push <model>   # local -> Unraid (for a model Mac Studio
                                                    # just pulled fresh that Unraid doesn't have yet)

Exits 1 and prints NOT_FOUND_AT_SOURCE if the model's manifest isn't present
on the source side at all.
"""
import sys
import os
import json
import shutil

MOUNT_ROOT = "/Volumes/data/ollama-models"
LOCAL_ROOT = "/Volumes/NVMe-Models/ollama-models"


def manifest_rel_path(model):
    if ":" in model:
        name, tag = model.rsplit(":", 1)
    else:
        name, tag = model, "latest"
    # Namespaced models (e.g. "MFDoom/deepseek-r1-tool-calling") live under
    # registry.ollama.ai/<namespace>/<name>/<tag>; official library models
    # (e.g. "qwen2.5-coder") live under registry.ollama.ai/library/<name>/<tag>.
    if "/" in name:
        return f"registry.ollama.ai/{name}/{tag}"
    return f"registry.ollama.ai/library/{name}/{tag}"


def copy_model(model, src_root, dst_root):
    rel = manifest_rel_path(model)
    src_manifest = os.path.join(src_root, "manifests", rel)

    if not os.path.isfile(src_manifest):
        print("NOT_FOUND_AT_SOURCE")
        sys.exit(1)

    with open(src_manifest) as f:
        manifest = json.load(f)

    digests = [manifest["config"]["digest"]]
    digests += [layer["digest"] for layer in manifest.get("layers", [])]

    for digest in digests:
        blob_name = digest.replace("sha256:", "sha256-", 1)
        src_blob = os.path.join(src_root, "blobs", blob_name)
        dst_blob = os.path.join(dst_root, "blobs", blob_name)

        if not os.path.isfile(src_blob):
            print(f"MISSING_BLOB_AT_SOURCE: {blob_name}")
            sys.exit(1)

        if os.path.isfile(dst_blob) and os.path.getsize(dst_blob) == os.path.getsize(src_blob):
            print(f"SKIP (already at destination): {blob_name}")
            continue

        os.makedirs(os.path.dirname(dst_blob), exist_ok=True)
        size_gb = os.path.getsize(src_blob) / 1e9
        print(f"COPYING: {blob_name} ({size_gb:.2f}GB)")
        shutil.copyfile(src_blob, dst_blob)

    dst_manifest = os.path.join(dst_root, "manifests", rel)
    os.makedirs(os.path.dirname(dst_manifest), exist_ok=True)
    shutil.copyfile(src_manifest, dst_manifest)
    print("DONE")


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("pull", "push"):
        print("usage: copy-ollama-model-from-unraid.py {pull|push} <model>", file=sys.stderr)
        sys.exit(2)
    direction, model = sys.argv[1], sys.argv[2]
    if direction == "pull":
        copy_model(model, MOUNT_ROOT, LOCAL_ROOT)
    else:
        copy_model(model, LOCAL_ROOT, MOUNT_ROOT)


if __name__ == "__main__":
    main()
