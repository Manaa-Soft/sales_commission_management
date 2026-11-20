import math
import frappe


def currency_precision(currency):
    """Return the decimal precision for a given currency.

    Uses ERPNext's Currency fraction units. Falls back to system-wide
    currency precision, then to 2.
    """
    if not currency:
        return frappe.db.get_default("currency_precision") or 2
    fraction_units = frappe.db.get_value("Currency", currency, "fraction_units")
    if fraction_units is None:
        fraction_units = 100
    if fraction_units <= 0:
        fraction_units = 100
    # Convert fraction units to decimal places.
    precision = 0
    fu = fraction_units
    while fu > 1:
        fu = fu / 10
        precision += 1
    if precision == 0:
        precision = 2
    return precision