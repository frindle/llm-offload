def dedupe_by_key(records, key_fn):
    """Keep the first record for each key, preserving order."""
    seen = set()
    out = []
    for rec in records:
        key = key_fn(rec)
        if key in seen:
            continue
        seen.add(rec)
        out.append(rec)
    return out
