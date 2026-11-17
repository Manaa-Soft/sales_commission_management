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
        {"fieldname": "posting_date", "label": _("Date"), "fieldtype": "Date", "width": 100},
        {"fieldname": "original_invoice", "label": _("Original Invoice"), "fieldtype": "Link", "options": "Sales Invoice", "width": 140},
        {"fieldname": "return_invoice", "label": _("Return Invoice"), "fieldtype": "Link", "options": "Sales Invoice", "width": 140},
        {"fieldname": "beneficiary_type", "label": _("Beneficiary Type"), "fieldtype": "Data", "width": 120},
        {"fieldname": "beneficiary", "label": _("Beneficiary"), "fieldtype": "Data", "width": 140},
        {"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 140},
        {"fieldname": "original_commission", "label": _("Original Commission"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "clawback_amount", "label": _("Clawback Amount"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "ratio", "label": _("Ratio"), "fieldtype": "Percent", "width": 80},
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
    ]


def get_data(filters):
    flt_filters = {"event_type": "Return Adjustment", "docstatus": 1}
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
        fields=["posting_date", "original_invoice", "return_invoice", "beneficiary_type",
                "sales_person", "sales_partner", "item_code", "earned_commission",
                "original_entry", "status"],
        limit_page_length=0,
    )

    data = []
    for r in rows:
        # look up original commission amount
        orig_commission = 0.0
        if r.original_entry:
            try:
                orig = frappe.get_doc("Commission Entry", r.original_entry)
                orig_commission = flt(orig.earned_commission)
            except Exception:
                orig_commission = 0.0
        clawback = flt(r.earned_commission)
        ratio = (abs(clawback) / orig_commission * 100.0) if orig_commission else 0.0
        data.append({
            "posting_date": r.posting_date,
            "original_invoice": r.original_invoice,
            "return_invoice": r.return_invoice,
            "beneficiary_type": r.beneficiary_type,
            "beneficiary": r.sales_person or r.sales_partner or "(group)",
            "item_code": r.item_code,
            "original_commission": flt(orig_commission, 2),
            "clawback_amount": flt(clawback, 2),
            "ratio": flt(ratio, 2),
            "status": r.status,
        })
    return data