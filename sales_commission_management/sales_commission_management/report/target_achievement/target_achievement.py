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
            "fieldname": "target_metric",
            "label": _("Metric"),
            "fieldtype": "Data",
            "width": 120,
        },
        {
            "fieldname": "target_value",
            "label": _("Target"),
            "fieldtype": "Float",
            "width": 100,
        },
        {
            "fieldname": "attainment",
            "label": _("Attainment"),
            "fieldtype": "Float",
            "width": 100,
        },
        {
            "fieldname": "achievement_pct",
            "label": _("Achievement %"),
            "fieldtype": "Percent",
            "width": 110,
        },
        {
            "fieldname": "period_type",
            "label": _("Period"),
            "fieldtype": "Data",
            "width": 100,
        },
    ]


def get_data(filters):
    flt_filters = {"docstatus": ["!=", 2]}
    if filters.get("beneficiary_type"):
        flt_filters["beneficiary_type"] = filters["beneficiary_type"]

    rows = frappe.get_list(
        "Commission Target",
        filters=flt_filters,
        fields=[
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "target_metric",
            "target_value",
            "attainment_percentage",
            "period_type",
        ],
        limit_page_length=0,
    )

    data = []
    for r in rows:
        ben = (
            r.sales_person if r.beneficiary_type == "Sales Person" else r.sales_partner
        )
        target = flt(r.target_value)
        attainment = flt(
            r.attainment_percentage
        )  # placeholder until recompute_period_slabs is real
        pct = (attainment / target * 100.0) if target else 0.0
        data.append(
            {
                "beneficiary_type": r.beneficiary_type,
                "beneficiary": ben,
                "target_metric": r.target_metric,
                "target_value": flt(target, 2),
                "attainment": flt(attainment, 2),
                "achievement_pct": flt(pct, 2),
                "period_type": r.period_type,
            }
        )
    return data
