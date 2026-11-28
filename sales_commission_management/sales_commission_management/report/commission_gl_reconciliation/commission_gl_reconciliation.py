# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate


def execute(filters=None):
    filters = filters or {}
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {
            "fieldname": "account",
            "label": _("Account"),
            "fieldtype": "Link",
            "options": "Account",
            "width": 220,
        },
        {
            "fieldname": "description",
            "label": _("Description"),
            "fieldtype": "Data",
            "width": 200,
        },
        {
            "fieldname": "gl_balance",
            "label": _("GL Balance"),
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "fieldname": "subledger_amount",
            "label": _("Subledger Amount"),
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "fieldname": "difference",
            "label": _("Difference"),
            "fieldtype": "Currency",
            "width": 140,
        },
    ]


def _settings():
    if not frappe.db.exists("DocType", "Commission Settings"):
        return None
    try:
        return frappe.get_single("Commission Settings")
    except Exception:
        return None


def _account_mappings(settings, company):
    mappings = []
    if settings:
        for row in (getattr(settings, "account_mappings", None) or []):
            if company and getattr(row, "company", None) != company:
                continue
            if not cint_safe(getattr(row, "enabled", 1)):
                continue
            mappings.append(row)
    if not mappings and settings:
        # Fall back to a single pseudo-mapping from defaults.
        mappings.append(settings)
    return mappings


def cint_safe(v):
    try:
        return frappe.utils.cint(v)
    except Exception:
        return 1 if v else 0


def _gl_balance(account, company, from_date, to_date):
    if not account:
        return 0.0
    filters = {"account": account, "company": company, "is_cancelled": 0}
    if from_date:
        filters["posting_date"] = [">=", from_date]
    if from_date and to_date:
        filters["posting_date"] = ["between", [from_date, to_date]]
    elif to_date:
        filters["posting_date"] = ["<=", to_date]
    rows = frappe.get_list(
        "GL Entry",
        filters=filters,
        fields=["debit", "credit"],
        limit_page_length=0,
    )
    # Liability balances are credit - debit.
    return sum(flt(r.credit) - flt(r.debit) for r in rows)


def _subledger_amounts(company, from_date, to_date, field):
    flt_filters = {"docstatus": 1}
    if company:
        flt_filters["company"] = company
    if from_date or to_date:
        # Commission Entry uses posting_date
        if from_date and to_date:
            flt_filters["posting_date"] = ["between", [from_date, to_date]]
        elif from_date:
            flt_filters["posting_date"] = [">=", from_date]
        elif to_date:
            flt_filters["posting_date"] = ["<=", to_date]
    # Exclude reversed entries from active subledger.
    flt_filters["status"] = ["!=", "Reversed"]
    rows = frappe.get_list(
        "Commission Entry",
        filters=flt_filters,
        fields=[field],
        limit_page_length=0,
    )
    return sum(flt(r.get(field)) for r in rows)


def get_data(filters):
    company = filters.get("company")
    from_date = filters.get("from_date")
    to_date = filters.get("to_date")
    data = []

    settings = _settings()
    mappings = _account_mappings(settings, company)

    # Collect distinct accounts to reconcile.
    accrued_accounts = set()
    payable_accounts = set()
    for m in mappings:
        accrued = getattr(m, "accrued_commission_payable_account", None)
        if not accrued and settings is not None and m is settings:
            accrued = getattr(settings, "default_accrued_liability_account", None)
        payable = getattr(m, "commission_payable_account", None)
        if not payable and settings is not None and m is settings:
            payable = getattr(settings, "default_commission_payable_account", None)
        if accrued:
            accrued_accounts.add(accrued)
        if payable:
            payable_accounts.add(payable)

    accrued_subledger = _subledger_amounts(company, from_date, to_date, "accrued_amount")
    payable_subledger = _subledger_amounts(company, from_date, to_date, "payable_amount")

    # Accrued liability accounts.
    for acct in sorted(accrued_accounts):
        gl = _gl_balance(acct, company, from_date, to_date)
        data.append(
            {
                "account": acct,
                "description": _("Accrued commission liability"),
                "gl_balance": flt(gl, 2),
                "subledger_amount": flt(accrued_subledger, 2),
                "difference": flt(accrued_subledger - gl, 2),
            }
        )

    # Payable liability accounts.
    for acct in sorted(payable_accounts):
        gl = _gl_balance(acct, company, from_date, to_date)
        data.append(
            {
                "account": acct,
                "description": _("Commission payable"),
                "gl_balance": flt(gl, 2),
                "subledger_amount": flt(payable_subledger, 2),
                "difference": flt(payable_subledger - gl, 2),
            }
        )

    if not data:
        data.append(
            {
                "account": None,
                "description": _("No commission accounts configured"),
                "gl_balance": 0.0,
                "subledger_amount": 0.0,
                "difference": 0.0,
            }
        )

    return data