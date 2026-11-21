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
    # Fail closed when the reference collection is missing or empty.
    refs = _pe_references(payment_entry)
    if not refs:
        return None

    ref_identity_ok = False
    pr_name = _get_attr(payment_reference, "name", None)
    pr_doctype = _get_attr(payment_reference, "reference_doctype", None)
    pr_refname = _get_attr(payment_reference, "reference_name", None)
    pr_allocated = flt(_get_attr(payment_reference, "allocated_amount", 0))

    if pr_name:
        for r in refs:
            if _get_attr(r, "name", None) == pr_name:
                # The supplied row must also agree with the actual child row's
                # doctype, reference name, and allocated amount.
                if (
                    _get_attr(r, "reference_doctype", None) == pr_doctype
                    and _get_attr(r, "reference_name", None) == pr_refname
                    and flt(_get_attr(r, "allocated_amount", 0)) == pr_allocated
                ):
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

    if not ref_identity_ok:
        return None
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

    # Only items that receive a positive settlement allocation are eligible
    # for the other currency distributions. Distributing independently across
    # all items would let zero-settlement items silently drop their share of
    # the other currencies.
    eligible_indices = [i for i, v in enumerate(sett_dist) if v > 0]
    if not eligible_indices:
        raise ValueError("No positive settlement allocations produced.")

    eligible_weights = [allocatable[i]["remaining"] for i in eligible_indices]
    source_dist = _largest_remainder_distribution(
        source_minor_total, eligible_weights, prec
    )
    commission_dist = _largest_remainder_distribution(
        commission_minor_total, eligible_weights, prec
    )
    company_dist = _largest_remainder_distribution(
        company_minor_total, eligible_weights, prec
    )

    result = []
    for k, i in enumerate(eligible_indices):
        p = allocatable[i]
        sett_alloc_minor = sett_dist[i]
        src_alloc_minor = source_dist[k]
        comm_alloc_minor = commission_dist[k]
        comp_alloc_minor = company_dist[k]

        sett_alloc = _from_minor(sett_alloc_minor, prec)
        src_alloc = _from_minor(src_alloc_minor, prec)
        comm_alloc = _from_minor(comm_alloc_minor, prec)
        comp_alloc = _from_minor(comp_alloc_minor, prec)

        if sett_alloc <= 0:
            continue
        # Reject allocations that cannot be represented at the configured
        # commission precision: a positive settlement allocation must not
        # produce a zero commission allocation (P3.2.2 zero-commission guard).
        if sett_alloc_minor > 0 and comm_alloc_minor <= 0:
            raise ValueError(
                "Commission allocation for item {0} cannot be represented "
                "at the configured precision (zero commission amount).".format(
                    p["item"]
                )
            )
        if comm_alloc <= 0 and sett_alloc > 0:
            raise ValueError(
                "Commission allocation for item {0} must be positive.".format(
                    p["item"]
                )
            )
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
        payment_allocation = kwargs.get("payment_allocation")
        if payment_allocation is None:
            payment_allocation = kwargs.get("payment_allocation_settlement")
        if payment_allocation is None:
            payment_allocation = kwargs.get("settlement_payment_allocation")
        if payment_allocation is None:
            payment_allocation = kwargs.get("allocation")
        if payment_allocation is None:
            payment_allocation = kwargs.get("amount")
        if payment_allocation is None:
            payment_allocation = kwargs.get("payment_amount")

        source_allocation = kwargs.get("source_allocation")
        if source_allocation is None:
            source_allocation = kwargs.get("source_amount")
        if source_allocation is None:
            source_allocation = kwargs.get("source_allocation_amount")

        target_exchange_rate = kwargs.get("target_exchange_rate")
        if target_exchange_rate is None:
            target_exchange_rate = kwargs.get("target_rate")
        if target_exchange_rate is None:
            target_exchange_rate = kwargs.get("exchange_rate")
        if target_exchange_rate is None:
            target_exchange_rate = kwargs.get("payment_entry_target_exchange_rate")

        settlement_fx_rate = kwargs.get("settlement_fx_rate")
        if settlement_fx_rate is None:
            settlement_fx_rate = kwargs.get("settlement_fx")
        if settlement_fx_rate is None:
            settlement_fx_rate = kwargs.get("fx_rate")
        if settlement_fx_rate is None:
            settlement_fx_rate = kwargs.get("settlement_fx_snapshot")

        items = kwargs.get("items")
        if items is None:
            items = kwargs.get("settlement_items")
        if items is None:
            items = kwargs.get("item_list")
        if items is None:
            items = kwargs.get("settlement_items_list")

    # Do not silently substitute missing financial inputs. Missing source
    # allocation or exchange rates are rejected downstream.
    if source_allocation is None:
        raise ValueError("Source allocation is required.")
    if target_exchange_rate is None:
        raise ValueError("Target exchange rate is required.")
    if settlement_fx_rate is None:
        raise ValueError("Settlement FX rate is required.")

    items = _normalize_items(items)
    return _allocate_core(
        payment_allocation,
        source_allocation,
        target_exchange_rate,
        settlement_fx_rate,
        items,
    )


