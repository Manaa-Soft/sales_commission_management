"""Manual (evidence-based) payout recording for Commission Settlements (P3.x).

This module implements the "Record Manual Payout" workflow for Commission
Settlements that use the ``Manual Approved Route`` settlement method.  It
validates the supplied payment evidence, allocates the paid amount across the
settlement's items, and generates ``Payout`` events through the trusted
event factory (``create_and_submit_payout_event``), then refreshes the
settlement's server-derived payout status.
"""

import math

import frappe
from frappe import _
from frappe.utils import flt, getdate

from sales_commission_management.sales_commission_management.utils import (
    currency_precision as _cp,
)

try:
    from sales_commission_management.sales_commission_management.payout.event_factory import (
        generate_event_key,
    )
except Exception:  # pragma: no cover
    generate_event_key = None

try:
    from sales_commission_management.sales_commission_management.payout.lifecycle import (
        create_and_submit_payout_event,
    )
except Exception:  # pragma: no cover
    create_and_submit_payout_event = None

try:
    from sales_commission_management.sales_commission_management.payout.supplier_payment import (
        _get_settlement_items,
        _previously_paid_for_item,
        allocate_supplier_payment_across_items,
        lock_commission_settlement,
    )
except Exception:  # pragma: no cover
    _get_settlement_items = None
    _previously_paid_for_item = None
    allocate_supplier_payment_across_items = None
    lock_commission_settlement = None

try:
    from sales_commission_management.sales_commission_management.payout.status import (
        compute_payout_status,
        refresh_settlement_payout_status,
    )
except Exception:  # pragma: no cover
    compute_payout_status = None
    refresh_settlement_payout_status = None


def _expected_party_type(party):
    """Resolve the actual ERPNext party type for a beneficiary name.

    Fail closed: only return a concrete type when the name resolves to a real
    Employee/Supplier/Customer record. Returns None for unresolved names (e.g.
    Sales Person display names) so callers can reject.
    """
    if not party:
        return None
    for doctype in ("Employee", "Supplier", "Customer"):
        try:
            if frappe.db.exists(doctype, party):
                return doctype
        except Exception:
            continue
    return None


def resolve_commission_accounting_beneficiary(sett):
    """Resolve the canonical ERPNext accounting party for a settlement.

    Returns a dict ``{"party_type": ..., "party": ...}`` and throws when the
    settlement's beneficiary cannot be mapped to a valid accounting party.
    Sales Person beneficiaries must link to an Employee; Sales Partner
    beneficiaries must have an explicitly configured Supplier/accounting party.
    """
    ben_type = _get_attr(sett, "beneficiary_type", None)
    employee = _get_attr(sett, "employee", None)
    sales_person = _get_attr(sett, "sales_person", None)
    sales_partner = _get_attr(sett, "sales_partner", None)
    supplier = _get_attr(sett, "supplier", None)
    beneficiary = _get_attr(sett, "beneficiary", None)

    if ben_type == "Sales Person" or sales_person:
        emp = employee
        if not emp and sales_person:
            try:
                emp = frappe.get_cached_value("Sales Person", sales_person, "employee")
            except Exception:
                emp = None
        if emp and frappe.db.exists("Employee", emp):
            return {"party_type": "Employee", "party": emp}
        frappe.throw(
            _("Sales Person beneficiary has no linked Employee accounting party.")
        )

    if ben_type == "Sales Partner" or sales_partner:
        # Explicitly configured accounting party (Supplier or other valid party).
        for cand in (supplier, beneficiary):
            if cand and frappe.db.exists("Supplier", cand):
                return {"party_type": "Supplier", "party": cand}
        # Allow a configured Employee as an explicit accounting party.
        for cand in (employee, beneficiary):
            if cand and frappe.db.exists("Employee", cand):
                return {"party_type": "Employee", "party": cand}
        frappe.throw(
            _("Sales Partner beneficiary has no configured accounting party.")
        )

    # Employee / Supplier beneficiaries.
    if employee:
        if frappe.db.exists("Employee", employee):
            return {"party_type": "Employee", "party": employee}
        frappe.throw(_("Commission settlement employee is not a valid Employee."))
    if supplier:
        if frappe.db.exists("Supplier", supplier):
            return {"party_type": "Supplier", "party": supplier}
        frappe.throw(_("Commission settlement supplier is not a valid Supplier."))
    if beneficiary and beneficiary != "(group)":
        for dt in ("Employee", "Supplier", "Customer"):
            try:
                if frappe.db.exists(dt, beneficiary):
                    return {"party_type": dt, "party": beneficiary}
            except Exception:
                continue

    frappe.throw(
        _("Commission settlement beneficiary could not be resolved to an accounting party.")
    )


def _company_currency(company):
    if not company:
        return None
    try:
        return frappe.get_cached_value("Company", company, "default_currency")
    except Exception:
        return None


