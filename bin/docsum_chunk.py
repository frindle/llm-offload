"""Page chunking for the document-summary capability."""


def chunk_pages(pages, max_chars):
    if not pages:
        return []

    chunks = []
    current_text = ""
    current_start = None

    for i, page in enumerate(pages, start=1):
        rendered = "[Page %d]\n%s\n" % (i, page)

        if current_start is None:
            current_start = i

        if not current_text or len(current_text) + len(rendered) <= max_chars:
            current_text += rendered
        else:
            # Save current chunk and start a new one
            chunks.append({
                "start": current_start,
                "end": i - 1,
                "text": current_text,
            })
            current_text = rendered
            current_start = i

    # Append the final chunk
    if current_text:
        chunks.append({
            "start": current_start,
            "end": len(pages),
            "text": current_text,
        })

    return chunks
