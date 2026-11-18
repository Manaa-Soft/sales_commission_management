# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _


def execute(filters=None):
    filters = filters or {}
    return get_columns(), get_data(filters)


def get_columns():
    return [
        {
            "fieldname": "name",
            "label": _("Batch"),
            "fieldtype": "Link",
            "options": "Commission Accrual Batch",
            "width": 140,
        },
        {
            "fieldname": "company",
            "label": _("Company"),
            "fieldtype": "Link",
            "options": "Company",
            "width": 120,
        },
        {
            "fieldname": "period_from",
            "label": _("Period From"),
            "fieldtype": "Date",
            "width": 100,
        },
        {
            "fieldname": "period_to",
            "label": _("Period To"),
            "fieldtype": "Date",
            "width": 100,
        },
        {
            "fieldname": "accrual_date",
            "label": _("Accrual Date"),
            "fieldtype": "Date",
            "width": 100,
        },
        {
            "fieldname": "total_amount",
            "label": _("Total Amount"),
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "status",
            "label": _("Status"),
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "fieldname": "journal_entry",
            "label": _("Journal Entry"),
            "fieldtype": "Link",
            "options": "Journal Entry",
            "width": 140,
        },
    ]


def get_data(filters):
    flt_filters = {}
    if filters.get("company"):
        flt_filters["company"] = filters["company"]
    if filters.get("from_date"):
        flt_filters["accrual_date"] = [">=", filters["from_date"]]
    if filters.get("to_date"):
        if "accrual_date" in flt_filters and isinstance(
            flt_filters["accrual_date"], list
        ):
            flt_filters["accrual_date"] = [
                "between",
                [filters["from_date"], filters["to_date"]],
            ]
        else:
            flt_filters["accrual_date"] = ["<=", filters["to_date"]]

    rows = frappe.get_list(
        "Commission Accrual Batch",
        filters=flt_filters,
        fields=[
            "name",
            "company",
            "period_from",
            "period_to",
            "accrual_date",
            "total_amount",
            "status",
            "journal_entry",
        ],
        order_by="accrual_date desc",
        limit_page_length=0,
    )
    return rows