def _get_attr(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _resolve_settlement(settlement):
    if settlement is None or settlement == "":
        frappe.throw(_("Commission Settlement is required."))
    if isinstance(settlement, dict):
        name = settlement.get("name") or settlement.get("settlement")
    elif hasattr(settlement, "name") and hasattr(settlement, "doctype"):
        name = settlement.name
    else:
        name = str(settlement)
    if not name:
        frappe.throw(_("Commission Settlement is required."))
    if not frappe.db.exists("Commission Settlement", name):
        frappe.throw(_("Commission Settlement {0} does not exist.").format(name))
    doc = frappe.get_doc("Commission Settlement", name)
    return name, doc


def _privileged():
    user = frappe.session.user
    if user in ("Administrator",):
        return True
    roles = set(frappe.get_roles(user))
    return bool({"System Manager", "Accounts Manager", "Commission Manager"} & roles)


def _first_kw(kwargs, *names):
    for n in names:
        v = kwargs.get(n)
        if v is not None:
            return v
    return None


def _is_finite(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    import math
    return math.isfinite(f)


def _validate_evidence(source_doctype, source_name, sett, amount, payment_date, commission_currency, company_amount=None, company_currency=None):
    """Validate payment evidence. Fails closed.

    Only explicitly supported payment document types are accepted:
    - Journal Entry
    - Payment Entry (Manual Approved Route path; no Supplier Purchase Invoice required)
    """
    if not source_doctype or not source_name:
        frappe.throw(_("Payment evidence (Evidence DocType and Evidence Document) is required."))
    if source_doctype == "Commission Settlement":
        frappe.throw(_("Commission Settlement cannot be used as payment evidence."))
    if source_doctype not in ("Journal Entry", "Payment Entry"):
        frappe.throw(
            _("Unsupported evidence document type {0}. Supported types: Journal Entry, Payment Entry.").format(
                source_doctype
            )
        )
    if not frappe.db.exists("DocType", source_doctype):
        frappe.throw(_("Evidence DocType {0} does not exist.").format(source_doctype))
    if not frappe.db.exists(source_doctype, source_name):
        frappe.throw(_("Evidence document {0} {1} does not exist.").format(source_doctype, source_name))
    try:
        ev = frappe.get_doc(source_doctype, source_name)
    except Exception:
        frappe.throw(_("Evidence document {0} {1} does not exist.").format(source_doctype, source_name))

    if getattr(ev, "docstatus", 0) != 1:
        frappe.throw(_("Evidence document {0} {1} must be submitted.").format(source_doctype, source_name))

    company = getattr(sett, "company", None)

    # Company check (fail closed).
    ev_company = getattr(ev, "company", None)
    if not ev_company:
        frappe.throw(_("Evidence document company could not be resolved."))
    if not company:
        frappe.throw(_("Settlement company could not be resolved."))
    if ev_company != company:
        frappe.throw(_("Evidence document company does not match settlement company."))

    # Date check.
    ev_date = None
    for f in ("posting_date", "payment_date", "date", "transaction_date"):
        v = getattr(ev, f, None)
        if v:
            ev_date = v
            break
    if ev_date:
        if str(getdate(ev_date)) != str(payment_date):
            frappe.throw(_("Evidence document date does not match payment date."))

    if source_doctype == "Journal Entry":
        _validate_journal_entry_evidence(ev, sett, amount, commission_currency, company, company_amount=company_amount, company_currency=company_currency)
    elif source_doctype == "Payment Entry":
        _validate_payment_entry_evidence(ev, sett, amount, commission_currency, company, company_amount=company_amount, company_currency=company_currency)


def _resolve_payable_account(company):
    from sales_commission_management.sales_commission_management.doctype.commission_settlement.commission_settlement import (
        _resolve_settlement_accounts,
    )

    # Fail closed: propagate resolution errors instead of swallowing them.
    res = _resolve_settlement_accounts(company)
    payable = None
    if isinstance(res, (tuple, list)):
        if len(res) >= 2:
            payable = res[1]
    elif isinstance(res, dict):
        payable = res.get("payable") or res.get("commission_payable_account")
    if not payable:
        frappe.throw(
            _("Commission payable account could not be resolved for company {0}.").format(company)
        )
    return payable


def _resolve_valid_parties(sett):
    """Resolve the set of valid party identifiers for a settlement's beneficiary.

    A Journal Entry / Payment Entry may reference the beneficiary through its
    linked Employee or Supplier mapping, not only through the raw Sales Person
    / Sales Partner name.
    """
    parties = set()
    ben = _get_attr(sett, "beneficiary", None)
    if ben and ben != "(group)":
        parties.add(ben)
    sp = _get_attr(sett, "sales_person", None)
    if sp:
        parties.add(sp)
        try:
            emp = frappe.get_cached_value("Sales Person", sp, "employee")
            if emp:
                parties.add(emp)
        except Exception:
            pass
    partner = _get_attr(sett, "sales_partner", None)
    if partner:
        parties.add(partner)
    emp = _get_attr(sett, "employee", None)
    if emp:
        parties.add(emp)
    supplier = _get_attr(sett, "supplier", None)
    if supplier:
        parties.add(supplier)
    return parties


def _validate_journal_entry_evidence(ev, sett, amount, commission_currency, company, company_amount=None, company_currency=None):
    """Validate a submitted Journal Entry as commission payment evidence."""
    if not company_currency:
        company_currency = _company_currency(company) or commission_currency
    # Resolve expected company amount (commission amount converted to company currency).
    if company_amount is None:
        if commission_currency == company_currency:
            company_amount = amount
        else:
            rate = None
            try:
                from frappe.utils import get_exchange_rate
                ev_date = getattr(ev, "posting_date", None) or frappe.utils.nowdate()
                r = get_exchange_rate(commission_currency, company_currency, ev_date)
                if r and _is_finite(r) and float(r) > 0:
                    rate = r
            except Exception:
                rate = None
            if not rate:
                frappe.throw(_("Cannot resolve commission-to-company exchange rate for Journal Entry validation."))
            company_amount = flt(amount) * flt(rate)
    company_amount = flt(company_amount, _cp(company_currency))

    # Debit/credit equality and amount (company currency).
    total_debit = flt(getattr(ev, "total_debit", 0) or 0)
    total_credit = flt(getattr(ev, "total_credit", 0) or 0)
    if total_debit <= 0 or total_credit <= 0:
        frappe.throw(_("Journal Entry must contain debit and credit lines."))
    if abs(total_debit - total_credit) > 0.005:
        frappe.throw(_("Journal Entry debits and credits do not balance."))
    prec = _cp(company_currency)
    tolerance = (10 ** (-prec)) if prec and prec > 0 else 0.005
    if abs(total_debit - company_amount) > tolerance and abs(total_credit - company_amount) > tolerance:
        frappe.throw(_("Journal Entry amount does not match payment amount."))

    # Currency: Journal Entry totals are in company currency.
    ev_currency = getattr(ev, "currency", None)
    if ev_currency and company_currency and ev_currency != company_currency:
        frappe.throw(_("Journal Entry currency does not match company currency."))

    # Company check (fail closed).
    ev_company = getattr(ev, "company", None)
    if not ev_company:
        frappe.throw(_("Journal Entry company could not be resolved."))
    if not company:
        frappe.throw(_("Settlement company could not be resolved."))
    if ev_company != company:
        frappe.throw(_("Journal Entry company does not match settlement company."))

    # Accounts.
    accounts = getattr(ev, "accounts", None) or []
    account_names = []
    for row in accounts or []:
        acct = getattr(row, "account", None) if not isinstance(row, dict) else row.get("account")
        if acct:
            account_names.append(acct)
    if not account_names:
        frappe.throw(_("Journal Entry has no account lines."))

    # Fail-closed payable account resolution.
    payable = _resolve_payable_account(company)
    if not payable:
        frappe.throw(_("Commission payable account does not exist."))
    if not frappe.db.exists("Account", payable):
        frappe.throw(_("Commission payable account does not exist."))
    _payable_company = frappe.get_cached_value("Account", payable, "company")
    if not _payable_company:
        frappe.throw(_("Commission payable account company could not be resolved."))
    if _payable_company != company:
        frappe.throw(_("Commission payable account does not belong to the company."))
    if payable not in account_names:
        frappe.throw(_("Journal Entry does not include the commission payable account."))

    # Cash/Bank accounts must use account_type Cash or Bank (not any Asset).
    has_cash_bank = False
    for acct in account_names:
        try:
            acct_type = frappe.get_cached_value("Account", acct, "account_type")
            acct_company = frappe.get_cached_value("Account", acct, "company")
        except Exception:
            acct_type = None
            acct_company = None
        if acct_type in ("Cash", "Bank") and (not company or acct_company == company):
            has_cash_bank = True
            break
    if not has_cash_bank:
        frappe.throw(_("Journal Entry must include a cash or bank account."))

    # Debit/credit direction validation.
    # Commission payable (liability) must be debited; cash/bank must be credited.
    payable_debit = 0.0
    payable_credit = 0.0
    cash_credit = 0.0
    cash_debit = 0.0
    for row in accounts or []:
        acct = getattr(row, "account", None) if not isinstance(row, dict) else row.get("account")
        debit = flt(getattr(row, "debit", 0) if not isinstance(row, dict) else row.get("debit", 0))
        credit = flt(getattr(row, "credit", 0) if not isinstance(row, dict) else row.get("credit", 0))
        if acct == payable:
            payable_debit += debit
            payable_credit += credit
        else:
            try:
                acct_type = frappe.get_cached_value("Account", acct, "account_type")
                acct_company = frappe.get_cached_value("Account", acct, "company")
            except Exception:
                acct_type = None
                acct_company = None
            if not acct:
                frappe.throw(_("Account is required."))
            if not frappe.db.exists("Account", acct):
                frappe.throw(_("Account does not exist."))
            if not acct_company:
                frappe.throw(_("Account company could not be resolved."))
            if acct_company != company:
                frappe.throw(_("Cash or bank account does not belong to the company."))
            if acct_type in ("Cash", "Bank"):
                cash_credit += credit
                cash_debit += debit
    if payable_credit > tolerance:
        frappe.throw(_("Commission payable account must be debited, not credited."))
    if cash_debit > tolerance:
        frappe.throw(_("Cash or bank account must be credited, not debited."))
    if abs(payable_debit - company_amount) > tolerance:
        frappe.throw(_("Commission payable debit does not match payment amount."))
    if abs(cash_credit - company_amount) > tolerance:
        frappe.throw(_("Cash or bank credit does not match payment amount."))

    # Beneficiary / party reference: must be present on the payable row.
    resolved = resolve_commission_accounting_beneficiary(sett)
    expected_party = resolved["party"]
    expected_party_type = resolved["party_type"]
    payable_party_found = False
    for row in accounts or []:
        acct = getattr(row, "account", None) if not isinstance(row, dict) else row.get("account")
        if acct != payable:
            continue
        party = getattr(row, "party", None) if not isinstance(row, dict) else row.get("party")
        party_type = getattr(row, "party_type", None) if not isinstance(row, dict) else row.get("party_type")
        if not party:
            frappe.throw(_("Commission payable account row must identify the beneficiary."))
        if party != expected_party:
            frappe.throw(_("Incorrect accounting beneficiary."))
        if not party_type:
            frappe.throw(_("Party type is required when a party is referenced."))
        if party_type != expected_party_type:
            frappe.throw(_("Incorrect accounting party type."))
        payable_party_found = True
    if not payable_party_found:
        frappe.throw(_("Commission payable account row must identify the beneficiary."))


def _validate_payment_entry_evidence(ev, sett, amount, commission_currency, company, company_amount=None, company_currency=None):
    """Validate a Payment Entry as evidence for the Manual Approved Route."""
    # Manual payout only creates Payout events, so the evidence must be a Pay entry.
    if getattr(ev, "payment_type", None) != "Pay":
        frappe.throw(_("Payment Entry must be a Pay type for manual payout evidence."))

    # Beneficiary-side amount: received_amount in paid_to (party) account currency.
    ev_amount = getattr(ev, "received_amount", None)
    if ev_amount is None:
        ev_amount = getattr(ev, "amount", None)
    if ev_amount is None:
        frappe.throw(_("Payment Entry amount is missing."))
    prec = _cp(commission_currency)
    tolerance = (10 ** (-prec)) if prec and prec > 0 else 0.005
    if abs(flt(ev_amount, prec) - flt(amount, prec)) > tolerance:
        frappe.throw(_("Payment Entry amount does not match payment amount."))

    # Beneficiary-side currency must match commission currency.
    ev_currency = getattr(ev, "paid_to_account_currency", None)
    if not ev_currency:
        frappe.throw(_("Payment Entry paid_to account currency is required."))
    if ev_currency != commission_currency:
        frappe.throw(_("Payment Entry currency does not match commission currency."))

    # Company check (fail closed).
    ev_company = getattr(ev, "company", None)
    if not ev_company:
        frappe.throw(_("Payment Entry company could not be resolved."))
    if not company:
        frappe.throw(_("Settlement company could not be resolved."))
    if ev_company != company:
        frappe.throw(_("Payment Entry company does not match settlement company."))

    # Canonical beneficiary resolution from the settlement.
    resolved = resolve_commission_accounting_beneficiary(sett)
    expected_party = resolved["party"]
    expected_party_type = resolved["party_type"]

    party = getattr(ev, "party", None)
    party_type = getattr(ev, "party_type", None)
    if not party:
        frappe.throw(_("Payment Entry must identify the beneficiary."))
    if party != expected_party:
        frappe.throw(_("Payment Entry party does not match settlement beneficiary."))
    if not party_type:
        frappe.throw(_("Party type is required when a party is referenced."))
    if party_type != expected_party_type:
        frappe.throw(_("Payment Entry party type does not match the referenced party."))

    # Account validation for Payment Entry evidence (fail closed).
    payable = _resolve_payable_account(company)
    if not payable:
        frappe.throw(_("Commission payable account does not exist."))
    _payable_company = frappe.get_cached_value("Account", payable, "company")
    if not _payable_company:
        frappe.throw(_("Commission payable account company could not be resolved."))
    if _payable_company != company:
        frappe.throw(_("Commission payable account does not belong to the company."))
    paid_to = getattr(ev, "paid_to", None)
    if not paid_to:
        frappe.throw(_("Payment Entry paid_to account is required."))
    if paid_to != payable:
        frappe.throw(_("Payment Entry paid_to must be the Commission Payable account."))
    paid_from = getattr(ev, "paid_from", None)
    if not paid_from:
        frappe.throw(_("Payment Entry paid_from account is required."))
    if not frappe.db.exists("Account", paid_from):
        frappe.throw(_("Account does not exist."))
    try:
        acct_type = frappe.get_cached_value("Account", paid_from, "account_type")
        acct_company = frappe.get_cached_value("Account", paid_from, "company")
    except Exception:
        acct_type = None
        acct_company = None
    if not acct_company:
        frappe.throw(_("Account company could not be resolved."))
    if acct_company != company:
        frappe.throw(_("Payment Entry paid_from account does not belong to the company."))
    if acct_type not in ("Cash", "Bank"):
        frappe.throw(_("Payment Entry paid_from must be a Cash or Bank account."))

    # Beneficiary-side account currency must match commission currency.
    paid_to_currency = getattr(ev, "paid_to_account_currency", None)
    if not paid_to_currency:
        frappe.throw(_("Payment Entry paid_to account currency is required."))
    if paid_to_currency != commission_currency:
        frappe.throw(_("Payment Entry paid_to account currency must match commission currency."))
    # Independently verify the underlying Account record's currency.
    paid_to = getattr(ev, "paid_to", None)
    if not paid_to:
        frappe.throw(_("Payment Entry paid_to account is required."))
    actual_paid_to_currency = None
    try:
        actual_paid_to_currency = frappe.get_cached_value("Account", paid_to, "account_currency")
    except Exception:
        actual_paid_to_currency = None
    if not actual_paid_to_currency:
        frappe.throw(_("Cannot resolve Commission Payable account currency."))
    if actual_paid_to_currency != paid_to_currency:
        frappe.throw(_("Payment Entry beneficiary account currency does not match its Account."))
    if actual_paid_to_currency != commission_currency:
        frappe.throw(_("Commission Payable account currency must match commission currency."))


def record_manual_payout(settlement=None, **kwargs):
    # Type validation at the whitelisted entry point.
    if isinstance(settlement, (list, dict)):
        frappe.throw(_("Settlement must be a string name, not {0}.").format(type(settlement).__name__))
    for scalar in ("amount", "paid_amount", "payment_amount", "total_amount", "amount_paid", "payout_amount"):
        v = kwargs.get(scalar)
        if v is not None and isinstance(v, (list, dict)):
            frappe.throw(_("{0} must be a scalar value.").format(scalar))
        if v is not None and isinstance(v, bool):
            frappe.throw(_("{0} must be a scalar value.").format(scalar))
    for scalar in ("source_name", "evidence_reference", "reference", "voucher", "evidence", "voucher_no"):
        v = kwargs.get(scalar)
        if v is not None and not isinstance(v, str):
            frappe.throw(_("{0} must be a string value.").format(scalar))
    """Record a manual (evidence-based) payout against a settlement."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Authentication required."), frappe.PermissionError)
    if not _privileged():
        frappe.throw(_("Insufficient permissions to record a manual payout."), frappe.PermissionError)

    if settlement is None:
        settlement = _first_kw(kwargs, "settlement", "settlement_name", "name", "commission_settlement")
    sett_name, sett = _resolve_settlement(settlement)

    if sett.docstatus != 1:
        frappe.throw(_("Commission Settlement {0} is not submitted.").format(sett_name))
    if getattr(sett, "settlement_method", None) != "Manual Approved Route":
        frappe.throw(
            _("Manual payout is only available for settlements using the Manual Approved Route method.")
        )

    payment_date = _first_kw(
        kwargs, "payment_date", "posting_date", "date", "payout_date", "paid_date"
    )
    if not payment_date:
        payment_date = frappe.utils.nowdate()
    payment_date = str(getdate(payment_date))

    amount = _first_kw(kwargs, "amount", "paid_amount", "amount_paid", "payment_amount", "payout_amount")
    if amount is None:
        frappe.throw(_("Payment amount is required."))
    amount = flt(amount)
    if amount <= 0:
        frappe.throw(_("Payment amount must be positive."))

    # Evidence source (mandatory).
    source_doctype = _first_kw(kwargs, "source_doctype", "evidence_doctype", "document_type", "doctype")
    source_name = _first_kw(
        kwargs, "source_name", "evidence_reference", "evidence", "reference", "voucher", "voucher_no"
    )
    evidence_attachment = _first_kw(kwargs, "evidence_attachment", "attachment")
    remarks = _first_kw(kwargs, "remarks", "note", "notes", "reason")

    # Require evidence. Attachments alone are not accepted as auditable evidence.
    if not source_doctype or not source_name:
        frappe.throw(_("Payment evidence (Evidence DocType and Evidence Document) is required."))

    # Lock the settlement (required).
    if lock_commission_settlement is None:
        frappe.throw(_("Settlement locking is unavailable."))
    lock_commission_settlement(sett_name)

    if generate_event_key is None or create_and_submit_payout_event is None:
        frappe.throw(_("Payout event factory is unavailable."))

    # Currency resolution.
    commission_currency = getattr(sett, "commission_currency", None) or _company_currency(sett.company)
    settlement_currency = getattr(sett, "settlement_currency", None) or commission_currency
    company_currency = _company_currency(sett.company) or commission_currency

    # Settlement FX rate = company currency per settlement currency (existing convention).
    settlement_fx_rate = getattr(sett, "settlement_fx_rate", None)
    if not settlement_fx_rate or not _is_finite(settlement_fx_rate) or float(settlement_fx_rate) <= 0:
        settlement_fx_rate = None
        if settlement_currency != company_currency:
            try:
                from frappe.utils import get_exchange_rate
                r = get_exchange_rate(settlement_currency, company_currency, payment_date)
                if r and _is_finite(r) and float(r) > 0:
                    settlement_fx_rate = r
            except Exception:
                pass
        if not settlement_fx_rate:
            if settlement_currency == company_currency:
                settlement_fx_rate = 1.0
            else:
                settlement_fx_rate = None
        if not settlement_fx_rate or not _is_finite(settlement_fx_rate) or float(settlement_fx_rate) <= 0:
            frappe.throw(_("Invalid or missing settlement FX rate."))
    settlement_fx_rate = flt(settlement_fx_rate)

    # Target (commission -> company) rate, resolved by evidence type first.
    if source_doctype == "Payment Entry":
        try:
            pe_doc = frappe.get_doc("Payment Entry", source_name)
        except Exception:
            frappe.throw(_("Payment Entry {0} does not exist.").format(source_name))
        if getattr(pe_doc, "docstatus", 0) != 1:
            frappe.throw(_("Payment Entry must be submitted."))
        if getattr(pe_doc, "payment_type", None) not in (None, "Pay"):
            frappe.throw(_("Payment Entry must be a Pay transaction."))
        if getattr(pe_doc, "company", None) and getattr(pe_doc, "company", None) != sett.company:
            frappe.throw(_("Payment Entry company does not match settlement."))
        if company_currency == commission_currency:
            target_exchange_rate = 1.0
        else:
            pe_rate = getattr(pe_doc, "target_exchange_rate", None)
            if not pe_rate or not _is_finite(pe_rate) or float(pe_rate) <= 0:
                frappe.throw(_("Invalid or missing Payment Entry target exchange rate."))
            target_exchange_rate = flt(pe_rate)
        # Verify beneficiary-side reconciliation: base_received = received * rate.
        pe_received = flt(getattr(pe_doc, "received_amount", 0) or 0)
        pe_base_received = flt(getattr(pe_doc, "base_received_amount", 0) or 0)
        if company_currency != commission_currency:
            if pe_base_received <= 0:
                frappe.throw(_("Payment Entry base received amount is required for foreign currency."))
            expected_base = flt(pe_received * float(target_exchange_rate), _cp(company_currency))
            prec = _cp(company_currency)
            tolerance = (10 ** (-prec)) if prec and prec > 0 else 0.005
            if abs(expected_base - pe_base_received) > tolerance:
                frappe.throw(_("Payment Entry beneficiary-side amount does not reconcile."))
        else:
            target_exchange_rate = 1.0
    else:
        if company_currency == commission_currency:
            target_exchange_rate = 1.0
        else:
            target_exchange_rate = None
            try:
                from frappe.utils import get_exchange_rate
                r = get_exchange_rate(commission_currency, company_currency, payment_date)
                if r and _is_finite(r) and float(r) > 0:
                    target_exchange_rate = r
            except Exception:
                pass
            if not target_exchange_rate or not _is_finite(target_exchange_rate) or float(target_exchange_rate) <= 0:
                frappe.throw(_("Invalid or missing company exchange rate."))
        target_exchange_rate = flt(target_exchange_rate)

    # Amounts: input amount is commission currency.
    commission_amount_total = flt(amount, _cp(commission_currency))
    if company_currency != commission_currency:
        company_amount_total = flt(commission_amount_total * target_exchange_rate, _cp(company_currency))
    else:
        company_amount_total = commission_amount_total

    # Validate evidence (company, currency, amount, date).
    _validate_evidence(source_doctype, source_name, sett, amount, payment_date, commission_currency, company_amount=company_amount_total, company_currency=company_currency)
    if settlement_currency == company_currency:
        settlement_amount_total = company_amount_total
    elif settlement_currency == commission_currency:
        settlement_amount_total = commission_amount_total
    else:
        settlement_amount_total = flt(company_amount_total / settlement_fx_rate, _cp(settlement_currency))

    # Build settlement items.
    sett_items = _get_settlement_items(sett) if _get_settlement_items else []
    if not sett_items:
        frappe.throw(_("Commission Settlement {0} has no items.").format(sett_name))

    items = []
    item_to_entry = {}
    for si in sett_items:
        si_name = _get_attr(si, "name", None)
        ce_name = _get_attr(si, "commission_ledger_entry", None)
        if not si_name or not ce_name:
            continue
        comm_balance = flt(_get_attr(si, "amount_included", 0))
        company_balance = flt(_get_attr(si, "base_currency_amount", 0))
        c2c_rate = flt(_get_attr(si, "commission_to_company_rate", 0))
        prev_paid = (
            _previously_paid_for_item(
                si_name,
                exclude_source_doctype=source_doctype,
                exclude_source_name=source_name,
            )
            if _previously_paid_for_item
            else 0.0
        )
        items.append(
            {
                "item": si_name,
                "settlement_amount": comm_balance,
                "company_amount": company_balance,
                "commission_to_company_rate": c2c_rate,
                "previously_paid": prev_paid,
            }
        )
        item_to_entry[si_name] = ce_name

    if not items:
        frappe.throw(_("Commission Settlement {0} has no eligible items.").format(sett_name))

    # Build the expected per-item allocation map up front so the idempotency
    # check can compare each existing event against its expected allocation
    # (aggregate totals alone cannot detect a changed per-item distribution).
    _expected_allocations = (
        allocate_supplier_payment_across_items(
            payment_allocation=settlement_amount_total,
            source_allocation=commission_amount_total,
            target_exchange_rate=target_exchange_rate,
            settlement_fx_rate=settlement_fx_rate,
            items=items,
        )
        if allocate_supplier_payment_across_items
        else []
    )
    _expected_map = {a.get("item"): a for a in (_expected_allocations or [])}

    # Idempotency: check for existing payout events from this exact payment
    # BEFORE outstanding-balance validation, so retries return the already
    # created events instead of being rejected with "no outstanding".
    try:
        _raw_existing = frappe.db.sql(
            "SELECT name, event_type, commission_amount, settlement_amount, "
            "company_amount, source_amount, source_row, settlement_item, "
            "settlement, commission_entry, source_doctype, source_name, "
            "source_currency, settlement_currency, commission_currency, "
            "company_currency, fx_from_currency, fx_to_currency, fx_rate, "
            "fx_date, fx_source, "
            "payment_fx_from_currency, payment_fx_to_currency, payment_fx_rate, "
            "payment_fx_date, payment_fx_source, "
            "commission_to_company_rate, "
            "commission_fx_from_currency, commission_fx_to_currency, "
            "commission_fx_rate, commission_fx_date, commission_fx_source "
            "FROM `tabCommission Payout Event` "
            "WHERE source_doctype = %s AND source_name = %s "
            "  AND settlement = %s AND docstatus = 1 "
            "FOR UPDATE",
            (source_doctype, source_name, sett_name),
            as_dict=True,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "Manual payout idempotency lookup failed",
        )
        raise
    existing_event_names = []
    existing_net_comm = 0.0
    has_reversal = False
    for _r in (_raw_existing or []):
        _et = _get_attr(_r, "event_type", None)
        _amt = flt(_get_attr(_r, "commission_amount", 0))
        if _et == "Payout":
            existing_event_names.append(_get_attr(_r, "name", None))
            existing_net_comm += _amt
        elif _et in ("Payout Reversal", "Recovery", "Recovery Reversal"):
            has_reversal = True
            existing_net_comm -= _amt
    if existing_event_names:
        prec_id = _cp(commission_currency)
        tol_id = (10 ** (-prec_id)) if prec_id and prec_id > 0 else 0.005
        if has_reversal:
            frappe.throw(
                _("Existing payout events include reversals or recoveries; refusing to reprocess.")
            )
        if abs(existing_net_comm - flt(amount, prec_id)) > tol_id:
            frappe.throw(
                _("Existing payout events for this evidence do not match the requested amount; refusing to reprocess.")
            )
        # Verify each existing event references a valid settlement item and
        # amounts/identity are consistent. Do not require the event count to
        # equal the total number of settlement child rows, because the
        # allocator only creates events for items with a positive remaining
        # balance (some items may already be fully paid).
        item_names = {it["item"] for it in items}
        seen_items = set()
        for _r in (_raw_existing or []):
            if _get_attr(_r, "event_type", None) != "Payout":
                continue
            si = _get_attr(_r, "settlement_item", None)
            if si is None:
                si = _get_attr(_r, "source_row", None)
            if si not in item_names:
                frappe.throw(
                    _("Existing payout event references an unknown settlement item; refusing to reprocess.")
                )
            if si in seen_items:
                frappe.throw(
                    _("Duplicate existing payout event for a settlement item; refusing to reprocess.")
                )
            seen_items.add(si)
            # Exact per-item allocation comparison: amounts, currencies, source
            # voucher, settlement, commission entry and FX snapshots must all
            # match the expected allocation for this item.
            exp = _expected_map.get(si)
            if exp is None:
                frappe.throw(
                    _("Existing payout event has no matching expected allocation; refusing to reprocess.")
                )
            prec_comm = _cp(commission_currency)
            prec_sett = _cp(settlement_currency)
            prec_comp = _cp(company_currency)
            prec_src = _cp(commission_currency)
            tol = 0.005
            def _cmp(field, got, expv, prec):
                try:
                    if abs(flt(got, prec) - flt(expv, prec)) > ((10 ** (-prec)) if prec and prec > 0 else tol):
                        return False
                except Exception:
                    return False
                return True
            if not _cmp("commission_amount", _get_attr(_r, "commission_amount", 0), exp.get("commission_amount", 0), prec_comm):
                frappe.throw(_("Existing payout event commission amount does not match; refusing to reprocess."))
            if not _cmp("settlement_amount", _get_attr(_r, "settlement_amount", 0), exp.get("settlement_amount", 0), prec_sett):
                frappe.throw(_("Existing payout event settlement amount does not match; refusing to reprocess."))
            if not _cmp("company_amount", _get_attr(_r, "company_amount", 0), exp.get("company_amount", 0), prec_comp):
                frappe.throw(_("Existing payout event company amount does not match; refusing to reprocess."))
            if not _cmp("source_amount", _get_attr(_r, "source_amount", 0), exp.get("source_amount", 0), prec_src):
                frappe.throw(_("Existing payout event source amount does not match; refusing to reprocess."))
            # Currencies (fail closed: missing does not match).
            if _get_attr(_r, "source_currency", None) != commission_currency:
                frappe.throw(_("Existing payout event source currency does not match; refusing to reprocess."))
            if _get_attr(_r, "settlement_currency", None) != settlement_currency:
                frappe.throw(_("Existing payout event settlement currency does not match; refusing to reprocess."))
            if _get_attr(_r, "commission_currency", None) != commission_currency:
                frappe.throw(_("Existing payout event commission currency does not match; refusing to reprocess."))
            if _get_attr(_r, "company_currency", None) != company_currency:
                frappe.throw(_("Existing payout event company currency does not match; refusing to reprocess."))
            # Source voucher / settlement / commission entry (fail closed).
            if _get_attr(_r, "source_doctype", None) != source_doctype:
                frappe.throw(_("Existing payout event source DocType does not match; refusing to reprocess."))
            if _get_attr(_r, "source_name", None) != source_name:
                frappe.throw(_("Existing payout event source document does not match; refusing to reprocess."))
            if _get_attr(_r, "settlement", None) != sett_name:
                frappe.throw(_("Existing payout event settlement does not match; refusing to reprocess."))
            ce_name = item_to_entry.get(si)
            if ce_name and _get_attr(_r, "commission_entry", None) != ce_name:
                frappe.throw(_("Existing payout event commission entry does not match; refusing to reprocess."))
            # FX snapshots — fail closed: a missing historical value does not
            # match a populated requested value.
            def _req_equal(field, got, exp):
                if exp is None:
                    # Requested missing: accept only if historical also missing.
                    return got in (None, "", 0, 0.0)
                if got is None or got == "":
                    return False
                return True

            # Settlement FX.
            if not _req_equal("fx_from_currency", _get_attr(_r, "fx_from_currency", None), settlement_currency) or _get_attr(_r, "fx_from_currency", None) != settlement_currency:
                frappe.throw(_("Existing payout event settlement FX from currency does not match; refusing to reprocess."))
            if not _req_equal("fx_to_currency", _get_attr(_r, "fx_to_currency", None), company_currency) or _get_attr(_r, "fx_to_currency", None) != company_currency:
                frappe.throw(_("Existing payout event settlement FX to currency does not match; refusing to reprocess."))
            if _get_attr(_r, "fx_rate", None) is None:
                frappe.throw(_("Existing payout event settlement FX rate is missing; refusing to reprocess."))
            if abs(flt(_get_attr(_r, "fx_rate", 0)) - flt(settlement_fx_rate)) > (10 ** (-6)):
                frappe.throw(_("Existing payout event settlement FX rate does not match; refusing to reprocess."))
            if _get_attr(_r, "fx_date", None) is None or str(getdate(_get_attr(_r, "fx_date", None))) != payment_date:
                frappe.throw(_("Existing payout event settlement FX date does not match; refusing to reprocess."))
            if _get_attr(_r, "fx_source", None) != "Manual Payout":
                frappe.throw(_("Existing payout event settlement FX source does not match; refusing to reprocess."))

            # Commission FX.
            if _get_attr(_r, "commission_fx_from_currency", None) != commission_currency:
                frappe.throw(_("Existing payout event commission FX from currency does not match; refusing to reprocess."))
            if _get_attr(_r, "commission_fx_to_currency", None) != company_currency:
                frappe.throw(_("Existing payout event commission FX to currency does not match; refusing to reprocess."))
            if _get_attr(_r, "commission_fx_rate", None) is None:
                frappe.throw(_("Existing payout event commission FX rate is missing; refusing to reprocess."))
            if abs(flt(_get_attr(_r, "commission_fx_rate", 0)) - flt(target_exchange_rate)) > (10 ** (-6)):
                frappe.throw(_("Existing payout event commission FX rate does not match; refusing to reprocess."))
            if _get_attr(_r, "commission_to_company_rate", None) is not None:
                if abs(flt(_get_attr(_r, "commission_to_company_rate", 0)) - flt(target_exchange_rate)) > (10 ** (-6)):
                    frappe.throw(_("Existing payout event commission-to-company rate does not match; refusing to reprocess."))
            else:
                frappe.throw(_("Existing payout event commission-to-company rate is missing; refusing to reprocess."))
            if _get_attr(_r, "commission_fx_date", None) is None or str(getdate(_get_attr(_r, "commission_fx_date", None))) != payment_date:
                frappe.throw(_("Existing payout event commission FX date does not match; refusing to reprocess."))
            if _get_attr(_r, "commission_fx_source", None) != "Manual Payout target_exchange_rate":
                frappe.throw(_("Existing payout event commission FX source does not match; refusing to reprocess."))

            # Payment FX (Payment Entry evidence only).
            if source_doctype == "Payment Entry":
                if _get_attr(_r, "payment_fx_from_currency", None) != bank_fx_from:
                    frappe.throw(_("Existing payout event payment FX from currency does not match; refusing to reprocess."))
                if _get_attr(_r, "payment_fx_to_currency", None) != company_currency:
                    frappe.throw(_("Existing payout event payment FX to currency does not match; refusing to reprocess."))
                if _get_attr(_r, "payment_fx_rate", None) is None:
                    frappe.throw(_("Existing payout event payment FX rate is missing; refusing to reprocess."))
                if abs(flt(_get_attr(_r, "payment_fx_rate", 0)) - flt(bank_fx_rate)) > (10 ** (-6)):
                    frappe.throw(_("Existing payout event payment FX rate does not match; refusing to reprocess."))
                if _get_attr(_r, "payment_fx_date", None) is None or str(getdate(_get_attr(_r, "payment_fx_date", None))) != payment_date:
                    frappe.throw(_("Existing payout event payment FX date does not match; refusing to reprocess."))
                if _get_attr(_r, "payment_fx_source", None) != "Payment Entry source_exchange_rate":
                    frappe.throw(_("Existing payout event payment FX source does not match; refusing to reprocess."))
        # Exact retry: ensure the complete expected allocation is present.
        if seen_items != set(_expected_map):
            frappe.throw(
                _("Existing payout events do not match the complete expected allocation.")
            )
        # Exact retry: return existing events without reallocating.
        if refresh_settlement_payout_status is not None:
            refresh_settlement_payout_status(sett_name)
        return existing_event_names

    # Outstanding check (fail closed).
    if compute_payout_status is None:
        frappe.throw(_("Payout status calculation is unavailable."))
    try:
        ps = compute_payout_status(sett)
        outstanding = flt(ps.get("outstanding_payout_amount", 0))
    except Exception:
        frappe.throw(_("Unable to compute authoritative payout status; refusing to record payment."))
    if outstanding <= 0:
        frappe.throw(_("No outstanding payout amount remaining."))
    prec = _cp(commission_currency)
    tolerance = (10 ** (-prec)) if prec and prec > 0 else 0.005
    if amount > outstanding + tolerance:
        frappe.throw(
            _("Payment amount {0} exceeds outstanding payout {1}.").format(amount, outstanding)
        )

    allocations = _expected_allocations if _expected_allocations else allocate_supplier_payment_across_items(
        payment_allocation=settlement_amount_total,
        source_allocation=commission_amount_total,
        target_exchange_rate=target_exchange_rate,
        settlement_fx_rate=settlement_fx_rate,
        items=items,
    )

    beneficiary_type = getattr(sett, "beneficiary_type", None)
    beneficiary = _get_attr(sett, "sales_person", None) or _get_attr(sett, "sales_partner", None)
    company = getattr(sett, "company", None)

    # Bank-side (paid_from) snapshot for Payment Entry evidence.
    bank_fx_from = None
    bank_fx_rate = None
    pe_company = None
    if source_doctype == "Payment Entry":
        try:
            pe_doc = frappe.get_doc("Payment Entry", source_name)
            bank_fx_from = getattr(pe_doc, "paid_from_account_currency", None)
            bank_fx_rate = getattr(pe_doc, "source_exchange_rate", None)
            paid_from_account = getattr(pe_doc, "paid_from", None)
            paid_amount = flt(getattr(pe_doc, "paid_amount", 0) or 0)
            base_paid_amount = flt(getattr(pe_doc, "base_paid_amount", 0) or 0)
            pe_company = getattr(pe_doc, "company", None)
        except Exception:
            bank_fx_from = None
            bank_fx_rate = None
            paid_from_account = None
            paid_amount = 0.0
            base_paid_amount = 0.0
            pe_company = None
        if not pe_company:
            frappe.throw(_("Payment Entry company could not be resolved."))
        if pe_company != sett.company:
            frappe.throw(_("Payment Entry company must match the settlement company."))
        if not paid_from_account:
            frappe.throw(_("Payment Entry paid_from is required."))
        try:
            actual_currency = frappe.get_cached_value("Account", paid_from_account, "account_currency")
        except Exception:
            actual_currency = None
        if not actual_currency:
            frappe.throw(_("Cannot resolve bank account currency."))
        if bank_fx_from and actual_currency != bank_fx_from:
            frappe.throw(_("Payment Entry paid_from account currency does not match the Account record."))
        if not bank_fx_from:
            bank_fx_from = actual_currency
        if not bank_fx_from:
            frappe.throw(_("Cannot resolve the Payment Entry paid_from account currency."))
        _account = frappe.db.get_value(
            "Account", paid_from_account,
            ["company", "account_currency", "account_type"], as_dict=True,
        )
        if not _account:
            frappe.throw(_("Bank account does not exist."))
        if not _account.company:
            frappe.throw(_("Bank account company could not be resolved."))
        if _account.company != pe_company:
            frappe.throw(_("Bank account company does not match Payment Entry."))
        if _account.account_type not in ("Cash", "Bank"):
            frappe.throw(_("paid_from must be a Cash or Bank account."))
        if not _account.account_currency:
            frappe.throw(_("Cannot resolve bank account currency."))
        if _account.account_currency != bank_fx_from:
            frappe.throw(_("Payment Entry bank currency does not match its account."))
        # Mandatory amount positivity (always).
        if not _is_finite(paid_amount) or paid_amount <= 0:
            frappe.throw(_("Payment Entry paid amount must be positive."))
        if not _is_finite(base_paid_amount) or base_paid_amount <= 0:
            frappe.throw(_("Payment Entry base paid amount must be positive."))
        # Fail closed: if bank and company currencies differ, a valid rate is
        # required. Do not silently substitute None.
        if bank_fx_from != company_currency:
            if not bank_fx_rate or not _is_finite(bank_fx_rate) or float(bank_fx_rate) <= 0:
                frappe.throw(
                    _("Missing or invalid source exchange rate on Payment Entry {0}.").format(source_name)
                )
            if not _is_finite(bank_fx_rate) or bank_fx_rate <= 0:
                frappe.throw(_("Invalid bank-side exchange rate."))
        else:
            bank_fx_rate = 1.0
        # Reconcile base_paid = paid * source_exchange_rate for all currencies.
        if not _is_finite(bank_fx_rate) or bank_fx_rate <= 0:
            frappe.throw(_("Invalid bank-side exchange rate."))
        expected_base = flt(paid_amount * bank_fx_rate, _cp(company_currency))
        prec = _cp(company_currency)
        tolerance = 10 ** (-prec) if prec is not None and prec >= 0 else 0.005
        if abs(expected_base - base_paid_amount) > tolerance:
            frappe.throw(_("Payment Entry bank-side amount does not reconcile."))

    # Voucher-total reconciliation: the sum of all event company amounts must
    # match the voucher's company-currency total.  This is the multi-item
    # check that the per-event controller cannot perform.
    total_alloc_company = sum(flt(a.get("company_amount", 0)) for a in allocations)
    if source_doctype == "Journal Entry":
        je_doc = frappe.get_doc("Journal Entry", source_name)
        je_total = flt(getattr(je_doc, "total_debit", 0) or 0)
        prec = _cp(company_currency)
        tolerance = (10 ** (-prec)) if prec and prec > 0 else 0.005
        if abs(je_total - total_alloc_company) > tolerance:
            frappe.throw(
                _("Journal Entry total debit ({0}) does not match the sum of "
                  "allocated company amounts ({1}).").format(
                    je_total, total_alloc_company
                )
            )
    elif source_doctype == "Payment Entry":
        pe_doc = frappe.get_doc("Payment Entry", source_name)
        pe_total = flt(getattr(pe_doc, "base_received_amount", 0) or 0)
        if pe_total <= 0:
            frappe.throw(
                _("Payment Entry base received amount is required for voucher reconciliation.")
            )
        # Verify base_received = received * target_exchange_rate.
        pe_received = flt(getattr(pe_doc, "received_amount", 0) or 0)
        pe_rate = flt(getattr(pe_doc, "target_exchange_rate", 0) or 0)
        if pe_rate <= 0:
            pe_rate = 1.0 if company_currency == commission_currency else pe_rate
        if pe_received > 0 and pe_rate > 0:
            expected_base = flt(pe_received * pe_rate, _cp(company_currency))
            prec = _cp(company_currency)
            tolerance = (10 ** (-prec)) if prec and prec > 0 else 0.005
            if abs(expected_base - pe_total) > tolerance:
                frappe.throw(
                    _("Payment Entry beneficiary-side amount does not reconcile.")
                )
        prec = _cp(company_currency)
        tolerance = (10 ** (-prec)) if prec and prec > 0 else 0.005
        if abs(pe_total - total_alloc_company) > tolerance:
            frappe.throw(
                _("Payment Entry total ({0}) does not match the sum of "
                  "allocated company amounts ({1}).").format(
                    pe_total, total_alloc_company
                )
            )

    created_events = []
    for alloc in allocations:
        item_name = alloc["item"]
        ce_name = item_to_entry.get(item_name)
        if not ce_name:
            continue
        event_key = generate_event_key(
            source_doctype,
            source_name,
            item_name,
            sett_name,
            item_name,
            ce_name,
            "Payout",
        )
        payload = {
            "event_key": event_key,
            "event_type": "Payout",
            "posting_date": payment_date,
            "evidence_level": "Manual Evidence",
            "settlement": sett_name,
            "settlement_item": item_name,
            "commission_entry": ce_name,
            "company": company,
            "beneficiary_type": beneficiary_type,
            "beneficiary": beneficiary,
            "source_doctype": source_doctype,
            "source_name": source_name,
            "source_row": item_name,
            "source_currency": commission_currency,
            "source_amount": alloc.get("source_amount", amount),
            "settlement_currency": settlement_currency,
            "settlement_amount": alloc.get("settlement_amount", amount),
            "commission_currency": commission_currency,
            "commission_amount": alloc.get("commission_amount", amount),
            "company_currency": company_currency,
            "company_amount": alloc.get("company_amount", amount),
            "fx_from_currency": settlement_currency,
            "fx_to_currency": company_currency,
            "fx_rate": settlement_fx_rate,
            "fx_date": payment_date,
            "fx_source": "Manual Payout",
            "commission_to_company_rate": target_exchange_rate,
            "commission_fx_from_currency": commission_currency,
            "commission_fx_to_currency": company_currency,
            "commission_fx_rate": target_exchange_rate,
            "commission_fx_date": payment_date,
            "commission_fx_source": "Manual Payout target_exchange_rate",
            "remarks": remarks,
            "evidence_attachment": evidence_attachment,
            "payment_fx_from_currency": bank_fx_from if source_doctype == "Payment Entry" else None,
            "payment_fx_to_currency": company_currency if source_doctype == "Payment Entry" else None,
            "payment_fx_rate": bank_fx_rate if source_doctype == "Payment Entry" else None,
            "payment_fx_date": payment_date if source_doctype == "Payment Entry" else None,
            "payment_fx_source": "Payment Entry source_exchange_rate" if source_doctype == "Payment Entry" else None,
        }
        name = create_and_submit_payout_event(payload)
        created_events.append(name)

    if refresh_settlement_payout_status is not None:
        refresh_settlement_payout_status(sett_name)

    return created_events


def _is_finite(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    import math
    return math.isfinite(f)


# Aliases
def record_manual_payout_event(*a, **k):
    return record_manual_payout(*a, **k)

def record_manual_payment(*a, **k):
    return record_manual_payout(*a, **k)

def process_manual_payout(*a, **k):
    return record_manual_payout(*a, **k)

def manual_payment(*a, **k):
    return record_manual_payout(*a, **k)

def create_manual_payment(*a, **k):
    return record_manual_payout(*a, **k)

def record_manual_payout_for(*a, **k):
    return record_manual_payout(*a, **k)

def manual_payout(*a, **k):
    return record_manual_payout(*a, **k)

def create_manual_payout(*a, **k):
    return record_manual_payout(*a, **k)

def record_manual_payout_for_settlement(*a, **k):
    return record_manual_payout(*a, **k)


def _whitelist(fn):
    return frappe.whitelist()(fn)


record_manual_payout = _whitelist(record_manual_payout)
record_manual_payout_event = _whitelist(record_manual_payout_event)
manual_payout = _whitelist(manual_payout)
create_manual_payout = _whitelist(create_manual_payout)
record_manual_payout_for_settlement = _whitelist(record_manual_payout_for_settlement)
record_manual_payment = _whitelist(record_manual_payment)
process_manual_payout = _whitelist(process_manual_payout)
manual_payment = _whitelist(manual_payment)
create_manual_payment = _whitelist(create_manual_payment)
record_manual_payout_for = _whitelist(record_manual_payout_for)