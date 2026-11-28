# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry import (
    _expand_sales_person,
)


def execute(filters=None):
    filters = filters or {}
    return get_columns(), get_data(filters)


def get_columns():
    return [
        {
            "fieldname": "posting_date",
            "label": _("Date"),
            "fieldtype": "Date",
            "width": 100,
        },
        {
            "fieldname": "sales_invoice",
            "label": _("Invoice"),
            "fieldtype": "Link",
            "options": "Sales Invoice",
            "width": 140,
        },
        {
            "fieldname": "beneficiary_type",
            "label": _("Beneficiary Type"),
            "fieldtype": "Data",
            "width": 120,
        },
        {
            "fieldname": "beneficiary",
            "label": _("Beneficiary"),
            "fieldtype": "Data",
            "width": 140,
        },
        {
            "fieldname": "earned_commission",
            "label": _("Earned"),
            "fieldtype": "Currency",
            "width": 110,
        },
        {
            "fieldname": "paid_amount",
            "label": _("Paid"),
            "fieldtype": "Currency",
            "width": 100,
        },
        {
            "fieldname": "paid_delta",
            "label": _("Allocation"),
            "fieldtype": "Currency",
            "width": 100,
        },
        {
            "fieldname": "outstanding_amount",
            "label": _("Outstanding"),
            "fieldtype": "Currency",
            "width": 110,
        },
        {
            "fieldname": "status",
            "label": _("Status"),
            "fieldtype": "Data",
            "width": 100,
        },
    ]


def get_data(filters):
    flt_filters = {"docstatus": 1}
    if filters.get("company"):
        flt_filters["company"] = filters["company"]
    if filters.get("from_date"):
        flt_filters["posting_date"] = [">=", filters["from_date"]]
    if filters.get("to_date"):
        if "posting_date" in flt_filters and isinstance(
            flt_filters["posting_date"], list
        ):
            flt_filters["posting_date"] = [
                "between",
                [filters["from_date"], filters["to_date"]],
            ]
        else:
            flt_filters["posting_date"] = ["<=", filters["to_date"]]
    if filters.get("sales_person"):
        expanded = _expand_sales_person(filters["sales_person"])
        if len(expanded) == 1:
            flt_filters["sales_person"] = expanded[0]
        else:
            flt_filters["sales_person"] = ["in", expanded]

    rows = frappe.get_list(
        "Commission Entry",
        filters=flt_filters,
        fields=[
            "posting_date",
            "sales_invoice",
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "earned_commission",
            "paid_amount",
            "paid_delta",
            "outstanding_amount",
            "status",
        ],
        order_by="posting_date asc",
        limit_page_length=0,
    )

    data = []
    for r in rows:
        data.append(
            {
                "posting_date": r.posting_date,
                "sales_invoice": r.sales_invoice,
                "beneficiary_type": r.beneficiary_type,
                "beneficiary": r.sales_person or r.sales_partner,
                "earned_commission": flt(r.earned_commission, 2),
                "paid_amount": flt(r.paid_amount, 2),
                "paid_delta": flt(r.paid_delta, 2),
                "outstanding_amount": flt(r.outstanding_amount, 2),
                "status": r.status,
            }
        )
    return data
