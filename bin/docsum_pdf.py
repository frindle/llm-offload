"""Markdown-ish summary text to a simple PDF (stdlib only, Helvetica)."""

import textwrap


def layout_lines(md, width=90, per_page=50):
    lines = md.split('\n')
    raw_pairs = []
    for line in lines:
        if not line.strip():
            raw_pairs.append(("body", ""))
        elif line.startswith('# '):
            raw_pairs.append(("h1", line[2:].strip()))
        elif line.lstrip().startswith('#'):
            stripped = line.lstrip('#').strip()
            raw_pairs.append(("h2", stripped))
        elif line.lstrip().startswith('- ') or line.lstrip().startswith('* '):
            stripped = line.lstrip()
            if stripped.startswith('- '):
                text = stripped[2:].strip()
            else:
                text = stripped[1:].strip()
            raw_pairs.append(("bullet", text))
        else:
            raw_pairs.append(("body", line.strip()))

    wrapped = []
    for style, text in raw_pairs:
        if not text:
            wrapped.append((style, ""))
        elif style == "bullet":
            pieces = textwrap.wrap(text, width=width, initial_indent="- ", subsequent_indent="  ")
            for piece in pieces:
                wrapped.append((style, piece))
        else:
            pieces = textwrap.wrap(text, width=width)
            for piece in pieces:
                wrapped.append((style, piece))

    pages = []
    for i in range(0, max(len(wrapped), 1), per_page):
        pages.append(wrapped[i:i+per_page])

    return pages


def render_pdf(pages):
    if not pages:
        pages = [[]]
    style_map = {"h1": ("F2", 16), "h2": ("F2", 13), "bullet": ("F1", 10), "body": ("F1", 10)}
    repl = {0x2013: "-", 0x2014: "-", 0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2022: "-", 0x2026: "..."}
    def escape(s):
        out = []
        for c in s:
            if c in "\\()":
                out.append("\\")
            out.append(c)
        return "".join(out)
    def make_stream(page_lines):
        lines = []
        y = 750.0
        for style, text in page_lines:
            fn, size = style_map.get(style, ("F1", 10))
            translated = "".join(repl.get(ord(c), c) for c in text)
            escaped = escape(translated)
            encoded = escaped.encode("latin-1", "replace").decode("latin-1")
            lines.append(f"BT /{fn} {size} Tf 50 {y:.1f} Td ({encoded}) Tj ET")
            y -= (size + 4)
        return "\n".join(lines)
    num_pages = len(pages)
    N = 5 + 2 * num_pages
    parts = []
    parts.append("%PDF-1.4\n")
    offsets = []
    obj = "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    offsets.append(len("".join(parts)))
    parts.append(obj)
    kids = " ".join(f"{5+2*i} 0 R" for i in range(num_pages))
    obj = f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {num_pages} >>\nendobj\n"
    offsets.append(len("".join(parts)))
    parts.append(obj)
    obj = "3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    offsets.append(len("".join(parts)))
    parts.append(obj)
    obj = "4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>\nendobj\n"
    offsets.append(len("".join(parts)))
    parts.append(obj)
    for i, page_lines in enumerate(pages):
        pid = 5 + 2 * i
        sid = 6 + 2 * i
        stream_text = make_stream(page_lines)
        length = len(stream_text)
        obj = f"{pid} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {sid} 0 R /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>\nendobj\n"
        offsets.append(len("".join(parts)))
        parts.append(obj)
        obj = f"{sid} 0 obj\n<< /Length {length} >>\nstream\n{stream_text}\nendstream\nendobj\n"
        offsets.append(len("".join(parts)))
        parts.append(obj)
    xref = f"xref\n0 {N}\n"
    xref += "0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n"
    trailer = f"trailer\n<< /Size {N} /Root 1 0 R >>\nstartxref\n{len(''.join(parts))}\n%%EOF"
    parts.append(xref)
    parts.append(trailer)
    return "".join(parts).encode("latin-1")
