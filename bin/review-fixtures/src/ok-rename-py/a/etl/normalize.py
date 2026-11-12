def normalize(xs):
    t = sum(xs)
    if t == 0:
        return [0.0 for _ in xs]
    return [x / t for x in xs]
