# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from sales_commission_management.sales_commission_management.payout.status import (
    compute_payout_status,
)
from sales_commission_management.sales_commission_management.utils import (
    currency_precision as _cp,
)


def execute(filters=None):
    filters = filters or {}
    return get_columns(), get_data(filters)


def get_columns():
    return [
        {"fieldname": "settlement", "label": _("Settlement"), "fieldtype": "Link",
         "options": "Commission Settlement", "width": 150},
        {"fieldname": "company", "label": _("Company"), "fieldtype": "Link",
         "options": "Company", "width": 140},
        {"fieldname": "beneficiary", "label": _("Beneficiary"), "fieldtype": "Data", "width": 140},
        {"fieldname": "currency", "label": _("Currency"), "fieldtype": "Data", "width": 80},
        {"fieldname": "payable", "label": _("Payable"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "actual_paid", "label": _("Actual Paid"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "outstanding", "label": _("Outstanding"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "recoverable", "label": _("Recoverable"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "payout_status", "label": _("Payout Status"), "fieldtype": "Data", "width": 140},
        {"fieldname": "status", "label": _("Settlement Status"), "fieldtype": "Data", "width": 120},
    ]


def get_data(filters):
    flt_filters = {}
    if filters.get("company"):
        flt_filters["company"] = filters["company"]
    if filters.get("payout_status"):
        flt_filters["payout_status"] = filters["payout_status"]
    if filters.get("status"):
        flt_filters["status"] = filters["status"]

    rows = frappe.get_list(
        "Commission Settlement",
        filters=flt_filters,
        fields=[
            "name",
            "company",
            "beneficiary_type",
            "sales_person",
            "sales_partner",
            "employee",
            "supplier",
            "commission_currency",
            "total_payable",
            "status",
            "payout_status",
        ],
        order_by="creation desc",
        limit_page_length=0,
    )

    data = []
    for r in rows:
        ps = compute_payout_status(r.name, for_update=False)
        beneficiary = (
            r.sales_person or r.sales_partner or r.employee or r.supplier or r.beneficiary_type
        )
        currency = ps.get("currency") or r.commission_currency
        precision = _cp(currency) if currency else 2
        data.append(
            {
                "settlement": r.name,
                "company": r.company,
                "beneficiary": beneficiary,
                "currency": currency,
                "payable": flt(ps.get("total_payable", r.total_payable), precision),
                "actual_paid": flt(ps.get("actual_paid_amount", 0), precision),
                "outstanding": flt(ps.get("outstanding_payout_amount", 0), precision),
                "recoverable": flt(ps.get("recoverable_amount", 0), precision),
                "payout_status": ps.get("payout_status") or r.payout_status or "Not Paid",
                "status": r.status,
            }
        )
    return data