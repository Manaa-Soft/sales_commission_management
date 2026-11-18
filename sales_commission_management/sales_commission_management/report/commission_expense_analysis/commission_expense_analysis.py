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
        {
            "fieldname": "company",
            "label": _("Company"),
            "fieldtype": "Link",
            "options": "Company",
            "width": 120,
        },
        {
            "fieldname": "cost_center",
            "label": _("Cost Center"),
            "fieldtype": "Link",
            "options": "Cost Center",
            "width": 120,
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
            "fieldname": "earned",
            "label": _("Earned"),
            "fieldtype": "Currency",
            "width": 110,
        },
        {
            "fieldname": "paid",
            "label": _("Paid"),
            "fieldtype": "Currency",
            "width": 100,
        },
        {
            "fieldname": "outstanding",
            "label": _("Outstanding"),
            "fieldtype": "Currency",
            "width": 110,
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

    rows = frappe.get_list(
        "Commission Entry",
        filters=flt_filters,
        fields=[
            "company",
            "cost_center",
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "earned_commission",
            "paid_amount",
            "outstanding_amount",
        ],
        limit_page_length=0,
    )

    agg = {}
    for r in rows:
        key = (
            r.company,
            r.cost_center,
            r.beneficiary_type,
            r.sales_person or r.sales_partner,
        )
        a = agg.setdefault(
            key,
            {
                "company": r.company,
                "cost_center": r.cost_center,
                "beneficiary_type": r.beneficiary_type,
                "beneficiary": r.sales_person or r.sales_partner,
                "earned": 0.0,
                "paid": 0.0,
                "outstanding": 0.0,
            },
        )
        a["earned"] += flt(r.earned_commission)
        a["paid"] += flt(r.paid_amount)
        a["outstanding"] += flt(r.outstanding_amount)

    data = []
    for a in agg.values():
        data.append(
            {
                "company": a["company"],
                "cost_center": a["cost_center"],
                "beneficiary_type": a["beneficiary_type"],
                "beneficiary": a["beneficiary"],
                "earned": flt(a["earned"], 2),
                "paid": flt(a["paid"], 2),
                "outstanding": flt(a["outstanding"], 2),
            }
        )
    data.sort(key=lambda d: d["earned"], reverse=True)
    return data
