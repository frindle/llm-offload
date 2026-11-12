"""Scenario-focused summarisation prompts and map-reduce orchestration (model passed in as a callable)."""
from docsum_chunk import chunk_pages

SECTIONS = ["Bottom line", "Key facts", "Risks and gaps", "Citations"]


def build_messages(scenario, text, kind):
    if kind == "map":
        task = (
            "Extract only what matters for this scenario from the excerpt. "
            "Cite page numbers like (p. 3). Output terse bullet notes."
        )
    elif kind == "single":
        task = (
            "Write a Markdown summary of the document for this scenario, "
            "with exactly these sections, one per line:\n"
            + "\n".join("## " + s for s in SECTIONS)
            + "\nCite page numbers like (p. 3) throughout."
        )
    elif kind == "reduce":
        task = (
            "Write a Markdown summary of the notes labelled with page ranges "
            "for this scenario, with exactly these sections, one per line:\n"
            + "\n".join("## " + s for s in SECTIONS)
            + "\nCite page numbers like (p. 3) throughout."
        )
    else:
        raise ValueError(f"Unknown kind: {kind}")

    system = (
        "You write focused summaries for one specific reader and decision. "
        "Quote exact figures, dates and names from the text with their page number. "
        "Never invent content that is not in the text. "
        "Say plainly when the text does not say something the reader needs."
    )

    user = "SCENARIO:\n" + scenario + "\n\nTASK:\n" + task + "\n\nTEXT:\n" + text
    return system, user


def summarize(pages, scenario, call, max_chars=24000):
    chunks = chunk_pages(pages, max_chars)
    if len(chunks) <= 1:
        text = chunks[0]["text"] if chunks else ""
        summary = call(*build_messages(scenario, text, "single"))
        return {"summary": summary, "calls": 1, "chunks": len(chunks)}

    # Map phase: one call per chunk
    notes = []
    for chunk in chunks:
        output = call(*build_messages(scenario, chunk["text"], "map"))
        notes.append((chunk["start"], chunk["end"], output))

    # Reduce phase: while the rendered notes are too long for one reduce call, merge consecutive notes in
    # greedy groups of at most max_chars (one "map" call per group) and repeat; each pass must shrink the list.
    def render(group):
        return "\n\n".join("[Pages %d-%d]\n%s" % n for n in group)

    calls = len(notes)
    while len(render(notes)) > max_chars:
        groups, cur, cur_len = [], [], 0
        for n in notes:
            n_len = len(render([n]))
            if cur and cur_len + 2 + n_len > max_chars:
                groups.append(cur)
                cur, cur_len = [], 0
            cur_len = n_len if not cur else cur_len + 2 + n_len
            cur.append(n)
        groups.append(cur)
        if len(groups) == len(notes):
            raise ValueError("E_REDUCE_OVERFLOW")
        notes = [(g[0][0], g[-1][1], call(*build_messages(scenario, render(g), "map"))) for g in groups]
        calls += len(groups)

    summary = call(*build_messages(scenario, render(notes), "reduce"))
    return {"summary": summary, "calls": calls + 1, "chunks": len(chunks)}
