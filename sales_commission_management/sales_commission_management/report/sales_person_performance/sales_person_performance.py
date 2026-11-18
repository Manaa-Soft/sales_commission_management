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
            "fieldname": "sales_person",
            "label": _("Sales Person"),
            "fieldtype": "Link",
            "options": "Sales Person",
            "width": 160,
        },
        {
            "fieldname": "total_sales",
            "label": _("Total Sales"),
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "total_gp",
            "label": _("Gross Profit"),
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "total_commission",
            "label": _("Commission"),
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "commission_pct_gp",
            "label": _("Commission % of GP"),
            "fieldtype": "Percent",
            "width": 130,
        },
        {
            "fieldname": "target",
            "label": _("Target"),
            "fieldtype": "Currency",
            "width": 110,
        },
        {
            "fieldname": "achievement_pct",
            "label": _("Achievement %"),
            "fieldtype": "Percent",
            "width": 110,
        },
    ]


def get_data(filters):
    flt_filters = {"beneficiary_type": "Sales Person", "docstatus": 1}
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
            "sales_person",
            "company_basis_amount",
            "commission_basis",
            "earned_commission",
            "earned_commission",
        ],
        limit_page_length=0,
    )

    # Aggregate commission per sales person
    agg = {}
    for r in rows:
        sp = r.sales_person
        if not sp:
            continue
        a = agg.setdefault(
            sp,
            {
                "sales_person": sp,
                "total_sales": 0.0,
                "total_gp": 0.0,
                "total_commission": 0.0,
            },
        )
        a["total_commission"] += flt(r.earned_commission)
        if r.commission_basis == "Net Total":
            a["total_sales"] += flt(r.company_basis_amount)
        elif r.commission_basis == "Gross Profit":
            a["total_gp"] += flt(r.company_basis_amount)
            a["total_sales"] += flt(r.company_basis_amount)

    # Look up targets
    for sp, a in agg.items():
        try:
            targets = frappe.get_list(
                "Commission Target",
                filters={
                    "beneficiary_type": "Sales Person",
                    "sales_person": sp,
                    "docstatus": ["!=", 2],
                },
                fields=["target_value", "target_metric"],
                limit_page_length=1,
            )
            if targets:
                a["target"] = flt(targets[0].target_value)
                a["achievement_pct"] = (
                    (a["total_sales"] / a["target"] * 100.0) if a["target"] else 0.0
                )
            else:
                a["target"] = 0.0
                a["achievement_pct"] = 0.0
        except Exception:
            a["target"] = 0.0
            a["achievement_pct"] = 0.0

    data = []
    for a in agg.values():
        pct = (a["total_commission"] / a["total_gp"] * 100.0) if a["total_gp"] else 0.0
        data.append(
            {
                "sales_person": a["sales_person"],
                "total_sales": flt(a["total_sales"], 2),
                "total_gp": flt(a["total_gp"], 2),
                "total_commission": flt(a["total_commission"], 2),
                "commission_pct_gp": flt(pct, 2),
                "target": flt(a.get("target", 0), 2),
                "achievement_pct": flt(a.get("achievement_pct", 0), 2),
            }
        )
    data.sort(key=lambda d: d["total_commission"], reverse=True)
    return data
