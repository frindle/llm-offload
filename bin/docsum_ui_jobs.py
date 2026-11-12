"""Job status view for the docsum web UI, read from the private job dirs. No document text ever leaves a job dir except
the original file name (meta.json), which the UI shows only in the browser."""


def read_json(path):
    import json
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def classify(st, queue_status, age):
    import re
    if "ok" in st:
        if st.get("ok") is True:
            pages = st["pages"] if isinstance(st.get("pages"), int) else None
            return ("done", None, pages)
        else:
            code = st.get("code")
            if isinstance(code, str) and re.fullmatch(r"E_[A-Z0-9_]{1,40}", code):
                return ("failed", code, None)
            return ("failed", "E_UNKNOWN", None)
    if queue_status == "running":
        return ("running", None, None)
    if queue_status in ("failed", "error", "needs_opus", "cancelled"):
        return ("failed", "E_JOB_FAILED", None)
    if queue_status is None and age > 600:
        return ("failed", "E_LOST", None)
    return ("queued", None, None)


def job_info(root, job_id, queue_status, now):
    import os
    d = os.path.join(root, job_id)
    if not os.path.isdir(d):
        return None
    meta = read_json(os.path.join(d, 'meta.json'))
    st = read_json(os.path.join(d, 'status.json'))
    try:
        start = os.path.getmtime(os.path.join(d, 'meta.json'))
    except OSError:
        start = now
    status, code, pages = classify(st, queue_status, now - start)
    end = now
    if 'ok' in st and status in ('done', 'failed'):
        try:
            end = os.path.getmtime(os.path.join(d, 'status.json'))
        except OSError:
            pass
    name = meta['src_name'] if isinstance(meta.get('src_name'), str) else ''
    title = meta['name'] if isinstance(meta.get('name'), str) else ''
    bts = meta['bytes'] if isinstance(meta.get('bytes'), int) else None
    has_pdf = status == 'done' and os.path.isfile(os.path.join(d, 'summary.pdf'))
    has_md = status == 'done' and os.path.isfile(os.path.join(d, 'summary.md'))
    return {
        'id': job_id,
        'status': status,
        'code': code,
        'pages': pages,
        'elapsed': int(end - start),
        'name': name,
        'title': title,
        'bytes': bts,
        'has_pdf': has_pdf,
        'has_md': has_md,
    }


def list_jobs(root, queue_rows, now):
    import os
    import re
    if not os.path.isdir(root):
        return []
    qs = {}
    if queue_rows:
        for row in queue_rows:
            label = str(row.get("label") or "")
            if label.startswith("docsum-"):
                qs[label[7:]] = row.get("status")
    results = []
    for jid in os.listdir(root):
        if not re.fullmatch(r"[0-9a-f]{8}", jid):
            continue
        meta_path = os.path.join(root, jid, "meta.json")
        if not os.path.isfile(meta_path):
            continue
        info = job_info(root, jid, qs.get(jid), now)
        if info is None:
            continue
        info["mtime"] = os.path.getmtime(meta_path)
        results.append(info)
    results.sort(key=lambda x: x["mtime"], reverse=True)
    for r in results:
        del r["mtime"]
    return results
