"""Argument validation and private job preparation for the doc-summary CLI. Errors are E_* codes only, never content."""
MIN_CTX, MAX_CTX = 4096, 131072


MODEL_PREFERENCE = ("qwen3.6-35b", "gemma-4-26b", "qwen3.5-35b", "qwen3.5-9b")


def _rank(model_id):
    low = model_id.lower()
    for i, key in enumerate(MODEL_PREFERENCE):
        if key in low:
            return i
    return len(MODEL_PREFERENCE)


def current_model(served=None):
    """The best model Darkbloom has loaded right now (MODEL_PREFERENCE order; unknown ids last, list order kept). No model is ever chosen by the user."""
    try:
        if served is None:
            import plex_bypass
            served = plex_bypass.served_model_ids
        ids = [m for m in served() if isinstance(m, str) and m]
    except Exception:
        ids = []
    if not ids:
        raise ValueError("E_MODEL_NOT_SERVED")
    return min(ids, key=_rank)


def parse_args(argv):
    import argparse
    import os

    class _Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError("E_USAGE")

    parser = _Parser(add_help=False)
    parser.add_argument("pdf")
    parser.add_argument("--scenario", default="")
    parser.add_argument("--ctx", default=None)

    a = parser.parse_args(argv)
    pdf = a.pdf
    scenario = a.scenario
    ctx = a.ctx

    if not os.path.isfile(pdf):
        raise ValueError("E_NO_FILE")

    if not pdf.lower().endswith(".pdf"):
        raise ValueError("E_NOT_PDF")

    try:
        with open(pdf, "rb") as f:
            head = f.read(5)
    except Exception:
        raise ValueError("E_NOT_PDF")

    if head != b"%PDF-":
        raise ValueError("E_NOT_PDF")

    if not scenario.strip():
        raise ValueError("E_NO_SCENARIO")

    if ctx is not None:
        try:
            ci = int(ctx)
            if str(ci) != str(ctx):
                raise ValueError("E_BAD_CTX")
        except (ValueError, TypeError):
            raise ValueError("E_BAD_CTX")
        if ci < MIN_CTX or ci > MAX_CTX:
            raise ValueError("E_BAD_CTX")
        ctx = ci

    return {"pdf": pdf, "scenario": scenario, "ctx": ctx}


def prepare_job(pdf, scenario, root, job_id=None):
    import json, os, secrets, shutil
    if job_id is None:
        job_id = secrets.token_hex(4)
    d = os.path.join(root, job_id)
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    if os.path.exists(d):
        raise ValueError("E_JOB_EXISTS")
    os.makedirs(d, mode=0o700)
    os.chmod(d, 0o700)
    shutil.copyfile(pdf, os.path.join(d, "input.pdf"))
    os.chmod(os.path.join(d, "input.pdf"), 0o600)
    with open(os.path.join(d, "scenario.txt"), "w") as f:
        f.write(scenario)
    os.chmod(os.path.join(d, "scenario.txt"), 0o600)
    meta = {"src_name": os.path.basename(pdf), "bytes": os.path.getsize(pdf)}
    with open(os.path.join(d, "meta.json"), "w") as f:
        f.write(json.dumps(meta))
    os.chmod(os.path.join(d, "meta.json"), 0o600)
    task = {"job": job_id, "root": str(root)}
    task_path = os.path.join(d, "task.json")
    with open(task_path, "w") as f:
        f.write(json.dumps(task))
    os.chmod(task_path, 0o600)
    return {"job_id": job_id, "dir": d, "task_file": task_path}
