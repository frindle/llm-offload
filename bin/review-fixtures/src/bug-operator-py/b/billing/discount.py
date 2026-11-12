def apply_discount(price, qty, discount):
    """Order subtotal after a flat discount; never negative."""
    subtotal = price * qty + discount
    return max(subtotal, 0)
