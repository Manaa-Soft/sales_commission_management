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
                frappe.throw(
                    _("Invalid accounting date on accrual batch {0}: {1}.").format(
                        b.name, posting_date
                    )
                )
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

    # Subtract reclassified amounts using the actual submitted settlement
    # Journal Entries. The JE lines are the accounting truth: they tell us
    # which accrued liability account was debited and by how much. This avoids
    # aggregating into one arbitrary account and avoids the historical
    # base_currency_amount fallback (which could overstate reclassification).
    settlements = frappe.get_all(
        "Commission Settlement",
        filters={"company": company, "docstatus": 1},
        fields=["name", "approval_date", "settlement_journal_entry"],
    )
    for s in settlements or []:
        je = getattr(s, "settlement_journal_entry", None)
        if mode == "Periodic Accrual" and not je:
            continue
        try:
            je_status = frappe.get_cached_value("Journal Entry", je, "docstatus")
            je_posting = frappe.get_cached_value("Journal Entry", je, "posting_date")
            je_company = frappe.get_cached_value("Journal Entry", je, "company")
        except Exception:
            raise
        if je_status != 1:
            continue
        if je_company and je_company != company:
            frappe.throw(
                _("Journal Entry {0} does not belong to company {1}.").format(je, company)
            )
        cutoff_date = je_posting or getattr(s, "approval_date", None)
        if to_date and cutoff_date:
            try:
                if getdate(cutoff_date) > getdate(to_date):
                    continue
            except Exception:
                frappe.throw(
                    _("Invalid accounting date on settlement {0}: {1}.").format(
                        s.name, cutoff_date
                    )
                )
        # Read the JE account lines; debits to accrued accounts are the
        # reclassified amounts.
        je_lines = frappe.get_all(
            "Journal Entry Account",
            filters={"parent": je},
            fields=["account", "debit_in_account_currency", "credit_in_account_currency"],
        )
        for line in je_lines or []:
            acct = line.get("account")
            debit = flt(line.get("debit_in_account_currency"))
            if acct and debit and debit > 0:
                # A reclassification JE debits the accrued liability account.
                by_account[acct] = by_account.get(acct, 0.0) - debit

    # Account for cancellation / accrual-reversal. Use the linked reversal
    # Journal Entry as the accounting reference when available.
    seen_reversal_jes = set()
    rev_entries = frappe.get_all(
        "Commission Entry",
        filters={
            "company": company,
            "docstatus": 1,
            "event_type": "Cancellation Reversal",
        },
        fields=[
            "name",
            "accrued_reversal_company_amount",
            "accrued_amount",
            "commission_to_company_rate",
            "commission_currency",
            "company_currency",
            "posting_date",
            "reversal_journal_entry",
            "reverses_entry",
        ],
    )
    for e in rev_entries or []:
        rje = getattr(e, "reversal_journal_entry", None)
        # Determine posting date for cutoff.
        if rje:
            if rje in seen_reversal_jes:
                continue
            try:
                rje_status = frappe.get_cached_value("Journal Entry", rje, "docstatus")
                rje_posting = frappe.get_cached_value("Journal Entry", rje, "posting_date")
                rje_company = frappe.get_cached_value("Journal Entry", rje, "company")
            except Exception:
                raise
            if rje_status != 1:
                # Linked reversal JE cancelled/missing: do not count.
                continue
            if rje_company and rje_company != company:
                frappe.throw(
                    _("Reversal Journal Entry {0} does not belong to company {1}.").format(
                        rje, company
                    )
                )
            cutoff_pd = rje_posting
            if not cutoff_pd:
                frappe.throw(
                    _("Reversal Journal Entry {0} has no posting date.").format(rje)
                )
            if to_date:
                try:
                    if getdate(cutoff_pd) > getdate(to_date):
                        continue
                except Exception:
                    frappe.throw(
                        _("Invalid accounting date on reversal Journal Entry {0}.").format(rje)
                    )
            seen_reversal_jes.add(rje)
            # Allocate per account from the JE lines (debits to accrued accounts).
            rje_lines = frappe.get_all(
                "Journal Entry Account",
                filters={"parent": rje},
                fields=["account", "debit_in_account_currency"],
            )
            for line in rje_lines or []:
                acct = line.get("account")
                debit = flt(line.get("debit_in_account_currency"))
                if acct and debit and debit > 0:
                    by_account[acct] = by_account.get(acct, 0.0) - debit
            continue

        # Historical fallback: no reversal_journal_entry. Use persisted amount
        # allocated by the entry's originating accrual batch accounts. Do not
        # silently treat base_currency_amount as the reclassified amount.
        pd = getattr(e, "posting_date", None)
        if to_date and pd:
            try:
                if getdate(pd) > getdate(to_date):
                    continue
            except Exception:
                frappe.throw(
                    _("Invalid accounting date on cancellation reversal entry {0}.").format(
                        e.get("name")
                    )
                )
        amt = getattr(e, "accrued_reversal_company_amount", None)
        if amt is None:
            # Historical rows without persisted amount: reconstruct using the
            # old calculation, but flag is not possible here; allocate by batch
            # accounts. If no reliable amount, skip to avoid overstating.
            amt = getattr(e, "accrued_amount", None)
            if amt is None:
                continue
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
            amt = flt(flt(amt) * rate)
        else:
            amt = flt(amt)
        # amt is the persisted (negative) reversal amount; magnitude for allocation.
        magnitude = abs(amt)
        if magnitude <= 0.005:
            continue
        # Determine originating accounts from accrual batches.
        try:
            from sales_commission_management.sales_commission_management.doctype.commission_accrual_batch.commission_accrual_batch import (
                _accrued_for_entry_by_account as _by_acct,
            )

            # The reversal entry reverses an original entry; find the original
            # via reverses_entry if present, else use this entry's batches.
            orig_name = getattr(e, "reverses_entry", None) or e.get("name")
            by_acct = _by_acct(orig_name)
        except Exception:
            raise
        total_accrued = sum(flt(v) for v in (by_acct or {}).values())
        if total_accrued <= 0.005:
            frappe.throw(
                _(
                    "Cannot establish accrued liability account allocation for "
                    "cancellation reversal entry {0}."
                ).format(e.get("name"))
            )
        # amt sign: original persisted value is negative. Preserve sign per account.
        sign = -1.0 if amt < 0 else 1.0
        for acct, aamt in (by_acct or {}).items():
            share = flt(aamt) / total_accrued if total_accrued else 0.0
            allocated = flt(magnitude * share * sign, 6)
            if allocated:
                by_account[acct] = by_account.get(acct, 0.0) + allocated

    return by_account


