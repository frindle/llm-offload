#!/usr/bin/env python3
"""Text-to-image executor. Runs UNDER the torch venv (.difvenv/python3.12).
Pure txt2img (no init image) via StableDiffusionXLPipeline.from_single_file on a
staged SDXL photoreal checkpoint (RealVisXL / Juggernaut). Model loaded once,
then loops over N seeds. Writes PNGs + a status.json, mirroring render_exec.py.

job JSON:
 {"job_id","prompt","negative","model","steps","guidance","width","height",
  "count","seed","out_dir"}
Writes: <out_dir>/<job_id>/<idx>.png  and  <out_dir>/<job_id>/status.json
"""
import sys, json, time, os, traceback, signal
from pathlib import Path


def _write_status(sdir, **kw):
    (sdir / "status.json").write_text(json.dumps(kw))


def main():
    job = json.loads(Path(sys.argv[1]).read_text())
    out = Path(job["out_dir"]) / job["job_id"]
    out.mkdir(parents=True, exist_ok=True)
    count = int(job.get("count", 1))
    steps = int(job.get("steps", 40))
    guidance = float(job.get("guidance", 6.0))
    w = int(job.get("width", 1024))
    h = int(job.get("height", 1024))
    base_seed = int(job.get("seed", 1000))
    results = [{"idx": i, "done": False, "url": None} for i in range(count)]

    def _on_term(signum, frame):
        _write_status(out, status="cancelled", results=results, error="stopped")
        os._exit(0)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, _on_term)

    _write_status(out, status="running", results=results, error=None)
    FAST = bool(os.environ.get("PPS_FAST"))
    if FAST:
        steps, w, h = min(steps, 6), 512, 512
    init_path = job.get("init_image")
    strength = float(job.get("strength", 0.55))
    try:
        import torch
        from diffusers import (StableDiffusionXLPipeline, StableDiffusionXLImg2ImgPipeline,
                               DPMSolverMultistepScheduler)
        if init_path:
            from PIL import Image
            pipe = StableDiffusionXLImg2ImgPipeline.from_single_file(job["model"], torch_dtype=torch.float16).to("mps")
        else:
            pipe = StableDiffusionXLPipeline.from_single_file(job["model"], torch_dtype=torch.float16).to("mps")
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config, use_karras_sigmas=True)
        init_img = None
        if init_path:
            init_img = Image.open(init_path).convert("RGB").resize((w, h), Image.LANCZOS)
        for i in range(count):
            t0 = time.time()
            g = torch.Generator(device="mps").manual_seed(base_seed + i * 13)
            if init_path:
                img = pipe(prompt=job["prompt"], negative_prompt=job.get("negative", ""),
                           image=init_img, strength=strength,
                           num_inference_steps=steps, guidance_scale=guidance, generator=g).images[0]
            else:
                img = pipe(prompt=job["prompt"], negative_prompt=job.get("negative", ""),
                           width=w, height=h, num_inference_steps=steps,
                           guidance_scale=guidance, generator=g).images[0]
            img.save(out / f"{i}.png")
            results[i] = {"idx": i, "done": True, "url": f"/api/result/{job['job_id']}/{i}.png", "secs": round(time.time() - t0)}
            _write_status(out, status="running", results=results, error=None)
            print(f"[txt2img] {i}.png ({results[i]['secs']}s)", flush=True)
        _write_status(out, status="done", results=results, error=None)
    except Exception as e:
        _write_status(out, status="error", results=results, error=str(e))
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
