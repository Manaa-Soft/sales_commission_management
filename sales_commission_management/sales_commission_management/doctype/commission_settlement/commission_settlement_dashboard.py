from frappe import _


def get_data():
    """Dashboard connections for Commission Settlement.

    Internal links are derived from the child table entries.
    """
    import frappe

    internal_links = {
        "Commission Entry": ["entries", "commission_ledger_entry"],
        "Sales Invoice": ["entries", "original_source_invoice"],
    }
    # Conditionally include Additional Salary only when HRMS is installed.
    if frappe.db.exists("DocType", "Additional Salary"):
        internal_links["Additional Salary"] = ["commission_settlement"]

    return {
        "fieldname": "name",
        "internal_links": internal_links,
        "transactions": [
            {"label": _("Commission"), "items": ["Commission Entry", "Sales Invoice"]},
            {"label": _("Payout"), "items": ["Commission Payout Event", "Payment Entry"]},
            {"label": _("Accounting & Settlement"), "items": ["Journal Entry", "Purchase Invoice", "Additional Salary"]},
        ],
    }
PY