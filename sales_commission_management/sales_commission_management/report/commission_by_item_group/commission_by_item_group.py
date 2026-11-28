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
            "fieldname": "item_group",
            "label": _("Item Group"),
            "fieldtype": "Link",
            "options": "Item Group",
            "width": 160,
        },
        {"fieldname": "qty", "label": _("Qty"), "fieldtype": "Float", "width": 80},
        {
            "fieldname": "commission_basis",
            "label": _("Basis"),
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "fieldname": "commission_rate",
            "label": _("Rate %"),
            "fieldtype": "Percent",
            "width": 80,
        },
        {
            "fieldname": "total_basis",
            "label": _("Total Basis"),
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "total_commission",
            "label": _("Total Commission"),
            "fieldtype": "Currency",
            "width": 130,
        },
        {
            "fieldname": "pct_of_total",
            "label": _("% of Total"),
            "fieldtype": "Percent",
            "width": 90,
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
    if filters.get("item_group"):
        flt_filters["item_group"] = filters["item_group"]

    rows = frappe.get_list(
        "Commission Entry",
        filters=flt_filters,
        fields=[
            "item_group",
            "source_name",
            "source_row",
            "event_type",
            "source_qty",
            "commission_basis",
            "commission_rate",
            "company_basis_amount",
            "earned_commission",
        ],
        limit_page_length=0,
    )

    agg = {}
    seen_sales = set()
    for r in rows:
        key = r.item_group or _("(none)")
        a = agg.setdefault(
            key,
            {
                "item_group": r.item_group,
                "qty": 0.0,
                "basis": r.commission_basis,
                "rate": flt(r.commission_rate),
                "total_basis": 0.0,
                "total_commission": 0.0,
            },
        )
        sales_key = (r.source_name, r.source_row, r.event_type)
        if sales_key not in seen_sales:
            seen_sales.add(sales_key)
            a["qty"] += flt(r.source_qty)
            a["total_basis"] += flt(r.company_basis_amount)
        a["total_commission"] += flt(r.earned_commission)

    grand = sum(a["total_commission"] for a in agg.values()) or 1.0
    data = []
    for a in agg.values():
        data.append(
            {
                "item_group": a["item_group"],
                "qty": flt(a["qty"], 2),
                "commission_basis": a["basis"],
                "commission_rate": a["rate"],
                "total_basis": flt(a["total_basis"], 2),
                "total_commission": flt(a["total_commission"], 2),
                "pct_of_total": flt(a["total_commission"] / grand * 100.0, 2),
            }
        )
    data.sort(key=lambda d: d["total_commission"], reverse=True)
    return data
