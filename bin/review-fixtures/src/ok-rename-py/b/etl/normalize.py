def normalize(values):
    """Scale values so they sum to 1 (all zeros if the sum is 0)."""
    total = sum(values)
    if total == 0:
        return [0.0 for _ in values]
    return [v / total for v in values]
