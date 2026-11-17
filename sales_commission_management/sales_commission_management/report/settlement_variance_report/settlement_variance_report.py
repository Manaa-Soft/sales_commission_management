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
        {"fieldname": "settlement", "label": _("Settlement"), "fieldtype": "Link", "options": "Commission Settlement", "width": 140},
        {"fieldname": "beneficiary", "label": _("Beneficiary"), "fieldtype": "Data", "width": 140},
        {"fieldname": "payable", "label": _("Payable"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "settled", "label": _("Settled"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "variance", "label": _("Variance"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
    ]


def get_data(filters):
    flt_filters = {}
    if filters.get("company"):
        flt_filters["company"] = filters["company"]

    rows = frappe.get_list(
        "Commission Settlement",
        filters=flt_filters,
        fields=["name", "sales_person", "sales_partner", "beneficiary_type",
                "total_payable", "total_paid", "outstanding", "status"],
        order_by="creation desc",
        limit_page_length=0,
    )

    data = []
    for r in rows:
        payable = flt(r.total_payable)
        settled = flt(r.total_paid)
        variance = payable - settled
        data.append({
            "settlement": r.name,
            "beneficiary": r.sales_person or r.sales_partner,
            "payable": flt(payable, 2),
            "settled": flt(settled, 2),
            "variance": flt(variance, 2),
            "status": r.status,
        })
    return data