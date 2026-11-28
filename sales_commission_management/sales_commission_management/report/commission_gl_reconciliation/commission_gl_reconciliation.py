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
    """Accrued liability subledger in company currency as of to_date.

    Returns a dict mapping liability account -> amount (company currency),
    derived from submitted Commission Accrual Batch items and their linked
    Journal Entries, less reclassified and cancellation-reversal amounts.
    """
    if mode != "Periodic Accrual":
        return {}
    # Sum submitted Commission Accrual Batch Items (company_amount) per actual
    # liability account used on the item.
    by_account = {}
    batches = frappe.get_all(
        "Commission Accrual Batch",
        filters={"company": company, "docstatus": 1},
        fields=["name", "accrual_date", "journal_entry", "company"],
    )
    for b in batches or []:
        je_name = getattr(b, "journal_entry", None)
        if not je_name:
            frappe.throw(
                _("Submitted Commission Accrual Batch {0} has no linked Journal Entry.").format(
                    b.name
                )
            )
        try:
            je = frappe.get_doc("Journal Entry", je_name)
        except Exception:
            raise
        if getattr(je, "docstatus", None) != 1:
            frappe.throw(
                _("Journal Entry {0} linked to batch {1} is not submitted.").format(
                    je_name, b.name
                )
            )
        if getattr(je, "company", None) != company:
            frappe.throw(
                _("Journal Entry {0} does not belong to company {1}.").format(
                    je_name, company
                )
            )
        # Use the Journal Entry posting date for accounting cutoff.
        posting_date = getattr(je, "posting_date", None) or getattr(
            b, "accrual_date", None
        )
        if to_date and posting_date:
            try:
                if getdate(posting_date) > getdate(to_date):
                    continue
            except Exception:
                pass
        items = frappe.get_all(
            "Commission Accrual Batch Item",
            filters={"parent": b.name},
            fields=["company_amount", "accrued_liability_account"],
        )
        for it in items or []:
            acct = getattr(it, "accrued_liability_account", None)
            if not acct:
                # Fall back to batch-level account resolution is not reliable;
                # require row-level account.
                frappe.throw(
                    _("Accrual batch item in {0} has no accrued liability account.").format(
                        b.name
                    )
                )
            by_account[acct] = by_account.get(acct, 0.0) + flt(
                it.get("company_amount")
            )

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
            je_posting = frappe.get_cached_value("Journal Entry", je, "posting_date")
        except Exception:
            raise
        if je_status != 1:
            continue
        cutoff_date = je_posting or getattr(s, "approval_date", None)
        if to_date and cutoff_date:
            try:
                if getdate(cutoff_date) > getdate(to_date):
                    continue
            except Exception:
                pass
        items = frappe.get_all(
            "Commission Settlement Item",
            filters={"parent": s.name},
            fields=["base_currency_amount", "reclassified_company_amount"],
        )
        for it in items or []:
            rca = it.get("reclassified_company_amount")
            if rca is None:
                rca = it.get("base_currency_amount")
            if rca is not None and rca != "":
                reclassified_total += flt(rca)
    # Resolve configured accrued account.
    try:
        _settings_obj = frappe.get_single("Commission Settings") if frappe.db.exists(
            "DocType", "Commission Settings"
        ) else None
    except Exception:
        _settings_obj = None
    _accrued_cfg, _ = _resolve_accounts(_settings_obj, company)
    target_acct = _accrued_cfg if _accrued_cfg in by_account else (
        next(iter(by_account), None)
    )
    if target_acct is not None and reclassified_total:
        by_account[target_acct] = by_account.get(target_acct, 0.0) - reclassified_total

    # Account for cancellation / accrual-reversal. Use persisted company amount.
    reversal_total_by_acct = {}
    rev_entries = frappe.get_all(
        "Commission Entry",
        filters={
            "company": company,
            "docstatus": 1,
            "event_type": "Cancellation Reversal",
        },
        fields=[
            "accrued_reversal_company_amount",
            "accrued_amount",
            "commission_to_company_rate",
            "commission_currency",
            "company_currency",
            "posting_date",
        ],
    )
    for e in rev_entries or []:
        pd = getattr(e, "posting_date", None)
        if to_date and pd and getdate(pd) > getdate(to_date):
            continue
        # Prefer persisted company-currency reversal amount.
        amt = getattr(e, "accrued_reversal_company_amount", None)
        if amt is None:
            # Fallback for historical rows: old calculation.
            amt = flt(e.get("accrued_amount"))
            rate = flt(e.get("commission_to_company_rate") or 0)
            comm = e.get("commission_currency")
            comp = e.get("company_currency") or _company_currency(company)
            if comm and comp and comm == comp:
                rate = 1.0 if not rate else rate
            if not rate or rate <= 0:
                if comm and comp and comm != comp:
                    frappe.throw(
                        _("Missing commission-to-company rate for cancellation reversal entry.")
                    )
                rate = 1.0
            amt = flt(amt * rate)
        else:
            amt = flt(amt)
        # amt is negative reversal; add to accrued liability.
        acct = target_acct if target_acct is not None else (
            next(iter(by_account), None)
        )
        if acct is None:
            # No batch accounts; nothing to attribute. Skip.
            continue
        by_account[acct] = by_account.get(acct, 0.0) + amt

    return by_account


def _payable_subledger(company, to_date, mode):
    """Commission payable subledger in company currency as of to_date."""
    total = 0.0
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
            je_posting = frappe.get_cached_value("Journal Entry", je, "posting_date")
        except Exception:
            raise
        if je_status != 1:
            continue
        cutoff_date = je_posting or getattr(s, "approval_date", None)
        if to_date and cutoff_date:
            try:
                if getdate(cutoff_date) > getdate(to_date):
                    continue
            except Exception:
                pass
        items = frappe.get_all(
            "Commission Settlement Item",
            filters={"parent": s.name},
            fields=["base_currency_amount", "reclassified_company_amount"],
        )
        for it in items or []:
            if mode == "Periodic Accrual":
                rca = it.get("reclassified_company_amount")
                if rca is None:
                    rca = it.get("base_currency_amount")
                if rca is not None and rca != "":
                    total += flt(rca)
            else:
                # Subledger Until Settlement: approved settlement amount route.
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
    return flt(total)


def get_data(filters):
    company = filters.get("company")
    if not company:
        frappe.throw(_("Company is required for GL Reconciliation."))
    to_date = filters.get("to_date") or nowdate()
    if not to_date:
        to_date = nowdate()

    data = []

    settings = _settings()
    accrued_account, payable_account = _resolve_accounts(settings, company)
    mode = _accounting_mode(settings)

    accrued_by_acct = _batch_accrued_subledger(company, to_date, mode)
    payable_subledger = _payable_subledger(company, to_date, mode)

    accrued_accounts = set()
    payable_accounts = set()
    if accrued_account:
        accrued_accounts.add(accrued_account)
    if payable_account:
        payable_accounts.add(payable_account)
    accrued_accounts |= set(accrued_by_acct.keys())

    # Accrued liability accounts.
    for acct in sorted(accrued_accounts):
        gl = _gl_balance(acct, company, to_date)
        sub = flt(accrued_by_acct.get(acct, 0.0))
        data.append(
            {
                "account": acct,
                "description": _("Accrued commission liability"),
                "gl_balance": flt(gl, 2),
                "subledger_amount": flt(sub, 2),
                "difference": flt(sub - gl, 2),
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