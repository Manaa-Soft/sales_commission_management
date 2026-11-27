# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
    filters = filters or {}
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {
            "fieldname": "description",
            "label": _("Description"),
            "fieldtype": "Data",
            "width": 260,
        },
        {
            "fieldname": "amount",
            "label": _("Amount"),
            "fieldtype": "Currency",
            "width": 140,
        },
    ]


def get_data(filters):
    company = filters.get("company")
    data = []

    # Subledger balance: sum of outstanding_amount on submitted commission entries
    flt_filters = {"docstatus": 1}
    if company:
        flt_filters["company"] = company
    rows = frappe.get_list(
        "Commission Entry",
        filters=flt_filters,
        fields=["outstanding_amount", "earned_commission", "paid_amount"],
        limit_page_length=0,
    )
    subledger_outstanding = sum(flt(r.outstanding_amount) for r in rows)
    subledger_earned = sum(flt(r.earned_commission) for r in rows)
    subledger_paid = sum(flt(r.paid_amount) for r in rows)

    data.append(
        {
            "description": _("Commission subledger — earned"),
            "amount": flt(subledger_earned, 2),
        }
    )
    data.append(
        {
            "description": _("Commission subledger — paid"),
            "amount": flt(subledger_paid, 2),
        }
    )
    data.append(
        {
            "description": _("Commission subledger — outstanding"),
            "amount": flt(subledger_outstanding, 2),
        }
    )

    # GL accrued liability: sum of outstanding on submitted accrual batches
    batch_filters = {"docstatus": 1}
    if company:
        batch_filters["company"] = company
    batches = frappe.get_list(
        "Commission Accrual Batch",
        filters=batch_filters,
        fields=["total_amount", "journal_entry"],
        limit_page_length=0,
    )
    gl_accrued = sum(flt(b.total_amount) for b in batches)
    data.append(
        {
            "description": _("GL accrued liability (batches)"),
            "amount": flt(gl_accrued, 2),
        }
    )
    data.append(
        {
            "description": _("Difference (subledger − GL)"),
            "amount": flt(subledger_outstanding - gl_accrued, 2),
        }
    )

    # Expense recognized via settlements
    sett_filters = {"docstatus": 1}
    if company:
        sett_filters["company"] = company
    settlements = frappe.get_list(
        "Commission Settlement",
        filters=sett_filters,
        fields=["total_payable"],
        limit_page_length=0,
    )
    settled = sum(flt(s.total_payable) for s in settlements)
    data.append(
        {
            "description": _("Settled commission (settlements)"),
            "amount": flt(settled, 2),
        }
    )

    return data
