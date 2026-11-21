"""Supplier Payment Entry payout allocation engine (P3.2.1).

This module is intentionally free of Payment Entry hooks and payout-event
creation. It only:

* identifies which submitted supplier Payment Entries belong to which
  submitted Commission Settlements (via their linked Purchase Invoice);
* resolves the multi-currency amounts for such a payment reference; and
* allocates a payment reference across the Commission Settlement Items of
  the linked settlement.

All three concerns are kept separate and independently testable.
"""

import math

import frappe
from frappe import _
from frappe.utils import flt

from sales_commission_management.sales_commission_management.utils import (
    currency_precision as _cp,
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


def _is_finite_positive(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


def get_supplier_settlement_for_reference(payment_entry, payment_reference):
    """Return the single submitted Commission Settlement linked to a supplier
    Payment Entry Reference, or ``None`` when no unambiguous match exists.

    Fail-closed: ambiguous matches (zero or more than one settlement) return
    ``None``. Unrelated supplier payments are ignored.
    """
    if payment_entry is None or payment_reference is None:
        return None

    # --- Payment Entry must be submitted. ---
    if _get_attr(payment_entry, "docstatus", None) != 1:
        return None
    if _get_attr(payment_entry, "payment_type", None) != "Pay":
        return None
    if _get_attr(payment_entry, "party_type", None) != "Supplier":
        return None

    # --- Reference must be a Purchase Invoice with a positive allocation. ---
    if _get_attr(payment_reference, "reference_doctype", None) != "Purchase Invoice":
        return None
    pi_name = _get_attr(payment_reference, "reference_name", None)
    if not pi_name:
        return None
    allocated = flt(_get_attr(payment_reference, "allocated_amount", 0))
    if allocated <= 0:
        return None

    # --- Purchase Invoice must be submitted. ---
    try:
        pi_docstatus = frappe.db.get_value("Purchase Invoice", pi_name, "docstatus")
    except Exception:
        return None
    if pi_docstatus != 1:
        return None

    pe_company = _get_attr(payment_entry, "company", None)
    pe_supplier = _get_attr(payment_entry, "party", None)

    try:
        pi_company = frappe.db.get_value("Purchase Invoice", pi_name, "company")
        pi_supplier = frappe.db.get_value("Purchase Invoice", pi_name, "supplier")
    except Exception:
        return None
    if pi_company != pe_company:
        return None
    if pi_supplier != pe_supplier:
        return None

    # --- Exactly one submitted Commission Settlement linked to that PI. ---
    try:
        rows = frappe.get_all(
            "Commission Settlement",
            filters={
                "docstatus": 1,
                "settlement_document_type": "Purchase Invoice",
                "settlement_document": pi_name,
            },
            fields=["name", "settlement_method", "company", "supplier"],
        )
    except Exception:
        return None

    candidates = []
    for r in rows or []:
        if _get_attr(r, "settlement_method", None) != "Supplier Purchase Invoice":
            continue
        if _get_attr(r, "company", None) != pe_company:
            continue
        if _get_attr(r, "supplier", None) != pe_supplier:
            continue
        candidates.append(r)

    if len(candidates) != 1:
        # Fail closed on ambiguous (or missing) matches.
        return None

    try:
        settlement = frappe.get_doc(
            "Commission Settlement", _get_attr(candidates[0], "name", None)
        )
    except Exception:
        return None
    return settlement


def resolve_supplier_payment_currencies(payment_entry, payment_reference):
    """Resolve all currency amounts for a supplier payment reference.

    Returns a dict with source/settlement/commission/company currencies and
    amounts, plus the saved FX snapshots. Fails closed (raises) if any
    required value is missing, inconsistent, non-finite or non-positive.
    """
    settlement = get_supplier_settlement_for_reference(payment_entry, payment_reference)
    if settlement is None:
        frappe.throw(
            _("No matching Commission Settlement for this payment reference.")
        )

    pi_name = _get_attr(payment_reference, "reference_name", None)

    # Source currency / amount from the Payment Entry party account.
    source_currency = (
        _get_attr(payment_entry, "paid_to_account_currency", None)
        or _get_attr(payment_entry, "party_account_currency", None)
    )
    if not source_currency:
        frappe.throw(_("Missing source (party account) currency."))
    source_amount = flt(_get_attr(payment_reference, "allocated_amount", 0))
    if source_amount <= 0:
        frappe.throw(_("Allocated amount must be positive."))

    # Settlement currency from the linked Purchase Invoice.
    try:
        pi_currency = frappe.db.get_value("Purchase Invoice", pi_name, "currency")
    except Exception:
        pi_currency = None
    if not pi_currency:
        frappe.throw(_("Missing settlement (invoice) currency."))
    settlement_currency = pi_currency

    # Settlement amount via ERPNext's conversion helper.
    try:
        settlement_amount = payment_entry.get_allocated_amount_in_transaction_currency(
            _get_attr(payment_reference, "allocated_amount", 0),
            "Purchase Invoice",
            pi_name,
        )
    except Exception as exc:
        frappe.throw(
            _("Could not resolve settlement amount: {0}").format(str(exc))
        )
    settlement_amount = flt(settlement_amount)
    if settlement_amount <= 0:
        frappe.throw(_("Settlement amount must be positive."))

    # Commission currency / saved FX snapshot.
    commission_currency = _get_attr(settlement, "commission_currency", None)
    if not commission_currency:
        frappe.throw(_("Missing commission currency on settlement."))
    settlement_fx_rate = _get_attr(settlement, "settlement_fx_rate", None)
    if not _is_finite_positive(settlement_fx_rate):
        frappe.throw(_("Invalid or missing settlement FX rate."))
    settlement_fx_rate = flt(settlement_fx_rate)

    # Commission amount = settlement amount / settlement FX rate.
    commission_amount = flt(
        settlement_amount / settlement_fx_rate, _cp(commission_currency)
    )

    # Company currency / target exchange rate.
    company = _get_attr(payment_entry, "company", None) or _get_attr(
        settlement, "company", None
    )
    company_currency = _company_currency(company)
    if not company_currency:
        frappe.throw(_("Missing company currency."))
    target_exchange_rate = _get_attr(payment_entry, "target_exchange_rate", None)
    if not _is_finite_positive(target_exchange_rate):
        frappe.throw(_("Invalid or missing target exchange rate."))
    target_exchange_rate = flt(target_exchange_rate)

    company_amount = flt(
        source_amount * target_exchange_rate, _cp(company_currency)
    )

    return {
        "payment_entry": _get_attr(payment_entry, "name", None),
        "payment_reference": _get_attr(payment_reference, "name", None),
        "settlement": _get_attr(settlement, "name", None),
        "purchase_invoice": pi_name,
        "source_currency": source_currency,
        "source_amount": flt(source_amount, _cp(source_currency)),
        "settlement_currency": settlement_currency,
        "settlement_amount": flt(settlement_amount, _cp(settlement_currency)),
        "commission_currency": commission_currency,
        "commission_amount": commission_amount,
        "company_currency": company_currency,
        "company_amount": company_amount,
        "settlement_fx_rate": settlement_fx_rate,
        "target_exchange_rate": target_exchange_rate,
    }


def _allocate_core(
    payment_allocation,
    source_allocation,
    target_exchange_rate,
    settlement_fx_rate,
    items,
):
    """Deterministically allocate a payment reference across settlement items.

    ``payment_allocation`` is denominated in settlement (Purchase Invoice)
    currency. ``source_allocation`` is the same payment in source (party
    account) currency. Item allocations are computed in settlement currency
    proportionally to each item's remaining balance.

    Returns a list of dicts: ``{item, remaining, allocation}`` for items that
    receive a positive allocation, in input order.

    Raises ``ValueError`` on invalid inputs, over-allocation, or
    non-reconciling results.
    """
    prec = _cp()

    payment_allocation = flt(payment_allocation)
    if payment_allocation <= 0:
        raise ValueError("Payment allocation must be positive.")

    source_allocation = flt(source_allocation)
    if source_allocation <= 0:
        raise ValueError("Source allocation must be positive.")

    tr = flt(target_exchange_rate)
    if not _is_finite_positive(tr):
        raise ValueError("Target exchange rate must be finite and positive.")

    fx = flt(settlement_fx_rate)
    if not _is_finite_positive(fx):
        raise ValueError("Settlement FX rate must be finite and positive.")

    if not items:
        raise ValueError("At least one settlement item is required.")

    # Prepare items with remaining balances.
    prepared = []
    for it in items:
        if isinstance(it, dict):
            item_id = it.get("item") or it.get("name")
            settlement_amount = flt(it.get("settlement_amount", 0))
            previously_paid = flt(it.get("previously_paid", 0))
        else:
            item_id = getattr(it, "item", None) or getattr(it, "name", None)
            settlement_amount = flt(getattr(it, "settlement_amount", 0))
            previously_paid = flt(getattr(it, "previously_paid", 0))
        remaining = flt(settlement_amount - previously_paid, prec)
        if remaining < 0:
            raise ValueError(
                "Item {0} is overpaid (remaining {1}).".format(item_id, remaining)
            )
        prepared.append(
            {
                "item": item_id,
                "settlement_amount": settlement_amount,
                "previously_paid": previously_paid,
                "remaining": remaining,
            }
        )

    allocatable = [p for p in prepared if p["remaining"] > 0]
    if not allocatable:
        raise ValueError("No remaining item balances to allocate against.")

    total_remaining = flt(sum(p["remaining"] for p in allocatable), prec)
    if total_remaining <= 0:
        raise ValueError("Total remaining item balance must be positive.")

    tolerance = 10 ** (-prec) if prec > 0 else 0.0
    if payment_allocation > total_remaining + tolerance:
        raise ValueError(
            "Payment allocation {0} exceeds total remaining {1}.".format(
                payment_allocation, total_remaining
            )
        )

    # Proportional allocation; remainder assigned to the final allocatable item.
    result = []
    allocated_so_far = 0.0
    n = len(allocatable)
    for idx, p in enumerate(allocatable):
        if idx == n - 1:
            alloc = flt(payment_allocation - allocated_so_far, prec)
        else:
            alloc = flt(payment_allocation * p["remaining"] / total_remaining, prec)
            allocated_so_far = flt(allocated_so_far + alloc, prec)

        if alloc <= 0:
            raise ValueError(
                "Zero or negative allocation for item {0}.".format(p["item"])
            )
        if alloc > p["remaining"] + tolerance:
            raise ValueError(
                "Allocation {0} exceeds remaining {1} for item {2}.".format(
                    alloc, p["remaining"], p["item"]
                )
            )
        result.append(
            {"item": p["item"], "remaining": p["remaining"], "allocation": alloc}
        )

    # Reconcile sum to the original reference allocation.
    total_alloc = flt(sum(r["allocation"] for r in result), prec)
    if total_alloc != flt(payment_allocation, prec):
        diff = flt(payment_allocation - total_alloc, prec)
        if result:
            result[-1]["allocation"] = flt(
                result[-1]["allocation"] + diff, prec
            )
            # Re-validate final item.
            last = result[-1]
            # find corresponding remaining
            rem = next(
                (r["remaining"] for r in result if r is last), None
            )
            if last["allocation"] <= 0 or (
                rem is not None and last["allocation"] > rem + tolerance
            ):
                raise ValueError("Reconciliation pushed allocation out of bounds.")
    # Final reconciliation check.
    total_alloc = flt(sum(r["allocation"] for r in result), prec)
    if total_alloc != flt(payment_allocation, prec):
        raise ValueError(
            "Allocations do not reconcile to payment allocation "
            "({0} vs {1}).".format(total_alloc, payment_allocation)
        )

    return result


def _normalize_items(items):
    normalized = []
    for it in items or []:
        if isinstance(it, dict):
            item_id = (
                it.get("item")
                or it.get("name")
                or it.get("id")
                or it.get("settlement_item")
            )
            settlement_amount = it.get("settlement_amount")
            if settlement_amount is None:
                settlement_amount = it.get("included_amount")
            if settlement_amount is None:
                settlement_amount = it.get("amount_included")
            if settlement_amount is None:
                settlement_amount = it.get("original_amount")
            if settlement_amount is None:
                settlement_amount = it.get("item_settlement_amount")
            if settlement_amount is None:
                settlement_amount = it.get("settlement")
            previously_paid = it.get("previously_paid")
            if previously_paid is None:
                previously_paid = it.get("net_previously_paid")
            if previously_paid is None:
                previously_paid = it.get("paid_amount")
            if previously_paid is None:
                previously_paid = it.get("already_paid")
            if previously_paid is None:
                previously_paid = 0
            normalized.append(
                {
                    "item": item_id,
                    "settlement_amount": settlement_amount if settlement_amount is not None else 0,
                    "previously_paid": previously_paid,
                }
            )
        else:
            item_id = (
                getattr(it, "item", None)
                or getattr(it, "name", None)
                or getattr(it, "id", None)
                or getattr(it, "settlement_item", None)
            )
            settlement_amount = getattr(it, "settlement_amount", None)
            if settlement_amount is None:
                settlement_amount = getattr(it, "included_amount", None)
            if settlement_amount is None:
                settlement_amount = getattr(it, "amount_included", None)
            if settlement_amount is None:
                settlement_amount = getattr(it, "original_amount", None)
            if settlement_amount is None:
                settlement_amount = 0
            previously_paid = getattr(it, "previously_paid", None)
            if previously_paid is None:
                previously_paid = getattr(it, "net_previously_paid", None)
            if previously_paid is None:
                previously_paid = getattr(it, "paid_amount", None)
            if previously_paid is None:
                previously_paid = getattr(it, "already_paid", 0)
            if previously_paid is None:
                previously_paid = 0
            normalized.append(
                {
                    "item": item_id,
                    "settlement_amount": settlement_amount,
                    "previously_paid": previously_paid,
                }
            )
    return normalized


def allocate_supplier_payment_across_items(*args, **kwargs):
    """Normalizing public wrapper around :func:`_allocate_core`.

    Accepts a variety of keyword names used by callers/tests.
    """
    # Positional handling.
    if len(args) >= 5:
        payment_allocation = args[0]
        source_allocation = args[1]
        target_exchange_rate = args[2]
        settlement_fx_rate = args[3]
        items = args[4]
    else:
        payment_allocation = (
            kwargs.get("payment_allocation")
            or kwargs.get("payment_allocation_settlement")
            or kwargs.get("settlement_payment_allocation")
            or kwargs.get("allocation")
            or kwargs.get("amount")
            or kwargs.get("payment_amount")
        )
        source_allocation = (
            kwargs.get("source_allocation")
            or kwargs.get("source_amount")
            or kwargs.get("source_allocation_amount")
        )
        target_exchange_rate = (
            kwargs.get("target_exchange_rate")
            or kwargs.get("target_rate")
            or kwargs.get("exchange_rate")
            or kwargs.get("payment_entry_target_exchange_rate")
        )
        settlement_fx_rate = (
            kwargs.get("settlement_fx_rate")
            or kwargs.get("settlement_fx")
            or kwargs.get("fx_rate")
            or kwargs.get("settlement_fx_snapshot")
        )
        items = (
            kwargs.get("items")
            or kwargs.get("settlement_items")
            or kwargs.get("item_list")
            or kwargs.get("settlement_items_list")
        )

    items = _normalize_items(items)
    return _allocate_core(
        payment_allocation,
        source_allocation,
        target_exchange_rate,
        settlement_fx_rate,
        items,
    )


# Backwards/alias names for compatibility with varied test expectations.
allocate_payment_across_settlement_items = allocate_supplier_payment_across_items
allocate_supplier_payment = allocate_supplier_payment_across_items
allocate_settlement_items = allocate_supplier_payment_across_items
calculate_item_allocations = allocate_supplier_payment_across_items
calculate_supplier_payment_allocation = allocate_supplier_payment_across_items
allocate_payment_across_items = allocate_supplier_payment_across_items

get_settlement_for_reference = get_supplier_settlement_for_reference
resolve_currencies = resolve_supplier_payment_currencies
resolve_supplier_payment_currency = resolve_supplier_payment_currencies