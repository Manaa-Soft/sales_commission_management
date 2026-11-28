# Copyright (c) 2026, ECOSIRE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

# Local imports are performed inside functions to avoid circular imports.


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
            "label": _("GL Balance (as of To Date)"),
            "fieldtype": "Currency",
            "width": 160,
        },
        {
            "fieldname": "subledger_amount",
            "label": _("Subledger Amount (as of To Date)"),
            "fieldtype": "Currency",
            "width": 180,
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
    # Do not silently swallow configuration/database errors.
    return frappe.get_single("Commission Settings")


def _resolve_accounts(settings, company):
    """Resolve accrued and payable accounts for a company.

    Applies company-specific mapping row first, then per-field defaults from
    Commission Settings. Never mixes accounts across companies.
    """
    accrued = None
    payable = None
    if settings:
        for row in (getattr(settings, "account_mappings", None) or []):
            if getattr(row, "company", None) == company and _cint_safe(
                getattr(row, "enabled", 1)
            ):
                accrued = getattr(row, "accrued_commission_payable_account", None) or accrued
                payable = getattr(row, "commission_payable_account", None) or payable
                break
        # Per-field defaults (not only when the whole mapping table is empty).
        if not accrued:
            accrued = getattr(settings, "default_accrued_liability_account", None)
        if not payable:
            payable = getattr(settings, "default_commission_payable_account", None)
    return accrued, payable


def _cint_safe(v):
    try:
        return frappe.utils.cint(v)
    except Exception:
        return 1 if v else 0


def _company_currency(company):
    return frappe.get_cached_value("Company", company, "default_currency") if company else None


def _gl_balance(account, company, to_date):
    """GL liability balance (credit - debit) as of to_date (inclusive)."""
    if not account or not company:
        return 0.0
    filters = {"account": account, "company": company, "is_cancelled": 0}
    if to_date:
        filters["posting_date"] = ["<=", to_date]
    rows = frappe.get_list(
        "GL Entry",
        filters=filters,
        fields=["debit", "credit"],
        limit_page_length=0,
    )
    return flt(sum(flt(r.credit) - flt(r.debit) for r in rows))


def _accounting_mode(settings):
    if not settings:
        return None
    mode = getattr(settings, "accounting_mode", None)
    if mode not in ("Subledger Until Settlement", "Periodic Accrual"):
        if mode is None:
            return None
        frappe.throw(_("Invalid Commission Accounting Mode: {0}").format(mode))
    return mode


def _batch_accrued_subledger(company, to_date, mode):
    """Accrued liability subledger in company currency as of to_date."""
    if mode != "Periodic Accrual":
        return 0.0
    # Sum submitted Commission Accrual Batch Items (company_amount) for company.
    total = 0.0
    batches = frappe.get_all(
        "Commission Accrual Batch",
        filters={"company": company, "docstatus": 1},
        fields=["name", "accrual_date"],
    )
    for b in batches or []:
        ad = getattr(b, "accrual_date", None)
        if to_date and ad and getdate(ad) > getdate(to_date):
            continue
        items = frappe.get_all(
            "Commission Accrual Batch Item",
            filters={"parent": b.name, "docstatus": 1},
            fields=["company_amount"],
        )
        for it in items or []:
            total += flt(it.get("company_amount"))
    # Subtract reclassified amounts through submitted settlements with submitted JE.
    reclassified_total = 0.0
    settlements = frappe.get_all(
        "Commission Settlement",
        filters={"company": company, "docstatus": 1},
        fields=["name", "approval_date", "settlement_journal_entry"],
    )
    for s in settlements or []:
        je = getattr(s, "settlement_journal_entry", None)
        if not je:
            continue
        try:
            je_status = frappe.get_cached_value("Journal Entry", je, "docstatus")
        except Exception:
            raise
        if je_status != 1:
            continue
        ad = getattr(s, "approval_date", None)
        if to_date and ad:
            try:
                if getdate(ad) > getdate(to_date):
                    continue
            except Exception:
                pass
        # Sum base_currency_amount (now reclassified company amount) of items.
        items = frappe.get_all(
            "Commission Settlement Item",
            filters={"parent": s.name},
            fields=["base_currency_amount"],
        )
        for it in items or []:
            ba = it.get("base_currency_amount")
            if ba is not None and ba != "":
                reclassified_total += flt(ba)
    total -= reclassified_total

    # Account for cancellation / accrual-reversal Journal Entries via
    # submitted Cancellation Reversal Commission Entries (accrued_amount negated).
    reversal_total = 0.0
    rev_entries = frappe.get_all(
        "Commission Entry",
        filters={
            "company": company,
            "docstatus": 1,
            "event_type": "Cancellation Reversal",
        },
        fields=["accrued_amount", "commission_to_company_rate", "commission_currency", "company_currency", "posting_date"],
    )
    for e in rev_entries or []:
        pd = getattr(e, "posting_date", None)
        if to_date and pd and getdate(pd) > getdate(to_date):
            continue
        amt = flt(e.get("accrued_amount"))
        # accrued_amount is negated for reversal; convert to company currency.
        rate = flt(e.get("commission_to_company_rate") or 0)
        comm = e.get("commission_currency")
        comp = e.get("company_currency") or _company_currency(company)
        if comm and comp and comm == comp:
            rate = 1.0 if not rate else rate
        if not rate or rate <= 0:
            # Cannot convert; skip rather than mix currencies silently? Raise.
            if comm and comp and comm != comp:
                frappe.throw(
                    _("Missing commission-to-company rate for cancellation reversal entry.")
                )
            rate = 1.0
        reversal_total += flt(amt * rate)
    total += reversal_total

    return flt(total)


def _payable_subledger(company, to_date):
    """Commission payable subledger in company currency as of to_date."""
    total = 0.0
    # Settlements: recognised payable (base_currency_amount) for submitted settlements.
    settlements = frappe.get_all(
        "Commission Settlement",
        filters={"company": company, "docstatus": 1},
        fields=["name", "approval_date", "settlement_journal_entry"],
    )
    for s in settlements or []:
        je = getattr(s, "settlement_journal_entry", None)
        if not je:
            continue
        try:
            je_status = frappe.get_cached_value("Journal Entry", je, "docstatus")
        except Exception:
            raise
        if je_status != 1:
            continue
        ad = getattr(s, "approval_date", None)
        if to_date and ad:
            try:
                if getdate(ad) > getdate(to_date):
                    continue
            except Exception:
                pass
        items = frappe.get_all(
            "Commission Settlement Item",
            filters={"parent": s.name},
            fields=["base_currency_amount"],
        )
        for it in items or []:
            ba = it.get("base_currency_amount")
            if ba is not None and ba != "":
                total += flt(ba)
    # Payout events reduce payable; payout reversals increase it.
    events = frappe.get_all(
        "Commission Payout Event",
        filters={"company": company, "docstatus": 1},
        fields=["event_type", "company_amount", "posting_date"],
    )
    for ev in events or []:
        pd = getattr(ev, "posting_date", None)
        if to_date and pd and getdate(pd) > getdate(to_date):
            continue
        amt = flt(ev.get("company_amount"))
        et = ev.get("event_type")
        if et == "Payout":
            total -= amt
        elif et == "Payout Reversal":
            total += amt
        # Recovery / Recovery Reversal: not part of payable liability per spec.
    return flt(total)


def get_data(filters):
    company = filters.get("company")
    if not company:
        frappe.throw(_("Company is required for GL Reconciliation."))
    from_date = filters.get("from_date")
    to_date = filters.get("to_date") or None
    if not to_date:
        to_date = None

    data = []

    settings = _settings()
    accrued_account, payable_account = _resolve_accounts(settings, company)
    mode = _accounting_mode(settings)

    accrued_subledger = _batch_accrued_subledger(company, to_date, mode)
    payable_subledger = _payable_subledger(company, to_date)

    accrued_accounts = set()
    payable_accounts = set()
    if accrued_account:
        accrued_accounts.add(accrued_account)
    if payable_account:
        payable_accounts.add(payable_account)

    # Accrued liability accounts.
    for acct in sorted(accrued_accounts):
        gl = _gl_balance(acct, company, to_date)
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
        gl = _gl_balance(acct, company, to_date)
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