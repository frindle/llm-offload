def next_delay(attempt, base=2, cap=300):
    """Seconds to wait before retry number `attempt` (0-based)."""
    return base ** attempt


def label(attempt):
    return f"retry-{attempt}"
