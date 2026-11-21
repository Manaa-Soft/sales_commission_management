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


def _is_finite(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _is_finite_positive(value):
    return _is_finite(value) and float(value) > 0


def _minor_unit(prec):
    if prec <= 0:
        return 1.0
    return 10 ** (-prec)


def _to_minor(value, prec):
    return int(round(flt(value, prec) / _minor_unit(prec)))


def _from_minor(minor, prec):
    return flt(minor * _minor_unit(prec), prec)


def _largest_remainder_distribution(total_minor, weights, prec):
    """Distribute ``total_minor`` integer units across items proportionally to
    ``weights`` using the largest-remainder method.

    Returns a list of integer minor-unit allocations aligned with ``weights``.
    """
    n = len(weights)
    if n == 0:
        return []
    if total_minor <= 0:
        return [0] * n

    total_weight = sum(w for w in weights)
    if total_weight <= 0:
        raise ValueError("Allocation weights must be positive.")

    # Exact (possibly fractional) shares.
    exact = [total_minor * w / total_weight for w in weights]
    floors = [int(math.floor(x)) for x in exact]
    remainders = [x - floors[i] for i, x in enumerate(exact)]
    allocated = sum(floors)
    leftover = total_minor - allocated

    # Distribute leftover units to items with largest remainders (stable by
    # original index on ties).
    order = sorted(range(n), key=lambda i: (-remainders[i], i))
    bonus = [0] * n
    for k in range(leftover):
        bonus[order[k % n]] += 1

    return [floors[i] + bonus[i] for i in range(n)]


def _pe_references(payment_entry):
    refs = _get_attr(payment_entry, "references", None)
    if refs is None:
        refs = _get_attr(payment_entry, "payment_entry_references", None)
    if refs is None:
        refs = []
    return list(refs)


def get_supplier_settlement_for_reference(payment_entry, payment_reference):
    """Return the single submitted Commission Settlement linked to a supplier
    Payment Entry Reference, or ``None`` when no unambiguous match exists.

    Fail-closed: ambiguous matches (zero or more than one settlement) return
    ``None``. Unrelated supplier payments are ignored.

    The reference must actually belong to the supplied Payment Entry.
    """
    if payment_entry is None or payment_reference is None:
        return None

    pe_name = _get_attr(payment_entry, "name", None)
    if not pe_name:
        return None

    # --- Reference must belong to this Payment Entry. ---
    if _get_attr(payment_reference, "parent", None) != pe_name:
        return None

    # The reference row must exist among the Payment Entry's references.
    refs = _pe_references(payment_entry)
    ref_identity_ok = False
    pr_name = _get_attr(payment_reference, "name", None)
    pr_doctype = _get_attr(payment_reference, "reference_doctype", None)
    pr_refname = _get_attr(payment_reference, "reference_name", None)
    pr_allocated = flt(_get_attr(payment_reference, "allocated_amount", 0))
    if pr_name:
        for r in refs:
            if _get_attr(r, "name", None) == pr_name:
                ref_identity_ok = True
                break
    else:
        for r in refs:
            if (
                _get_attr(r, "reference_doctype", None) == pr_doctype
                and _get_attr(r, "reference_name", None) == pr_refname
                and flt(_get_attr(r, "allocated_amount", 0)) == pr_allocated
            ):
                ref_identity_ok = True
                break
    if not ref_identity_ok and refs:
        return None
    # If no references list is exposed, fall back to parent check already done.

    # --- Payment Entry must be submitted. ---
    if _get_attr(payment_entry, "docstatus", None) != 1:
        return None
    if _get_attr(payment_entry, "payment_type", None) != "Pay":
        return None
    if _get_attr(payment_entry, "party_type", None) != "Supplier":
        return None

    # --- Reference must be a Purchase Invoice with a positive allocation. ---
    if pr_doctype != "Purchase Invoice":
        return None
    pi_name = pr_refname
    if not pi_name:
        return None
    allocated = pr_allocated
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
    account) currency. Item balances (``settlement_amount`` /
    ``amount_included``) are denominated in commission currency and are
    converted to settlement currency using ``settlement_fx_rate``.

    Returns a list of dicts with per-item amounts in all four currencies.
    Only items receiving a positive settlement allocation are included.

    Raises ``ValueError`` on invalid inputs, over-allocation, or
    non-reconciling results.
    """
    prec = _cp()

    # --- Validate scalar financial inputs. ---
    if not _is_finite(payment_allocation):
        raise ValueError("Payment allocation must be finite.")
    payment_allocation = flt(payment_allocation)
    if payment_allocation <= 0:
        raise ValueError("Payment allocation must be positive.")

    if not _is_finite(source_allocation):
        raise ValueError("Source allocation must be finite.")
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

    # --- Prepare items with remaining balances (settlement currency). ---
    prepared = []
    seen_ids = set()
    for it in items:
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
                previously_paid = getattr(it, "already_paid", None)
            if previously_paid is None:
                previously_paid = 0

        if not item_id:
            raise ValueError("Settlement item is missing an identifier.")
        if item_id in seen_ids:
            raise ValueError("Duplicate settlement item identifier: {0}.".format(item_id))
        seen_ids.add(item_id)

        if not _is_finite(settlement_amount):
            raise ValueError(
                "Settlement amount must be finite for item {0}.".format(item_id)
            )
        settlement_amount = flt(settlement_amount)
        if settlement_amount < 0:
            raise ValueError(
                "Settlement amount cannot be negative for item {0}.".format(item_id)
            )

        if not _is_finite(previously_paid):
            raise ValueError(
                "Previously paid amount must be finite for item {0}.".format(item_id)
            )
        previously_paid = flt(previously_paid)
        if previously_paid < 0:
            raise ValueError(
                "Previously paid amount cannot be negative for item {0}.".format(item_id)
            )

        # Item balance is supplied in commission currency; convert to settlement
        # (Purchase Invoice) currency.
        settlement_balance = flt(settlement_amount * fx, prec)
        remaining = flt(settlement_balance - previously_paid, prec)
        if remaining < 0:
            raise ValueError(
                "Item {0} is overpaid (remaining {1}).".format(item_id, remaining)
            )

        prepared.append(
            {
                "item": item_id,
                "commission_balance": settlement_amount,
                "settlement_balance": settlement_balance,
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

    # Over-allocation check at accounting precision (no extra unit allowance).
    if flt(payment_allocation, prec) > flt(total_remaining, prec):
        raise ValueError(
            "Payment allocation {0} exceeds total remaining {1}.".format(
                payment_allocation, total_remaining
            )
        )

    unit = _minor_unit(prec)
    payment_minor = _to_minor(payment_allocation, prec)
    if payment_minor <= 0:
        raise ValueError("Payment allocation is too small to allocate.")

    # Source / commission / company aggregate totals in their own minor units.
    source_minor_total = _to_minor(source_allocation, prec)
    commission_total = flt(payment_allocation / fx, prec)
    commission_minor_total = _to_minor(commission_total, prec)
    company_total = flt(source_allocation * tr, prec)
    company_minor_total = _to_minor(company_total, prec)

    weights = [p["remaining"] for p in allocatable]
    sett_dist = _largest_remainder_distribution(payment_minor, weights, prec)
    source_dist = _largest_remainder_distribution(source_minor_total, weights, prec)
    commission_dist = _largest_remainder_distribution(commission_minor_total, weights, prec)
    company_dist = _largest_remainder_distribution(company_minor_total, weights, prec)

    result = []
    for idx, p in enumerate(allocatable):
        sett_alloc_minor = sett_dist[idx]
        src_alloc_minor = source_dist[idx]
        comm_alloc_minor = commission_dist[idx]
        comp_alloc_minor = company_dist[idx]

        sett_alloc = _from_minor(sett_alloc_minor, prec)
        src_alloc = _from_minor(src_alloc_minor, prec)
        comm_alloc = _from_minor(comm_alloc_minor, prec)
        comp_alloc = _from_minor(comp_alloc_minor, prec)

        if sett_alloc <= 0:
            # Skip zero-share items; they receive no allocation.
            continue
        if sett_alloc > p["remaining"]:
            raise ValueError(
                "Allocation {0} exceeds remaining {1} for item {2}.".format(
                    sett_alloc, p["remaining"], p["item"]
                )
            )

        result.append(
            {
                "item": p["item"],
                "remaining": p["remaining"],
                "allocation": sett_alloc,
                "settlement_amount": sett_alloc,
                "source_amount": src_alloc,
                "commission_amount": comm_alloc,
                "company_amount": comp_alloc,
                # Backwards/alias names.
                "settlement_allocation": sett_alloc,
                "source_allocation": src_alloc,
                "commission_allocation": comm_alloc,
                "company_allocation": comp_alloc,
                "allocated_amount": sett_alloc,
                "allocated_source_amount": src_alloc,
                "allocated_commission_amount": comm_alloc,
                "allocated_company_amount": comp_alloc,
            }
        )

    if not result:
        raise ValueError("No positive allocations produced.")

    # Reconciliation checks for all currencies.
    total_sett = flt(sum(r["allocation"] for r in result), prec)
    total_src = flt(sum(r["source_amount"] for r in result), prec)
    total_comm = flt(sum(r["commission_amount"] for r in result), prec)
    total_comp = flt(sum(r["company_amount"] for r in result), prec)

    if total_sett != flt(payment_allocation, prec):
        raise ValueError(
            "Allocations do not reconcile to payment allocation "
            "({0} vs {1}).".format(total_sett, payment_allocation)
        )
    if total_src != flt(source_allocation, prec):
        raise ValueError(
            "Source allocations do not reconcile ({0} vs {1}).".format(
                total_src, source_allocation
            )
        )
    if total_comm != flt(commission_total, prec):
        raise ValueError(
            "Commission allocations do not reconcile ({0} vs {1}).".format(
                total_comm, commission_total
            )
        )
    if total_comp != flt(company_total, prec):
        raise ValueError(
            "Company allocations do not reconcile ({0} vs {1}).".format(
                total_comp, company_total
            )
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

    if source_allocation is None:
        source_allocation = payment_allocation
    if target_exchange_rate is None:
        target_exchange_rate = 1
    if settlement_fx_rate is None:
        settlement_fx_rate = 1

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