def _payable_subledger(company, to_date, mode):
    """Commission payable subledger by account in company currency as of to_date.

    Returns a dict mapping payable account -> amount. Historical settlement
    accounts are derived from the actual submitted settlement Journal Entries
    rather than assuming the current configured payable account.
    """
    by_account = {}
    settlements = frappe.get_all(
        "Commission Settlement",
        filters={"company": company, "docstatus": 1},
        fields=["name", "approval_date", "settlement_journal_entry", "settlement_method", "settlement_document_type", "settlement_document"],
    )
    for s in settlements or []:
        je = getattr(s, "settlement_journal_entry", None)
        if mode == "Periodic Accrual":
            if getattr(s, "settlement_document_type", None) == "Additional Salary" or getattr(
                s, "settlement_method", None
            ) == "Additional Salary":
                continue
            if not je:
                continue
            try:
                je_status = frappe.get_cached_value("Journal Entry", je, "docstatus")
                je_posting = frappe.get_cached_value("Journal Entry", je, "posting_date")
                je_company = frappe.get_cached_value("Journal Entry", je, "company")
            except Exception:
                raise
            if je_status != 1:
                continue
            if je_company and je_company != company:
                frappe.throw(
                    _("Journal Entry {0} does not belong to company {1}.").format(je, company)
                )
            cutoff_date = je_posting or getattr(s, "approval_date", None)
            if to_date and cutoff_date:
                try:
                    if getdate(cutoff_date) > getdate(to_date):
                        continue
                except Exception:
                    frappe.throw(
                        _("Invalid accounting date on settlement {0}: {1}.").format(
                            s.name, cutoff_date
                        )
                    )
            # Use the actual reclassified amount per settlement JE credit lines.
            je_lines = frappe.get_all(
                "Journal Entry Account",
                filters={"parent": je},
                fields=["account", "debit_in_account_currency", "credit_in_account_currency"],
            )
            for line in je_lines or []:
                acct = line.get("account")
                credit = flt(line.get("credit_in_account_currency"))
                if acct and credit and credit > 0:
                    # A reclassification JE credits the payable account.
                    by_account[acct] = by_account.get(acct, 0.0) + credit
            # For Supplier Purchase Invoice settlements, the Purchase Invoice
            # subsequently debits the Commission Payable account (Periodic Accrual
            # mode). Subtract those debits so the subledger matches GL.
            sd_type = getattr(s, "settlement_document_type", None)
            sd_name = getattr(s, "settlement_document", None)
            if sd_type == "Purchase Invoice" and sd_name:
                try:
                    pi_status = frappe.get_cached_value(
                        "Purchase Invoice", sd_name, "docstatus"
                    )
                except Exception:
                    pi_status = None
                if pi_status == 1:
                    pi_lines = frappe.get_all(
                        "GL Entry",
                        filters={
                            "voucher_type": "Purchase Invoice",
                            "voucher_no": sd_name,
                            "company": company,
                            "is_cancelled": 0,
                        },
                        fields=["account", "debit", "credit"],
                    )
                    for pl in pi_lines or []:
                        acct = pl.get("account")
                        debit = flt(pl.get("debit"))
                        if acct and debit and debit > 0 and acct in by_account:
                            by_account[acct] = by_account.get(acct, 0.0) - debit
            continue

        # Subledger Until Settlement mode: approved settlement amount route.
        # For Supplier Purchase Invoice settlements, do not fabricate a
        # Commission Payable balance: the Purchase Invoice posts to the expense
        # and Accounts Payable accounts, not Commission Payable.
        if getattr(s, "settlement_document_type", None) == "Purchase Invoice" or getattr(
            s, "settlement_method", None
        ) == "Supplier Purchase Invoice":
            continue
        if getattr(s, "settlement_document_type", None) == "Additional Salary" or getattr(
            s, "settlement_method", None
        ) == "Additional Salary":
            continue
        if je:
            try:
                je_status = frappe.get_cached_value("Journal Entry", je, "docstatus")
                je_posting = frappe.get_cached_value("Journal Entry", je, "posting_date")
            except Exception:
                raise
            if je_status != 1:
                continue
            cutoff_date = je_posting or getattr(s, "approval_date", None)
        else:
            cutoff_date = getattr(s, "approval_date", None)
        if to_date and cutoff_date:
            try:
                if getdate(cutoff_date) > getdate(to_date):
                    continue
            except Exception:
                frappe.throw(
                    _("Invalid accounting date on settlement {0}: {1}.").format(
                        s.name, cutoff_date
                    )
                )
        items = frappe.get_all(
            "Commission Settlement Item",
            filters={"parent": s.name},
            fields=["base_currency_amount"],
        )
        total = 0.0
        for it in items or []:
            ba = it.get("base_currency_amount")
            if ba is not None and ba != "":
                total += flt(ba)
        by_account.setdefault("__subledger_until_settlement__", 0.0)
        by_account["__subledger_until_settlement__"] += flt(total)
    # Payout events reduce payable; payout reversals increase it.
    events = frappe.get_all(
        "Commission Payout Event",
        filters={"company": company, "docstatus": 1},
        fields=["event_type", "company_amount", "posting_date", "settlement"],
    )
    # Resolve configured payable account for payouts / subledger-until-settlement.
    settings = _settings()
    _accrued_cfg, payable_cfg = _resolve_accounts(settings, company)
    # Accumulate payout totals per actual payable account. Prefer the account
    # used on the linked settlement's settlement Journal Entry (the source
    # voucher's GL account) over the currently configured payable account, so
    # historical payouts follow the account that was actually posted.
    payout_totals_by_account = {}
    for ev in events or []:
        pd = getattr(ev, "posting_date", None)
        if to_date and pd:
            try:
                if getdate(pd) > getdate(to_date):
                    continue
            except Exception:
                frappe.throw(
                    _("Invalid accounting date on payout event.")
                )
        amt = flt(ev.get("company_amount"))
        et = ev.get("event_type")
        delta = 0.0
        if et == "Payout":
            delta = -amt
        elif et == "Payout Reversal":
            delta = amt
        else:
            continue
        acct = payable_cfg
        sett_name = getattr(ev, "settlement", None)
        if sett_name:
            try:
                sett_je = frappe.get_cached_value(
                    "Commission Settlement", sett_name, "settlement_journal_entry"
                )
            except Exception:
                sett_je = None
            if sett_je:
                try:
                    je_status = frappe.get_cached_value(
                        "Journal Entry", sett_je, "docstatus"
                    )
                    je_company = frappe.get_cached_value(
                        "Journal Entry", sett_je, "company"
                    )
                except Exception:
                    je_status = None
                    je_company = None
                if je_status == 1 and (not je_company or je_company == company):
                    je_lines = frappe.get_all(
                        "Journal Entry Account",
                        filters={"parent": sett_je},
                        fields=["account", "credit_in_account_currency"],
                    )
                    # Use the first credited payable account as the source voucher's
                    # payable account; fall back to configured account.
                    for line in je_lines or []:
                        credit = flt(line.get("credit_in_account_currency"))
                        if credit and credit > 0:
                            acct = line.get("account") or acct
                            break
        if not acct:
            acct = None
        payout_totals_by_account[acct] = (
            payout_totals_by_account.get(acct, 0.0) + flt(delta)
        )
    for acct, total in payout_totals_by_account.items():
        if acct is not None:
            by_account[acct] = by_account.get(acct, 0.0) + flt(total)
        else:
            by_account[None] = by_account.get(None, 0.0) + flt(total)
    # Move subledger-until-settlement accumulated total into configured payable account.
    sus_total = by_account.pop("__subledger_until_settlement__", 0.0)
    if sus_total and payable_cfg:
        by_account[payable_cfg] = by_account.get(payable_cfg, 0.0) + flt(sus_total)
    elif sus_total:
        # No configured payable account; leave as unmapped? Store under None.
        by_account[None] = by_account.get(None, 0.0) + flt(sus_total)
    return {k: flt(v) for k, v in by_account.items()}


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
    payable_by_acct = _payable_subledger(company, to_date, mode)

    accrued_accounts = set()
    payable_accounts = set()
    if accrued_account:
        accrued_accounts.add(accrued_account)
    if payable_account:
        payable_accounts.add(payable_account)
    accrued_accounts |= set(accrued_by_acct.keys())
    payable_accounts |= set((payable_by_acct or {}).keys())

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
        sub = flt((payable_by_acct or {}).get(acct, 0.0))
        data.append(
            {
                "account": acct,
                "description": _("Commission payable"),
                "gl_balance": flt(gl, 2),
                "subledger_amount": flt(sub, 2),
                "difference": flt(sub - gl, 2),
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