# ---------------------------------------------------------------------------
# P3.2.2 — Supplier Payment Entry handler
# ---------------------------------------------------------------------------


def _previously_paid_for_item(settlement_item_name):
    """Derive the net previously paid amount (in settlement currency) for a
    settlement item from submitted Payout events, including reversals.

    Commission Entry.settled_amount and Commission Settlement Item.
    already_settled_amount are denominated in commission currency and must NOT
    be passed directly into the allocator's ``previously_paid`` (which is
    settlement currency). Instead, derive from the net submitted payout-event
    settlement_amount values.
    """
    if not settlement_item_name:
        return 0.0
    # Propagate database errors; only return 0.0 when the query succeeds and
    # no payout events exist.
    rows = frappe.get_all(
        "Commission Payout Event",
        filters={
            "settlement_item": settlement_item_name,
            "docstatus": 1,
        },
        fields=["event_type", "settlement_amount"],
    )
    paid = 0.0
    for r in rows or []:
        et = _get_attr(r, "event_type", None)
        amt = flt(_get_attr(r, "settlement_amount", 0))
        if et == "Payout":
            paid += amt
        elif et == "Payout Reversal":
            paid -= amt
    # A negative net payout balance indicates a reversal inconsistency or
    # ledger problem. Reject it beyond the configured currency precision
    # instead of silently clamping to zero.
    from sales_commission_management.sales_commission_management.utils import (
        currency_precision as _cp_prev,
    )

    _prec = _cp_prev()
    _minor = _minor_unit(_prec)
    if paid < 0 and abs(paid) > _minor:
        frappe.throw(
            _("Negative net payout balance for settlement item {0}: {1}.").format(
                settlement_item_name, paid
            )
        )
    if paid < 0:
        paid = 0.0
    return flt(paid, _prec)


def _get_settlement_items(settlement):
    """Return Commission Settlement Item rows for a settlement."""
    sett_name = _get_attr(settlement, "name", None)
    if not sett_name:
        return []
    # Propagate database errors instead of silently returning an empty list.
    return frappe.get_all(
        "Commission Settlement Item",
        filters={"parent": sett_name},
        fields=[
            "name",
            "commission_ledger_entry",
            "amount_included",
            "already_settled_amount",
        ],
    ) or []


