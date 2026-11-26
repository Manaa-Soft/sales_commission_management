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


def _validate_evidence(source_doctype, source_name, sett, amount, payment_date, commission_currency):
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

    # Company check.
    ev_company = getattr(ev, "company", None)
    if ev_company and company and ev_company != company:
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
        _validate_journal_entry_evidence(ev, sett, amount, commission_currency, company)
    elif source_doctype == "Payment Entry":
        _validate_payment_entry_evidence(ev, sett, amount, commission_currency, company)


def _resolve_payable_account(company):
    try:
        accrued, payable, expense, _cc, _pe = None, None, None, None, None
        from sales_commission_management.sales_commission_management.doctype.commission_settlement.commission_settlement import (
            _resolve_settlement_accounts,
        )

        res = _resolve_settlement_accounts(company)
        if isinstance(res, (tuple, list)):
            if len(res) >= 2:
                payable = res[1]
        elif isinstance(res, dict):
            payable = res.get("payable") or res.get("commission_payable_account")
    except Exception:
        payable = None
    return payable


def _validate_journal_entry_evidence(ev, sett, amount, commission_currency, company):
    """Validate a submitted Journal Entry as commission payment evidence."""
    # Debit/credit equality and amount.
    total_debit = flt(getattr(ev, "total_debit", 0) or 0)
    total_credit = flt(getattr(ev, "total_credit", 0) or 0)
    if total_debit <= 0 or total_credit <= 0:
        frappe.throw(_("Journal Entry must contain debit and credit lines."))
    if abs(total_debit - total_credit) > 0.005:
        frappe.throw(_("Journal Entry debits and credits do not balance."))
    prec = _cp(commission_currency)
    tolerance = (10 ** (-prec)) if prec and prec > 0 else 0.005
    if abs(total_debit - flt(amount, prec)) > tolerance and abs(total_credit - flt(amount, prec)) > tolerance:
        frappe.throw(_("Journal Entry amount does not match payment amount."))

    # Currency: if a transaction currency is present, it must match.
    ev_currency = getattr(ev, "currency", None)
    if ev_currency and commission_currency and ev_currency != commission_currency:
        frappe.throw(_("Journal Entry currency does not match commission currency."))

    # Accounts: the commission payable account must be among the rows, and at
    # least one cash/bank (asset) account must be present.
    accounts = getattr(ev, "accounts", None) or []
    account_names = []
    for row in accounts or []:
        acct = getattr(row, "account", None) if not isinstance(row, dict) else row.get("account")
        if acct:
            account_names.append(acct)
    if not account_names:
        frappe.throw(_("Journal Entry has no account lines."))

    payable = _resolve_payable_account(company)
    if payable and payable not in account_names:
        frappe.throw(_("Journal Entry does not include the commission payable account."))

    has_asset = False
    for acct in account_names:
        try:
            root_type = frappe.get_cached_value("Account", acct, "root_type")
        except Exception:
            root_type = None
        if root_type == "Asset":
            has_asset = True
            break
    if not has_asset:
        frappe.throw(_("Journal Entry must include a cash or bank (asset) account."))

    # Beneficiary / party reference where applicable: if the settlement has a
    # beneficiary and the JE rows carry a party, it must match.
    beneficiary = _get_attr(sett, "sales_person", None) or _get_attr(sett, "sales_partner", None)
    party_found = False
    for row in accounts or []:
        party = getattr(row, "party", None) if not isinstance(row, dict) else row.get("party")
        if party:
            party_found = True
            if beneficiary and party != beneficiary:
                frappe.throw(_("Journal Entry party does not match settlement beneficiary."))
    # Party is optional; not all JEs carry a party. Only enforce when present.


