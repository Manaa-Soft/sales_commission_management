# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from sales_commission_management.sales_commission_management.doctype.commission_entry.commission_entry import (
    _expand_sales_person,
)


def execute(filters=None):
    filters = filters or {}
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {
            "fieldname": "beneficiary_type",
            "label": _("Type"),
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
            "fieldname": "payable_eligible",
            "label": _("Payable Eligible"),
            "fieldtype": "Currency",
            "width": 120,
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
        {
            "fieldname": "bucket_0_30",
            "label": _("0-30 Days"),
            "fieldtype": "Currency",
            "width": 100,
        },
        {
            "fieldname": "bucket_31_60",
            "label": _("31-60 Days"),
            "fieldtype": "Currency",
            "width": 100,
        },
        {
            "fieldname": "bucket_61_90",
            "label": _("61-90 Days"),
            "fieldtype": "Currency",
            "width": 100,
        },
        {
            "fieldname": "bucket_90_plus",
            "label": _("90+ Days"),
            "fieldtype": "Currency",
            "width": 100,
        },
    ]


def get_data(filters):
    flt_filters = {
        "docstatus": 1,
        "status": ["in", ["Accrued", "Payable", "Partially Paid"]],
    }
    if filters.get("company"):
        flt_filters["company"] = filters["company"]
    if filters.get("sales_person"):
        expanded = _expand_sales_person(filters["sales_person"])
        if len(expanded) == 1:
            flt_filters["sales_person"] = expanded[0]
        else:
            flt_filters["sales_person"] = ["in", expanded]
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
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "earned_commission",
            "payable_amount",
            "paid_amount",
            "outstanding_amount",
            "posting_date",
        ],
        limit_page_length=0,
    )

    today = getdate(nowdate())
    agg = {}
    for r in rows:
        bt = r.beneficiary_type
        ben = r.sales_person if bt == "Sales Person" else r.sales_partner
        if not ben:
            ben = "(group)"
        key = (bt, ben)
        a = agg.setdefault(
            key,
            {
                "earned": 0.0,
                "payable": 0.0,
                "paid": 0.0,
                "outstanding": 0.0,
                "b0": 0.0,
                "b1": 0.0,
                "b2": 0.0,
                "b3": 0.0,
            },
        )
        a["earned"] += flt(r.earned_commission)
        a["payable"] += flt(r.payable_amount) or flt(r.earned_commission)
        a["paid"] += flt(r.paid_amount)
        a["outstanding"] += flt(r.outstanding_amount)
        age = (today - getdate(r.posting_date)).days if r.posting_date else 0
        if age <= 30:
            a["b0"] += flt(r.outstanding_amount)
        elif age <= 60:
            a["b1"] += flt(r.outstanding_amount)
        elif age <= 90:
            a["b2"] += flt(r.outstanding_amount)
        else:
            a["b3"] += flt(r.outstanding_amount)

    data = []
    for (bt, ben), a in agg.items():
        data.append(
            {
                "beneficiary_type": bt,
                "beneficiary": ben,
                "earned": flt(a["earned"], 2),
                "payable_eligible": flt(a["payable"], 2),
                "paid": flt(a["paid"], 2),
                "outstanding": flt(a["outstanding"], 2),
                "bucket_0_30": flt(a["b0"], 2),
                "bucket_31_60": flt(a["b1"], 2),
                "bucket_61_90": flt(a["b2"], 2),
                "bucket_90_plus": flt(a["b3"], 2),
            }
        )
    data.sort(key=lambda d: d["outstanding"], reverse=True)
    return data
