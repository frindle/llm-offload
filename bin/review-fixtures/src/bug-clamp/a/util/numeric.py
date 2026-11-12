import math


def clamp(x, lo, hi):
    """Constrain x to the closed interval [lo, hi]."""
    if lo > hi:
        raise ValueError("lo must be <= hi")
    return max(lo, min(x, hi))


def lerp(a, b, t):
    return a + (b - a) * clamp(t, 0.0, 1.0)
