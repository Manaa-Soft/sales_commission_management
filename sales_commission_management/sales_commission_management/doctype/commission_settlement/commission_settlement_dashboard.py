from frappe import _


def get_data():
    """Dashboard connections for Commission Settlement.

    Internal links are derived from the child table entries.
    """
    return {
        "fieldname": "name",
        "internal_links": {
            "Commission Entry": ["entries.commission_ledger_entry"],
            "Sales Invoice": ["entries.original_source_invoice"],
        },
        "transactions": [
            {"label": _("Commission"), "items": ["Commission Entry", "Sales Invoice"]},
            {"label": _("Payout"), "items": ["Commission Payout Event", "Payment Entry"]},
            {"label": _("Accounting & Settlement"), "items": ["Journal Entry", "Purchase Invoice", "Additional Salary"]},
        ],
    }