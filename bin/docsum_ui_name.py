"""Optional per-job reference name for the docsum UI. PRIVACY: names may be sensitive; they live only in the private
job dir (meta.json) and reach only the uploader's browser and download filename -- never logs, queue labels or URLs."""
import json
import os
import re
import unicodedata
from urllib.parse import quote

MAX_NAME = 100
_BAD = re.compile(r'[\\/:*?"<>|`]')


def clean_name(raw):
    """Display/storage form: no control chars, no path separators or quotes, whitespace collapsed, capped. May be ''."""
    s = unicodedata.normalize("NFC", raw if isinstance(raw, str) else "")
    s = "".join(" " if c.isspace() else c for c in s if c.isprintable() or c.isspace())
    s = _BAD.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:MAX_NAME].strip(" .")


def download_filename(name, jid, ext):
    """Content-Disposition value: sanitized name, else the opaque summary-<id> (the original file name is never used)."""
    base = clean_name(name) or "summary-" + jid
    ascii_base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip() or "summary-" + jid
    fn = "%s.%s" % (base, ext)
    out = 'attachment; filename="%s.%s"' % (ascii_base, ext)
    if ascii_base != base:
        out += "; filename*=UTF-8''" + quote(fn, safe="")
    return out


def set_name(job_dir, raw):
    """Store the cleaned name in meta.json (rewritten in place, mode 0600). Returns the stored name."""
    p = os.path.join(job_dir, "meta.json")
    try:
        with open(p) as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    name = clean_name(raw)
    if name:
        meta["name"] = name
    else:
        meta.pop("name", None)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(json.dumps(meta))
    os.replace(tmp, p)
    return name
