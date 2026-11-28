# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from sales_commission_management.sales_commission_management.payout.recovery import (
    get_commission_recovery_position,
)
from sales_commission_management.sales_commission_management.utils import (
    currency_precision as _cp,
)


def execute(filters=None):
    filters = filters or {}
    return get_columns(), get_data(filters)


def get_columns():
    return [
        {"fieldname": "commission_entry", "label": _("Commission Entry"), "fieldtype": "Link",
         "options": "Commission Entry", "width": 160},
        {"fieldname": "company", "label": _("Company"), "fieldtype": "Link",
         "options": "Company", "width": 140},
        {"fieldname": "beneficiary", "label": _("Beneficiary"), "fieldtype": "Data", "width": 140},
        {"fieldname": "currency", "label": _("Currency"), "fieldtype": "Data", "width": 80},
        {"fieldname": "original_commission", "label": _("Original Commission"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "net_entitlement", "label": _("Net Entitlement"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "net_paid", "label": _("Net Paid"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "net_recovered", "label": _("Net Recovered"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "outstanding_recovery", "label": _("Outstanding Recovery"), "fieldtype": "Currency", "width": 140},
    ]


def get_data(filters):
    flt_filters = {"docstatus": 1}
    if filters.get("company"):
        flt_filters["company"] = filters["company"]

    # Only original (top-level) entries carry a recovery position.
    rows = frappe.get_list(
        "Commission Entry",
        filters=flt_filters,
        fields=[
            "name",
            "company",
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "employee",
            "supplier",
            "original_entry",
            "event_type",
        ],
        order_by="creation desc",
        limit_page_length=0,
    )

    data = []
    for r in rows:
        if r.original_entry:
            continue
        # Skip derived return/cancellation entries; only original entries carry
        # a recovery position.
        if r.event_type in ("Return Adjustment", "Cancellation Reversal"):
            continue
        pos = get_commission_recovery_position(r.name, for_update=False)
        outstanding = flt(pos.get("outstanding_recovery", 0))
        # Only show entries with a recovery due.
        if outstanding <= 0:
            continue
        beneficiary = r.sales_person or r.sales_partner or r.employee or r.supplier
        currency = pos.get("currency")
        precision = _cp(currency) if currency else 2
        data.append(
            {
                "commission_entry": r.name,
                "company": r.company,
                "beneficiary": beneficiary,
                "currency": currency,
                "original_commission": flt(pos.get("original_commission", 0), precision),
                "net_entitlement": flt(pos.get("net_entitlement", 0), precision),
                "net_paid": flt(pos.get("net_paid", 0), precision),
                "net_recovered": flt(pos.get("net_recovered", 0), precision),
                "outstanding_recovery": flt(outstanding, precision),
            }
        )
    return data