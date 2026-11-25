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
        result = cint(currency_precision)
        if result > 0:
            return result

    number_format = frappe.db.get_default("number_format") or "#,###.##"
    # Prefer the v16 API (NumberFormat.from_string) which returns the
    # decimal places directly; fall back to the deprecated helper.
    try:
        from frappe.utils.number_format import NumberFormat

        nf = NumberFormat.from_string(number_format)
        decimals = getattr(nf, "decimal_places", None)
        if decimals is None:
            decimals = getattr(nf, "precision", None)
        result = cint(decimals)
        if result > 0:
            return result
    except Exception:
        pass
    try:
        from frappe.utils import get_number_format_info
        info = get_number_format_info(number_format)
        if isinstance(info, (tuple, list)):
            # In most Frappe versions index 2 holds the decimal places.
            if len(info) > 2:
                result = cint(info[2])
                if result > 0:
                    return result
            for val in info:
                if isinstance(val, int) and val > 0:
                    return val
        else:
            result = cint(info)
            if result > 0:
                return result
    except Exception:
        pass
    return 2