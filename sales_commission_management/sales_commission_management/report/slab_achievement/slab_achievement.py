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
        {"fieldname": "slab_from", "label": _("From"), "fieldtype": "Currency", "width": 100},
        {"fieldname": "slab_to", "label": _("To"), "fieldtype": "Currency", "width": 100},
        {"fieldname": "rate", "label": _("Rate %"), "fieldtype": "Percent", "width": 80},
        {"fieldname": "fixed_bonus", "label": _("Fixed Bonus"), "fieldtype": "Currency", "width": 100},
        {"fieldname": "attainment", "label": _("Attainment"), "fieldtype": "Currency", "width": 110},
        {"fieldname": "effective_rate", "label": _("Effective Rate"), "fieldtype": "Percent", "width": 110},
    ]


def get_data(filters):
    # Placeholder: returns slab definitions per plan; attainment requires recompute_period_slabs
    plans = frappe.get_list(
        "Commission Plan",
        filters={"enabled": 1},
        fields=["name", "slabs"],
        limit_page_length=0,
    )
    data = []
    for plan in plans:
        try:
            doc = frappe.get_doc("Commission Plan", plan.name)
        except Exception:
            continue
        for slab in (doc.get("slabs") or []):
            data.append({
                "plan": plan.name,
                "slab_from": flt(getattr(slab, "min_attainment", None) or getattr(slab, "from_amount", 0), 2),
                "slab_to": flt(getattr(slab, "max_attainment", None) or getattr(slab, "to_amount", 0), 2),
                "rate": flt(getattr(slab, "rate_percentage", 0), 2),
                "fixed_bonus": flt(getattr(slab, "fixed_bonus", None) or getattr(slab, "fixed_amount", 0), 2),
                "attainment": 0.0,
                "effective_rate": 0.0,
            })
    return data