def process_supplier_payment_entry(payment_entry):
    """Process a submitted Pay / Supplier Payment Entry and generate one
    Commission Payout Event per eligible settlement item.

    Steps:
      1. Validate the Payment Entry is a submitted supplier Pay.
      2. For each reference, match to a single submitted Commission Settlement.
      3. Resolve currencies.
      4. Lock the Payment Entry Reference row (concurrency protection).
      5. Skip references already fully allocated (idempotency).
      6. Derive per-item previous payments from submitted events.
      7. Allocate across settlement items.
      8. Create + submit one payout event per allocation.
      9. Reconcile aggregate totals against the reference allocation.

    All work happens in the surrounding Payment Entry transaction; any
    failure propagates so the transaction cannot leave a partial record.

    Returns a list of created/submitted event names.
    """
    if isinstance(payment_entry, str):
        pe = frappe.get_doc("Payment Entry", payment_entry)
    else:
        pe = payment_entry

    if pe is None:
        return []

    # Only genuine supplier Pay entries are processed. Customer receipts are
    # handled by the separate collection hooks.
    if _get_attr(pe, "docstatus", 0) != 1:
        return []
    if _get_attr(pe, "payment_type", None) not in (None, "Pay"):
        return []
    if _get_attr(pe, "party_type", None) not in (None, "Supplier"):
        return []

    references = _pe_references(pe)
    if not references:
        return []

    if generate_event_key is None or create_and_submit_payout_event is None:
        frappe.throw(_("Payout event factory is unavailable."))

    created_events = []

    for pr in references:
        ref_name = _get_attr(pr, "name", None)
        # --- Concurrency protection: lock the Payment Entry Reference row. ---
        if not ref_name:
            frappe.throw(_("Payment Entry Reference row name is required."))
        # Lock must be acquired before checking existing events and calculating
        # previous payments. Propagate any locking failure so the transaction
        # rolls back; never continue without the lock.
        lock_rows = frappe.db.sql(
            "SELECT parent FROM `tabPayment Entry Reference` WHERE name = %s FOR UPDATE",
            (ref_name,),
        )
        if not lock_rows:
            frappe.throw(
                _("Payment Entry Reference {0} not found.").format(ref_name)
            )
        locked_parent = lock_rows[0][0] if isinstance(lock_rows[0], (tuple, list)) else lock_rows[0]
        if locked_parent != _get_attr(pe, "name", None):
            frappe.throw(
                _("Payment Entry Reference {0} does not belong to this Payment Entry.").format(
                    ref_name
                )
            )

        allocated_amount = flt(_get_attr(pr, "allocated_amount", 0))
        if allocated_amount <= 0:
            continue

        settlement = get_supplier_settlement_for_reference(pe, pr)
        if settlement is None:
            continue

        resolved = resolve_supplier_payment_currencies(pe, pr)

        # --- Idempotency: validate existing events before skipping. ---
        # Narrow filters to the full source identity and event type.
        try:
            existing_rows = frappe.get_all(
                "Commission Payout Event",
                filters={
                    "source_doctype": "Payment Entry",
                    "source_name": _get_attr(pe, "name", None),
                    "source_row": ref_name,
                    "docstatus": 1,
                },
                fields=[
                    "event_type",
                    "source_amount",
                    "settlement_amount",
                    "commission_amount",
                    "company_amount",
                ],
            )
        except Exception as exc:
            frappe.throw(
                _("Failed to look up existing payout events: {0}").format(str(exc))
            )
        # Net totals in all four currencies, accounting for reversals.
        net_src = 0.0
        net_sett = 0.0
        net_comm = 0.0
        net_comp = 0.0
        has_existing = bool(existing_rows)
        for r in existing_rows or []:
            et = _get_attr(r, "event_type", None)
            sign = 1.0 if et == "Payout" else (-1.0 if et == "Payout Reversal" else 0.0)
            net_src += sign * flt(_get_attr(r, "source_amount", 0))
            net_sett += sign * flt(_get_attr(r, "settlement_amount", 0))
            net_comm += sign * flt(_get_attr(r, "commission_amount", 0))
            net_comp += sign * flt(_get_attr(r, "company_amount", 0))

        from sales_commission_management.sales_commission_management.utils import (
            currency_precision as _cp_id,
        )

        psrc = _cp_id(resolved["source_currency"])
        psett = _cp_id(resolved["settlement_currency"])
        pcomm = _cp_id(resolved["commission_currency"])
        pcomp = _cp_id(resolved["company_currency"])

        if has_existing:
            fully_reconciles = (
                flt(net_src, psrc) == flt(resolved["source_amount"], psrc)
                and flt(net_sett, psett) == flt(resolved["settlement_amount"], psett)
                and flt(net_comm, pcomm) == flt(resolved["commission_amount"], pcomm)
                and flt(net_comp, pcomp) == flt(resolved["company_amount"], pcomp)
            )
            if fully_reconciles:
                # Idempotent: already fully processed.
                continue
            # Partial or inconsistent existing events: fail closed.
            frappe.throw(
                _("Existing payout events for reference {0} do not fully reconcile; "
                  "refusing to process partially.").format(ref_name)
            )

        # Build settlement items with derived previous payments.
        sett_items = _get_settlement_items(settlement)
        if not sett_items:
            frappe.throw(
                _("Commission Settlement {0} has no items.").format(
                    _get_attr(settlement, "name", None)
                )
            )
        items = []
        item_to_entry = {}
        for si in sett_items:
            si_name = _get_attr(si, "name", None)
            ce_name = _get_attr(si, "commission_ledger_entry", None)
            if not si_name or not ce_name:
                continue
            comm_balance = flt(_get_attr(si, "amount_included", 0))
            prev_paid = _previously_paid_for_item(si_name)
            items.append(
                {
                    "item": si_name,
                    "settlement_amount": comm_balance,
                    "previously_paid": prev_paid,
                }
            )
            item_to_entry[si_name] = ce_name

        if not items:
            frappe.throw(
                _("Commission Settlement {0} has no eligible items.").format(
                    _get_attr(settlement, "name", None)
                )
            )

        allocations = allocate_supplier_payment_across_items(
            payment_allocation=resolved["settlement_amount"],
            source_allocation=resolved["source_amount"],
            target_exchange_rate=resolved["target_exchange_rate"],
            settlement_fx_rate=resolved["settlement_fx_rate"],
            items=items,
        )

        # Create + submit one event per allocation.
        ref_event_names = []
        for alloc in allocations:
            item_name = alloc["item"]
            ce_name = item_to_entry.get(item_name)
            if not ce_name:
                continue
            event_key = generate_event_key(
                "Payment Entry",
                _get_attr(pe, "name", None),
                ref_name,
                _get_attr(settlement, "name", None),
                item_name,
                ce_name,
                "Payout",
            )
            sett_doc = _get_attr(settlement, "settlement_document", None)
            payload = {
                "event_key": event_key,
                "event_type": "Payout",
                "posting_date": _get_attr(pe, "posting_date", None) or frappe.utils.nowdate(),
                "evidence_level": "ERP Payment Voucher",
                "settlement": _get_attr(settlement, "name", None),
                "settlement_item": item_name,
                "commission_entry": ce_name,
                "company": resolved["company_currency"] and (_get_attr(pe, "company", None) or _get_attr(settlement, "company", None)),
                "beneficiary_type": _get_attr(settlement, "beneficiary_type", None),
                "beneficiary": _get_attr(settlement, "sales_person", None) or _get_attr(settlement, "sales_partner", None),
                "source_doctype": "Payment Entry",
                "source_name": _get_attr(pe, "name", None),
                "source_row": ref_name,
                "source_currency": resolved["source_currency"],
                "source_amount": alloc["source_amount"],
                "settlement_currency": resolved["settlement_currency"],
                "settlement_amount": alloc["settlement_amount"],
                "commission_currency": resolved["commission_currency"],
                "commission_amount": alloc["commission_amount"],
                "company_currency": resolved["company_currency"],
                "company_amount": alloc["company_amount"],
                "fx_from_currency": resolved["commission_currency"],
                "fx_to_currency": resolved["settlement_currency"],
                "fx_rate": resolved["settlement_fx_rate"],
                "fx_date": _get_attr(settlement, "settlement_fx_date", None)
                or _get_attr(pe, "posting_date", None)
                or frappe.utils.nowdate(),
                "fx_source": "Commission Settlement",
            }
            # Remove None-valued optional? keep; factory validates required.
            name = create_and_submit_payout_event(payload)
            ref_event_names.append(name)
            created_events.append(name)

        # --- Aggregate reconciliation for this reference. ---
        # Verify submitted event totals match the resolved reference values.
        try:
            ev_rows = frappe.get_all(
                "Commission Payout Event",
                filters={
                    "source_doctype": "Payment Entry",
                    "source_name": _get_attr(pe, "name", None),
                    "source_row": ref_name,
                    "docstatus": 1,
                },
                fields=[
                    "source_amount",
                    "settlement_amount",
                    "commission_amount",
                    "company_amount",
                ],
            )
            # Use source currency precision.
            from sales_commission_management.sales_commission_management.utils import currency_precision as _cp4
            psrc = _cp4(resolved["source_currency"])
            psett = _cp4(resolved["settlement_currency"])
            pcomm = _cp4(resolved["commission_currency"])
            pcomp = _cp4(resolved["company_currency"])
            total_src = flt(sum(flt(_get_attr(r, "source_amount", 0)) for r in (ev_rows or [])), psrc)
            total_sett = flt(sum(flt(_get_attr(r, "settlement_amount", 0)) for r in (ev_rows or [])), psett)
            total_comm = flt(sum(flt(_get_attr(r, "commission_amount", 0)) for r in (ev_rows or [])), pcomm)
            total_comp = flt(sum(flt(_get_attr(r, "company_amount", 0)) for r in (ev_rows or [])), pcomp)
            if total_src != flt(resolved["source_amount"], psrc):
                frappe.throw(
                    _("Aggregate source total {0} does not match reference {1}.").format(
                        total_src, resolved["source_amount"]
                    )
                )
            if total_sett != flt(resolved["settlement_amount"], psett):
                frappe.throw(
                    _("Aggregate settlement total {0} does not match reference {1}.").format(
                        total_sett, resolved["settlement_amount"]
                    )
                )
            if total_comm != flt(resolved["commission_amount"], pcomm):
                frappe.throw(
                    _("Aggregate commission total {0} does not match reference {1}.").format(
                        total_comm, resolved["commission_amount"]
                    )
                )
            if total_comp != flt(resolved["company_amount"], pcomp):
                frappe.throw(
                    _("Aggregate company total {0} does not match reference {1}.").format(
                        total_comp, resolved["company_amount"]
                    )
                )
        except frappe.ValidationError:
            raise
        except Exception as exc:
            frappe.throw(_("Reconciliation failed: {0}").format(str(exc)))

    return created_events


# Backwards/alias names for supplier payment processing.
process_supplier_payment = process_supplier_payment_entry
handle_supplier_payment_entry = process_supplier_payment_entry
process_payment_entry_supplier = process_supplier_payment_entry


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
