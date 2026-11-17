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
        {"fieldname": "plan", "label": _("Commission Plan"), "fieldtype": "Link", "options": "Commission Plan", "width": 160},
        {"fieldname": "invoices", "label": _("Invoices"), "fieldtype": "Int", "width": 80},
        {"fieldname": "earned", "label": _("Earned"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "paid", "label": _("Paid"), "fieldtype": "Currency", "width": 100},
        {"fieldname": "outstanding", "label": _("Outstanding"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "pct_of_gp", "label": _("Commission % of GP"), "fieldtype": "Percent", "width": 120},
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

    rows = frappe.get_list(
        "Commission Entry",
        filters=flt_filters,
        fields=["plan", "sales_invoice", "earned_commission", "paid_amount",
                "outstanding_amount", "company_basis_amount", "commission_basis"],
        limit_page_length=0,
    )

    agg = {}
    for r in rows:
        key = r.plan
        a = agg.setdefault(key, {"plan": key, "invoices": set(),
                                 "earned": 0.0, "paid": 0.0, "outstanding": 0.0,
                                 "basis": 0.0, "gp": 0.0})
        a["invoices"].add(r.sales_invoice)
        a["earned"] += flt(r.earned_commission)
        a["paid"] += flt(r.paid_amount)
        a["outstanding"] += flt(r.outstanding_amount)
        if r.commission_basis == "Gross Profit":
            a["basis"] += flt(r.company_basis_amount)

    data = []
    for a in agg.values():
        pct = (a["earned"] / a["basis"] * 100.0) if a["basis"] else 0.0
        data.append({
            "plan": a["plan"],
            "invoices": len(a["invoices"]),
            "earned": flt(a["earned"], 2),
            "paid": flt(a["paid"], 2),
            "outstanding": flt(a["outstanding"], 2),
            "pct_of_gp": flt(pct, 2),
        })
    data.sort(key=lambda d: d["earned"], reverse=True)
    return data