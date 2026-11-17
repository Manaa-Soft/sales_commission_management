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
        {"fieldname": "cogs", "label": _("COGS"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "gp", "label": _("Gross Profit"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "gp_pct", "label": _("GP %"), "fieldtype": "Percent", "width": 90},
        {"fieldname": "commission", "label": _("Commission"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "commission_pct_gp", "label": _("Commission % of GP"), "fieldtype": "Percent", "width": 130},
    ]


def get_data(filters):
    flt_filters = {"docstatus": 1, "commission_basis": "Gross Profit"}
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
        fields=["item_group", "company_basis_amount", "earned_commission"],
        limit_page_length=0,
    )

    agg = {}
    for r in rows:
        key = r.item_group or _("(none)")
        a = agg.setdefault(key, {"item_group": r.item_group, "sales": 0.0,
                                 "cogs": 0.0, "gp": 0.0, "commission": 0.0})
        # company_basis_amount is GP for Gross Profit basis
        a["gp"] += flt(r.company_basis_amount)
        a["commission"] += flt(r.earned_commission)

    data = []
    for a in agg.values():
        gp_pct = (a["gp"] / a["sales"] * 100.0) if a["sales"] else 0.0
        comm_pct = (a["commission"] / a["gp"] * 100.0) if a["gp"] else 0.0
        data.append({
            "item_group": a["item_group"],
            "sales": flt(a["sales"], 2),
            "cogs": flt(a["cogs"], 2),
            "gp": flt(a["gp"], 2),
            "gp_pct": flt(gp_pct, 2),
            "commission": flt(a["commission"], 2),
            "commission_pct_gp": flt(comm_pct, 2),
        })
    data.sort(key=lambda d: d["gp"], reverse=True)
    return data