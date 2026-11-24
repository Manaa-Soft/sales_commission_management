"""Commission recovery position calculation.

This module provides the recovery position engine for P3.4.2.
It calculates the recoverable balance for a commission entry based on
submitted ledger events (original commission, returns, cancellations,
payouts, payout reversals, recoveries, recovery reversals).
"""

import math

import frappe
from frappe import _
from frappe.utils import flt

from sales_commission_management.sales_commission_management.utils import (
    currency_precision as _cp,
)


def get_commission_recovery_position(commission_entry):
    """Calculate the recoverable balance for a commission entry.

    Locks the original Commission Entry, reads all submitted ledger events,
    and computes the recovery position in company currency.

    Args:
        commission_entry: Name of the original Commission Entry.

    Returns:
        dict with recovery position details (see recovery_design.md).

    Raises:
        frappe.ValidationError: If the commission entry is invalid or
            cannot be found, or if over-recovery is detected.
    """
    if not commission_entry:
        frappe.throw(_("Commission Entry is required."))

    if not frappe.db.exists("Commission Entry", commission_entry):
        frappe.throw(
            _("Commission Entry {0} does not exist.").format(commission_entry)
        )

    # Lock the original entry to serialize concurrent access.
    locked = frappe.db.sql(
        "SELECT name, docstatus, company, company_currency, "
        "commission_currency, commission_to_company_rate, earned_commission, "
        "event_type, original_entry, reverses_entry "
        "FROM `tabCommission Entry` WHERE name = %s FOR UPDATE",
        (commission_entry,),
        as_dict=True,
    )
    if not locked:
        frappe.throw(
            _("Could not lock Commission Entry {0}.").format(commission_entry)
        )

    orig = locked[0]
    if orig.docstatus != 1:
        frappe.throw(
            _("Commission Entry {0} must be submitted.").format(commission_entry)
        )

    company = orig.company
    company_currency = orig.company_currency
    if not company or not company_currency:
        frappe.throw(
            _("Commission Entry {0} is missing company or company currency.").format(
                commission_entry
            )
        )

    precision = _cp(company_currency)

    # --- Original commission (converted to company currency) ---
    comm_currency = orig.commission_currency or company_currency
    rate = flt(orig.commission_to_company_rate)
    if rate <= 0:
        rate = 1.0
    original_commission = flt(
        flt(orig.earned_commission) * rate, precision
    )

    # --- Return adjustments (negative clawbacks) ---
    adjustments = frappe.get_all(
        "Commission Entry",
        filters={
            "original_entry": commission_entry,
            "event_type": "Return Adjustment",
            "docstatus": 1,
        },
        fields=[
            "name",
            "earned_commission",
            "commission_currency",
            "company_currency",
            "commission_to_company_rate",
        ],
    )
    adjustment_names = [a.name for a in adjustments]

    # --- Cancellation reversals of Return Adjustments (positive restores) ---
    cancel_of_returns = []
    if adjustment_names:
        cancel_of_returns = frappe.get_all(
            "Commission Entry",
            filters={
                "reverses_entry": ["in", adjustment_names],
                "event_type": "Cancellation Reversal",
                "docstatus": 1,
            },
            fields=[
                "name",
                "earned_commission",
                "commission_currency",
                "company_currency",
                "commission_to_company_rate",
            ],
        )

    # --- Cancellation reversals directly referencing original (negative) ---
    cancel_of_original = frappe.get_all(
        "Commission Entry",
        filters={
            "reverses_entry": commission_entry,
            "event_type": "Cancellation Reversal",
            "docstatus": 1,
        },
        fields=[
            "name",
            "earned_commission",
            "commission_currency",
            "company_currency",
            "commission_to_company_rate",
        ],
    )

    # --- Calculate net entitlement (signed sum in company currency) ---
    net_entitlement_raw = original_commission
    for adj in adjustments:
        adj_rate = flt(adj.commission_to_company_rate) or 1.0
        adj_comm_curr = adj.commission_currency or company_currency
        adj_company = adj.company_currency or company_currency
        # Convert if needed
        if adj_comm_curr != company_currency and adj_rate > 0:
            adj_amount = flt(flt(adj.earned_commission) * adj_rate, precision)
        else:
            adj_amount = flt(adj.earned_commission, precision)
        net_entitlement_raw += adj_amount  # Return adjustments are negative

    for cr in cancel_of_returns:
        cr_rate = flt(cr.commission_to_company_rate) or 1.0
        cr_comm_curr = cr.commission_currency or company_currency
        if cr_comm_curr != company_currency and cr_rate > 0:
            cr_amount = flt(flt(cr.earned_commission) * cr_rate, precision)
        else:
            cr_amount = flt(cr.earned_commission, precision)
        net_entitlement_raw += cr_amount  # Restores (positive)

    for co in cancel_of_original:
        co_rate = flt(co.commission_to_company_rate) or 1.0
        co_comm_curr = co.commission_currency or company_currency
        if co_comm_curr != company_currency and co_rate > 0:
            co_amount = flt(flt(co.earned_commission) * co_rate, precision)
        else:
            co_amount = flt(co.earned_commission, precision)
        net_entitlement_raw += co_amount  # Full clawback (negative)

    net_entitlement = flt(max(0.0, net_entitlement_raw), precision)

    # --- Payout events ---
    payout_events = frappe.get_all(
        "Commission Payout Event",
        filters={
            "commission_entry": commission_entry,
            "event_type": "Payout",
            "docstatus": 1,
        },
        fields=[
            "name",
            "posting_date",
            "company_amount",
            "company_currency",
        ],
        order_by="posting_date asc, name asc",
    )

    payout_reversals = frappe.get_all(
        "Commission Payout Event",
        filters={
            "commission_entry": commission_entry,
            "event_type": "Payout Reversal",
            "docstatus": 1,
        },
        fields=[
            "name",
            "reversal_of",
            "company_amount",
            "company_currency",
        ],
    )

    # Build reversal map by original payout event
    reversal_map = {}
    for pr in payout_reversals:
        reversal_map.setdefault(pr.reversal_of, []).append(pr)

    # Calculate net payout per event
    net_paid = 0.0
    payout_allocations = []
    for pe in payout_events:
        pe_amount = flt(pe.company_amount, precision)
        # Subtract payout reversals for this event
        pe_reversals = reversal_map.get(pe.name, [])
        reversal_total = flt(
            sum(flt(pr.company_amount, precision) for pr in pe_reversals),
            precision,
        )
        net_pe = flt(pe_amount - reversal_total, precision)
        net_paid = flt(net_paid + net_pe, precision)

        payout_allocations.append({
            "payout_event": pe.name,
            "posting_date": pe.posting_date,
            "net_payout": net_pe,
            "recovered": 0.0,
            "recoverable_balance": 0.0,
        })

    # --- Recovery events ---
    recovery_events = frappe.get_all(
        "Commission Payout Event",
        filters={
            "commission_entry": commission_entry,
            "event_type": "Recovery",
            "docstatus": 1,
        },
        fields=["name", "company_amount", "company_currency"],
    )

    recovery_reversals = frappe.get_all(
        "Commission Payout Event",
        filters={
            "commission_entry": commission_entry,
            "event_type": "Recovery Reversal",
            "docstatus": 1,
        },
        fields=["name", "reversal_of", "company_amount", "company_currency"],
    )

    net_recovered = flt(
        sum(flt(r.company_amount, precision) for r in recovery_events)
        - sum(flt(rr.company_amount, precision) for rr in recovery_reversals),
        precision,
    )

    # --- Calculate recovery obligation ---
    recovery_due_before_recovery = flt(
        max(0.0, net_paid - net_entitlement), precision
    )
    outstanding_recovery = flt(
        max(0.0, recovery_due_before_recovery - net_recovered), precision
    )

    # Over-recovery check (on normalized values, no tolerance after normalization)
    if net_recovered > recovery_due_before_recovery:
        frappe.throw(
            _("Over-recovery detected for Commission Entry {0}: "
              "net_paid={1}, net_entitlement={2}, net_recovered={3}, "
              "recovery_due={4}.").format(
                commission_entry,
                net_paid,
                net_entitlement,
                net_recovered,
                recovery_due_before_recovery,
            )
        )

    # --- Allocate against payouts (oldest first) ---
    remaining_recovery_due = recovery_due_before_recovery
    remaining_net_recovered = net_recovered

    for alloc in payout_allocations:
        net_pe = alloc["net_payout"]
        # due_i = min(net_payout_i, remaining_recovery_due)
        due_i = flt(min(net_pe, remaining_recovery_due), precision)
        remaining_recovery_due = flt(remaining_recovery_due - due_i, precision)

        # recovered_i = min(due_i, remaining_net_recovered)
        recovered_i = flt(min(due_i, remaining_net_recovered), precision)
        remaining_net_recovered = flt(remaining_net_recovered - recovered_i, precision)

        alloc["recovered"] = recovered_i
        alloc["recoverable_balance"] = flt(due_i - recovered_i, precision)

    # --- Return position ---
    return {
        "commission_entry": commission_entry,
        "currency": company_currency,
        "original_commission": original_commission,
        "net_entitlement": net_entitlement,
        "net_paid": net_paid,
        "net_recovered": net_recovered,
        "recovery_due_before_recovery": recovery_due_before_recovery,
        "outstanding_recovery": outstanding_recovery,
        "payout_events": [pe.name for pe in payout_events],
        "recovery_events": [re.name for re in recovery_events],
        "return_adjustments": adjustment_names,
        "cancellation_reversals": (
            [cr.name for cr in cancel_of_returns]
            + [co.name for co in cancel_of_original]
        ),
        "payout_allocations": payout_allocations,
    }