def _validate_payment_entry_evidence(ev, sett, amount, commission_currency, company):
    """Validate a Payment Entry as evidence for the Manual Approved Route."""
    if getattr(ev, "payment_type", None) not in (None, "Pay", "Receive"):
        frappe.throw(_("Payment Entry must be a Pay or Receive entry."))

    # Amount: paid_amount / received_amount.
    ev_amount = None
    for f in ("paid_amount", "received_amount", "amount"):
        v = getattr(ev, f, None)
        if v is not None:
            ev_amount = v
            break
    if ev_amount is None:
        frappe.throw(_("Payment Entry amount is missing."))
    prec = _cp(commission_currency)
    tolerance = (10 ** (-prec)) if prec and prec > 0 else 0.005
    if abs(flt(ev_amount, prec) - flt(amount, prec)) > tolerance:
        frappe.throw(_("Payment Entry amount does not match payment amount."))

    # Currency check.
    ev_currency = getattr(ev, "currency", None) or getattr(ev, "paid_from_account_currency", None)
    if ev_currency and commission_currency and ev_currency != commission_currency:
        # Allow company currency? keep strict match to commission currency.
        frappe.throw(_("Payment Entry currency does not match commission currency."))

    # Posting date already checked by caller. Company checked by caller.


def record_manual_payout(settlement=None, **kwargs):
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

    # Target (commission -> company) rate.
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

    # Validate evidence (company, currency, amount, date).
    _validate_evidence(source_doctype, source_name, sett, amount, payment_date, commission_currency)

    # Amounts: input amount is commission currency.
    commission_amount_total = flt(amount, _cp(commission_currency))
    if company_currency != commission_currency:
        company_amount_total = flt(commission_amount_total * target_exchange_rate, _cp(company_currency))
    else:
        company_amount_total = commission_amount_total
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
        prev_paid = _previously_paid_for_item(si_name) if _previously_paid_for_item else 0.0
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

    # Idempotency: check for existing payout events from this exact payment
    # BEFORE outstanding-balance validation, so retries return the already
    # created events instead of being rejected with "no outstanding".
    try:
        _raw_existing = frappe.db.sql(
            "SELECT name, event_type, commission_amount "
            "FROM `tabCommission Payout Event` "
            "WHERE source_doctype = %s AND source_name = %s "
            "  AND settlement = %s AND docstatus = 1 "
            "FOR UPDATE",
            (source_doctype, source_name, sett_name),
            as_dict=True,
        )
    except Exception:
        _raw_existing = []
    existing_event_names = []
    existing_net_comm = 0.0
    for _r in (_raw_existing or []):
        _et = _get_attr(_r, "event_type", None)
        _amt = flt(_get_attr(_r, "commission_amount", 0))
        if _et == "Payout":
            existing_event_names.append(_get_attr(_r, "name", None))
            existing_net_comm += _amt
        elif _et == "Payout Reversal":
            existing_net_comm -= _amt
    if existing_event_names:
        prec_id = _cp(commission_currency)
        tol_id = (10 ** (-prec_id)) if prec_id and prec_id > 0 else 0.005
        if abs(existing_net_comm - flt(amount, prec_id)) <= tol_id:
            # Exact retry: return existing events without reallocating.
            if refresh_settlement_payout_status is not None:
                try:
                    refresh_settlement_payout_status(sett_name)
                except Exception:
                    pass
            return existing_event_names
        frappe.throw(
            _("Existing payout events for this evidence do not match the requested amount; refusing to reprocess.")
        )

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

    allocations = allocate_supplier_payment_across_items(
        payment_allocation=settlement_amount_total,
        source_allocation=commission_amount_total,
        target_exchange_rate=target_exchange_rate,
        settlement_fx_rate=settlement_fx_rate,
        items=items,
    )

    beneficiary_type = getattr(sett, "beneficiary_type", None)
    beneficiary = _get_attr(sett, "sales_person", None) or _get_attr(sett, "sales_partner", None)
    company = getattr(sett, "company", None)

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
            "remarks": remarks,
            "evidence_attachment": evidence_attachment,
        }
        name = create_and_submit_payout_event(payload)
        created_events.append(name)

    if refresh_settlement_payout_status is not None:
        try:
            refresh_settlement_payout_status(sett_name)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "Manual payout status refresh failed for {0}".format(sett_name),
            )

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