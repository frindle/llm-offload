from ledger.core import transfer


def refund(customer_acct, merchant_acct, amount):
    """Move `amount` from the merchant back to the customer."""
    if amount <= 0:
        raise ValueError("refund must be positive")
    return transfer(src=merchant_acct, dst=customer_acct, amount=amount)
