import frappe
from frappe.utils import cint


def currency_precision(currency=None):
    """Return the decimal precision for financial amounts.

    Uses ERPNext's established precision logic:
    1. System Settings currency_precision (if configured).
    2. Number format decimal places from System Settings.
    3. Falls back to 2.

    This is the system-wide accounting precision, not a per-currency
    display precision. All GL and settlement amounts use this precision.
    """
    currency_precision = frappe.db.get_default("currency_precision")
    if currency_precision not in (None, ""):
        return cint(currency_precision)

    number_format = frappe.db.get_default("number_format") or "#,###.##"
    try:
        from frappe.utils import get_number_format_info
        return get_number_format_info(number_format)[2]
    except Exception:
        return 2