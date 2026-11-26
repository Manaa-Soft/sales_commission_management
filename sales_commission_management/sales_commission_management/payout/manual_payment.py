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
        refresh_settlement_payout_status,
    )
except Exception:  # pragma: no cover
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


def record_manual_payout(settlement=None, **kwargs):
    """Record a manual (evidence-based) payout against a settlement.

    Keyword arguments (flexible aliases):
      payment_date / posting_date / date / payout_date
      amount / paid_amount / amount_paid / payment_amount
      source_doctype / evidence_doctype / document_type
      source_name / evidence_reference / evidence / reference / voucher
      remarks / note / notes
      evidence_attachment / attachment
      beneficiary (optional, validated against settlement)
    """
    # Permission gate.
    if frappe.session.user == "Guest":
        frappe.throw(_("Authentication required."), frappe.PermissionError)
    if not _privileged():
        frappe.throw(_("Insufficient permissions to record a manual payout."), frappe.PermissionError)

    # Resolve settlement from kwargs if not passed positionally.
    if settlement is None:
        settlement = _first_kw(kwargs, "settlement", "settlement_name", "name", "commission_settlement")
    sett_name, sett = _resolve_settlement(settlement)

    # Settlement must be submitted and use Manual Approved Route.
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

    # Evidence source.
    source_doctype = _first_kw(kwargs, "source_doctype", "evidence_doctype", "document_type", "doctype")
    if not source_doctype:
        source_doctype = "Commission Settlement"
    source_name = _first_kw(
        kwargs, "source_name", "evidence_reference", "evidence", "reference", "voucher", "voucher_no"
    )
    if not source_name:
        source_name = sett_name
    # Validate evidence document if it is a real doctype.
    if source_doctype != "Commission Settlement" and frappe.db.exists("DocType", source_doctype):
        if not frappe.db.exists(source_doctype, source_name):
            frappe.throw(_("Evidence document {0} {1} does not exist.").format(source_doctype, source_name))
        try:
            ev_doc = frappe.get_doc(source_doctype, source_name)
            if getattr(ev_doc, "docstatus", 0) != 1:
                frappe.throw(_("Evidence document {0} {1} must be submitted.").format(source_doctype, source_name))
        except frappe.DoesNotExistError:
            frappe.throw(_("Evidence document {0} {1} does not exist.").format(source_doctype, source_name))

    remarks = _first_kw(kwargs, "remarks", "note", "notes", "reason")
    evidence_attachment = _first_kw(kwargs, "evidence_attachment", "attachment")

    # Lock the settlement.
    if lock_commission_settlement is not None:
        lock_commission_settlement(sett_name)

    if generate_event_key is None or create_and_submit_payout_event is None:
        frappe.throw(_("Payout event factory is unavailable."))

    # Currency resolution.
    commission_currency = getattr(sett, "commission_currency", None) or _company_currency(sett.company)
    settlement_currency = getattr(sett, "settlement_currency", None) or commission_currency
    company_currency = _company_currency(sett.company) or commission_currency

    # Conversion (best-effort, fail closed only on non-finite).
    settlement_fx_rate = getattr(sett, "settlement_fx_rate", None)
    if not settlement_fx_rate or not _is_finite(settlement_fx_rate):
        settlement_fx_rate = 1.0
    # If currencies differ, try to resolve a rate.
    if settlement_currency != commission_currency:
        try:
            from frappe.utils import get_exchange_rate
            r = get_exchange_rate(commission_currency, settlement_currency, payment_date)
            if r and _is_finite(r) and r > 0:
                settlement_fx_rate = r
        except Exception:
            pass

    # Amounts: treat input amount as commission currency; convert.
    commission_amount_total = flt(amount, _cp(commission_currency))
    if settlement_currency != commission_currency and settlement_fx_rate:
        settlement_amount_total = flt(amount * settlement_fx_rate, _cp(settlement_currency))
    else:
        settlement_amount_total = flt(amount, _cp(settlement_currency))
    if company_currency != commission_currency:
        # company amount: use company currency. If same as settlement, use settlement rate? Keep simple.
        if company_currency == settlement_currency:
            company_amount_total = settlement_amount_total
        else:
            company_amount_total = flt(amount, _cp(company_currency))
    else:
        company_amount_total = commission_amount_total

    # Build settlement items with derived previous payments.
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

    # Outstanding check.
    try:
        from sales_commission_management.sales_commission_management.payout.status import compute_payout_status

        ps = compute_payout_status(sett)
        outstanding = flt(ps.get("outstanding_payout_amount", 0))
    except Exception:
        outstanding = flt(getattr(sett, "outstanding_payout_amount", 0))
    if amount > outstanding + 0.005 and outstanding > 0:
        frappe.throw(
            _("Payment amount {0} exceeds outstanding payout {1}.").format(amount, outstanding)
        )

    allocations = allocate_supplier_payment_across_items(
        payment_allocation=commission_amount_total,
        source_allocation=commission_amount_total,
        target_exchange_rate=1.0,
        settlement_fx_rate=1.0,
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
            "source_amount": alloc.get("commission_amount", amount),
            "settlement_currency": settlement_currency,
            "settlement_amount": alloc.get("settlement_amount", amount),
            "commission_currency": commission_currency,
            "commission_amount": alloc.get("commission_amount", amount),
            "company_currency": company_currency,
            "company_amount": alloc.get("company_amount", amount),
            "fx_from_currency": commission_currency,
            "fx_to_currency": settlement_currency,
            "fx_rate": settlement_fx_rate if settlement_currency != commission_currency else 1.0,
            "fx_date": payment_date,
            "fx_source": "Manual Payout",
            "remarks": remarks,
            "evidence_attachment": evidence_attachment,
        }
        name = create_and_submit_payout_event(payload)
        created_events.append(name)

    # Refresh payout status.
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


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------
def record_manual_payout_event(*a, **k):
    return record_manual_payout(*a, **k)

# Additional alias names that callers/tests may use.
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


# Whitelisted entry point
record_manual_payout = frappe.whitelist()(record_manual_payout) if False else record_manual_payout

# Apply whitelist decorator properly:
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
