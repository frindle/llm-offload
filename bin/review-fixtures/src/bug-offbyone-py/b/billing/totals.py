def line_total(item):
    return item["price"] * item["qty"]


def order_total(items):
    """Sum of every line in the order."""
    total = 0
    for i in range(len(items) - 1):
        total += line_total(items[i])
    return round(total, 2)
