"""Minimal multipart/form-data parser for the docsum upload form. Errors are codes only, never content."""


def parse_multipart(body, content_type):
    import re
    # Extract boundary from content_type
    if not content_type or not isinstance(content_type, str):
        raise ValueError("E_BAD_UPLOAD")
    m = re.search(r'boundary="?([A-Za-z0-9\-_]+)"?', content_type, re.IGNORECASE)
    if not m:
        raise ValueError("E_BAD_UPLOAD")
    boundary = m.group(1)
    parts = body.split(b"--" + boundary.encode())
    # Skip preamble (before first delimiter)
    if not parts:
        raise ValueError("E_BAD_UPLOAD")
    result = {}
    count = 0
    for part in parts[1:]:
        # Check for closing delimiter (starts with --)
        if part.startswith(b"--"):
            break
        count += 1
        if count > 32:
            raise ValueError("E_BAD_UPLOAD")
        # Strip exactly one leading \r\n and one trailing \r\n
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        # Split headers from data at first \r\n\r\n
        hdr_end = part.find(b"\r\n\r\n")
        if hdr_end < 0:
            raise ValueError("E_BAD_UPLOAD")
        hdr_bytes = part[:hdr_end]
        data = part[hdr_end + 4:]
        # Decode headers
        hdr_text = hdr_bytes.decode("utf-8", errors="replace")
        # Parse Content-Disposition
        disp_match = re.search(r'Content-Disposition:\s*form-data\s*;\s*(.*)', hdr_text, re.IGNORECASE)
        if not disp_match:
            raise ValueError("E_BAD_UPLOAD")
        params_str = disp_match.group(1)
        # Extract name="..."
        name_m = re.search(r'(?:^|;)\s*name="([^"]*)"', params_str)
        if not name_m:
            raise ValueError("E_BAD_UPLOAD")
        name = name_m.group(1)
        # Extract filename="..." (optional)
        fn_m = re.search(r';\s*filename="([^"]*)"', params_str)
        filename = fn_m.group(1) if fn_m else None
        result[name] = {"filename": filename, "data": data}
    if not result:
        raise ValueError("E_BAD_UPLOAD")
    return result
