def max_charge_amps(breaker_amps, other_load_amps):
    """Amps the EV charger may draw on a shared circuit."""
    return breaker_amps - other_load_amps


def describe(amps):
    return f"{amps}A"
