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
        {"fieldname": "item_group", "label": _("Item Group"), "fieldtype": "Link", "options": "Item Group", "width": 160},
        {"fieldname": "sales", "label": _("Sales"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "gp", "label": _("Gross Profit"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "commission", "label": _("Commission"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "roi", "label": _("ROI (GP / Commission)"), "fieldtype": "Float", "width": 130},
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
    if filters.get("item_group"):
        flt_filters["item_group"] = filters["item_group"]

    rows = frappe.get_list(
        "Commission Entry",
        filters=flt_filters,
        fields=["item_group", "company_basis_amount", "earned_commission", "commission_basis"],
        limit_page_length=0,
    )

    agg = {}
    for r in rows:
        key = r.item_group or _("(none)")
        a = agg.setdefault(key, {"item_group": r.item_group, "sales": 0.0,
                                 "gp": 0.0, "commission": 0.0})
        a["commission"] += flt(r.earned_commission)
        if r.commission_basis == "Gross Profit":
            a["gp"] += flt(r.company_basis_amount)
        elif r.commission_basis == "Net Total":
            a["sales"] += flt(r.company_basis_amount)

    data = []
    for a in agg.values():
        roi = (a["gp"] / a["commission"]) if a["commission"] else 0.0
        data.append({
            "item_group": a["item_group"],
            "sales": flt(a["sales"], 2),
            "gp": flt(a["gp"], 2),
            "commission": flt(a["commission"], 2),
            "roi": flt(roi, 2),
        })
    data.sort(key=lambda d: d["roi"], reverse=True)
    return data