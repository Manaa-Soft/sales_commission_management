# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
    filters = filters or {}
    return get_columns(), get_data(filters)


def get_columns():
    return [
        {"fieldname": "sales_invoice", "label": _("Sales Invoice"), "fieldtype": "Link", "options": "Sales Invoice", "width": 140},
        {"fieldname": "beneficiary_type", "label": _("Beneficiary Type"), "fieldtype": "Data", "width": 120},
        {"fieldname": "beneficiary", "label": _("Beneficiary"), "fieldtype": "Data", "width": 140},
        {"fieldname": "earned_commission", "label": _("Earned"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "paid_amount", "label": _("Collected/Paid"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "outstanding_amount", "label": _("Outstanding"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "collection_pct", "label": _("Collection %"), "fieldtype": "Percent", "width": 100},
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
    ]


def get_data(filters):
    flt_filters = {"docstatus": 1}
    if filters.get("company"):
        flt_filters["company"] = filters["company"]
    if filters.get("from_date"):
        flt_filters["posting_date"] = [">=", filters["from_date"]]
    if filters.get("to_date"):
        if "posting_date" in flt_filters and isinstance(flt_filters["posting_date"], list):
            flt_filters["posting_date"] = ["between", [filters["from_date"], filters["to_date"]]]
        else:
            flt_filters["posting_date"] = ["<=", filters["to_date"]]

    rows = frappe.get_list(
        "Commission Entry",
        filters=flt_filters,
        fields=["sales_invoice", "beneficiary_type", "sales_person", "sales_partner",
                "earned_commission", "paid_amount", "outstanding_amount", "status"],
        limit_page_length=0,
    )

    data = []
    for r in rows:
        earned = flt(r.earned_commission)
        paid = flt(r.paid_amount)
        pct = (paid / earned * 100.0) if earned else 0.0
        data.append({
            "sales_invoice": r.sales_invoice,
            "beneficiary_type": r.beneficiary_type,
            "beneficiary": r.sales_person or r.sales_partner,
            "earned_commission": flt(r.earned_commission, 2),
            "paid_amount": flt(r.paid_amount, 2),
            "outstanding_amount": flt(r.outstanding_amount, 2),
            "collection_pct": flt(pct, 2),
            "status": r.status,
        })
    return data