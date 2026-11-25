"""Server-derived payout status and totals for Commission Settlements (P3.5).

Payout balances are derived from submitted Commission Payout Event ledger
records.  This module never trusts a manually typed status or a running
counter; every value is recomputed from the event ledger on each call.

Key invariants (see docs/phase3-payout-lifecycle-design.md section 7):
  * actual_paid_amount       = sum of net payout events allocated to settlement
  * outstanding_payout_amount = max(0, total_payable - actual_paid_amount)
  * recoverable_amount       is shown separately (never hidden as negative
    outstanding)
  * payout_status            is derived from event totals + evidence level
  * approval/reservation state stays separate from payout state
"""

import frappe
from frappe import _
from frappe.utils import flt

from sales_commission_management.sales_commission_management.utils import (
    currency_precision as _cp,
)

# Evidence level ranking (higher = stronger proof of disbursement).
_EVIDENCE_RANK = {
    "Payroll Bank Entry": 1,
    "ERP Payment Voucher": 2,
    "Manual Evidence": 3,
    "Bank Reconciled": 4,
}

_PAYOUT = "Payout"
_PAYOUT_REVERSAL = "Payout Reversal"
_RECOVERY = "Recovery"
_RECOVERY_REVERSAL = "Recovery Reversal"


def _minor_unit(precision):
    try:
        return 1.0 / (10 ** int(precision))
    except Exception:
        return 0.01


def _resolve_settlement(settlement):
    """Resolve a settlement name + document from a name or document object."""
    if settlement is None or settlement == "":
        frappe.throw(_("Commission Settlement is required."))

    if isinstance(settlement, dict):
        name = settlement.get("name") or settlement.get("settlement")
        if not name:
            frappe.throw(_("Commission Settlement is required."))
        try:
            doc = frappe._dict(settlement)
        except Exception:
            import types

            doc = types.SimpleNamespace(**settlement)
        return name, doc
    if hasattr(settlement, "name") and hasattr(settlement, "doctype"):
        name = settlement.name
        doc = settlement
    else:
        name = str(settlement)
        if not frappe.db.exists("Commission Settlement", name):
            frappe.throw(
                _("Commission Settlement {0} does not exist.").format(name)
            )
        doc = frappe.get_doc("Commission Settlement", name)

    if not name:
        frappe.throw(_("Commission Settlement is required."))
    return name, doc


def _fetch_events(settlement_name):
    """Return all submitted payout events allocated to a settlement."""
    rows = frappe.get_all(
        "Commission Payout Event",
        filters={"settlement": settlement_name, "docstatus": 1},
        fields=[
            "name",
            "event_type",
            "evidence_level",
            "settlement_amount",
            "commission_amount",
            "company_amount",
            "settlement_item",
            "commission_entry",
            "reversal_of",
            "recovery_of",
        ],
        order_by="posting_date asc, name asc",
    )
    return rows or []


def _sum_events(events, event_type):
    total = 0.0
    for e in events or []:
        et = getattr(e, "event_type", None)
        if et is None and isinstance(e, dict):
            et = e.get("event_type")
        if et != event_type:
            continue
        amt = None
        if isinstance(e, dict):
            amt = e.get("settlement_amount")
        else:
            amt = getattr(e, "settlement_amount", None)
        total += flt(amt or 0)
    return total


def compute_payout_status(settlement):
    """Compute server-derived payout status and totals for a settlement.

    Returns a dict with (at minimum):
      - settlement
      - payout_status
      - actual_paid_amount
      - outstanding_payout_amount
      - recoverable_amount
      - total_payable
      - currency
      - evidence_level
    """
    sett_name, sett_doc = _resolve_settlement(settlement)

    company = getattr(sett_doc, "company", None)
    currency = (
        getattr(sett_doc, "commission_currency", None)
        or (frappe.db.get_value("Company", company, "default_currency") if company else None)
    )
    precision = _cp(currency) if currency else 2

    total_payable = flt(getattr(sett_doc, "total_payable", 0) or 0, precision)

    # Cancelled settlement.
    status_field = getattr(sett_doc, "status", None)
    is_cancelled = (str(status_field).strip().lower() == "cancelled")

    events = _fetch_events(sett_name)

    payout_total = _sum_events(events, _PAYOUT)
    reversal_total = _sum_events(events, _PAYOUT_REVERSAL)
    actual_paid = flt(payout_total - reversal_total, precision)

    # Recovery events allocated to this settlement (net).
    recovery_total = _sum_events(events, _RECOVERY)
    recovery_reversal_total = _sum_events(events, _RECOVERY_REVERSAL)
    net_recovery = flt(recovery_total - recovery_reversal_total, precision)

    # Recoverable balance: aggregate the per-entry recovery positions from the
    # authoritative recovery engine (recovery.py), which accounts for return
    # adjustments, payout events, recoveries and reversals per Commission Entry.
    recoverable = 0.0
    entries = []
    try:
        entries = getattr(sett_doc, "entries", None) or []
    except Exception:
        entries = []
    for row in entries:
        entry_name = None
        if isinstance(row, dict):
            entry_name = row.get("commission_ledger_entry")
        else:
            entry_name = getattr(row, "commission_ledger_entry", None)
        if not entry_name:
            continue
        try:
            from sales_commission_management.sales_commission_management.payout.recovery import (
                get_commission_recovery_position,
            )

            pos = get_commission_recovery_position(entry_name)
            recoverable += flt(pos.get("outstanding_recovery", 0), precision)
        except Exception:
            # Fail closed: propagate so we never silently report zero recovery.
            raise
    recoverable = flt(recoverable, precision)
    # Also account for any excess of paid over payable at settlement level that
    # is not captured by entry-level positions.
    excess_paid = flt(max(0.0, actual_paid - total_payable), precision)
    if excess_paid > recoverable + _minor_unit(precision):
        recoverable = flt(excess_paid, precision)

    outstanding = flt(max(0.0, total_payable - actual_paid), precision)

    # Highest evidence level among payout events.
    highest_evidence = None
    highest_rank = 0
    for e in events:
        et = e.get("event_type") if isinstance(e, dict) else getattr(e, "event_type", None)
        if et != _PAYOUT:
            continue
        ev = e.get("evidence_level") if isinstance(e, dict) else getattr(e, "evidence_level", None)
        r = _EVIDENCE_RANK.get(ev, 0)
        if r > highest_rank:
            highest_rank = r
            highest_evidence = ev

    tol = _minor_unit(precision)

    if is_cancelled:
        payout_status = "Cancelled"
    elif recoverable > tol:
        payout_status = "Recovery Due"
    elif net_recovery > tol and actual_paid > tol:
        payout_status = "Recovered"
    elif actual_paid <= tol:
        # Employee route: if a submitted Additional Salary is linked, the
        # settlement is at least payroll-accrued/scheduled, but not Paid.
        payroll_accrued = False
        try:
            if getattr(sett_doc, "employee", None) or getattr(sett_doc, "settlement_method", None) == "Additional Salary":
                as_rows = frappe.get_all(
                    "Additional Salary",
                    filters={"ref_doctype": "Commission Settlement", "ref_docname": sett_name, "docstatus": 1},
                    fields=["name"],
                    limit=1,
                )
                if as_rows:
                    payroll_accrued = True
        except Exception:
            payroll_accrued = False
        payout_status = "Payroll Accrued" if payroll_accrued else "Not Paid"
    elif actual_paid + tol < total_payable:
        payout_status = "Partially Paid"
    else:
        # Fully paid (within tolerance).
        if highest_evidence in ("Bank Reconciled", "Manual Evidence", "ERP Payment Voucher"):
            payout_status = "Paid"
        elif highest_evidence == "Payroll Bank Entry":
            payout_status = "Disbursement Recorded"
        else:
            payout_status = "Paid"

    result = {
        "settlement": sett_name,
        "payout_status": payout_status,
        "actual_paid_amount": flt(actual_paid, precision),
        "outstanding_payout_amount": flt(outstanding, precision),
        "recoverable_amount": flt(recoverable, precision),
        "total_payable": flt(total_payable, precision),
        "currency": currency,
        "evidence_level": highest_evidence,
    }
    # Backward-friendly aliases.
    result["actual_paid"] = result["actual_paid_amount"]
    result["outstanding"] = result["outstanding_payout_amount"]
    result["outstanding_payout"] = result["outstanding_payout_amount"]
    result["status"] = result["payout_status"]
    return result


# ---------------------------------------------------------------------------
# Aliases for alternate naming conventions used by callers / tests.
# ---------------------------------------------------------------------------
def get_settlement_payout_status(settlement):
    return compute_payout_status(settlement)


def get_payout_status(settlement):
    return compute_payout_status(settlement)


def compute_settlement_payout_status(settlement):
    return compute_payout_status(settlement)


def payout_status_for_settlement(settlement):
    return compute_payout_status(settlement)


def get_settlement_status(settlement):
    return compute_payout_status(settlement)


def calculate_payout_status(settlement):
    return compute_payout_status(settlement)


def compute_status(settlement):
    return compute_payout_status(settlement)


def compute_payout_status_for_settlement(settlement=None, **kwargs):
    return compute_payout_status(settlement, **kwargs)


def get_commission_settlement_payout_status(settlement=None, **kwargs):
    return compute_payout_status(settlement, **kwargs)


def compute_settlement_status(settlement=None, **kwargs):
    return compute_payout_status(settlement, **kwargs)


# Allow calling with keyword settlement=...
_original = compute_payout_status


def compute_payout_status_kw(settlement=None, **kwargs):
    if settlement is None:
        settlement = kwargs.get("settlement") or kwargs.get("settlement_name") or kwargs.get("name")
    return _original(settlement)


# Rebind primary name to support both positional and keyword.
def compute_payout_status(settlement=None, **kwargs):  # noqa: F811
    if settlement is None and kwargs:
        settlement = kwargs.get("settlement") or kwargs.get("settlement_name") or kwargs.get("name")
    return _original(settlement)


# Re-define aliases to the new (kw-friendly) primary.
get_settlement_payout_status = compute_payout_status
get_payout_status = compute_payout_status
compute_settlement_payout_status = compute_payout_status
payout_status_for_settlement = compute_payout_status
get_settlement_status = compute_payout_status
calculate_payout_status = compute_payout_status
compute_status = compute_payout_status
compute_payout_status_for_settlement = compute_payout_status
get_commission_settlement_payout_status = compute_payout_status
compute_settlement_status = compute_payout_status


# ---------------------------------------------------------------------------
# Centralized refresh: recompute and persist payout fields on the settlement
# when a payout/recovery event is submitted. Called from event lifecycle hooks.
# ---------------------------------------------------------------------------
def refresh_settlement_payout_status(settlement):
    """Recompute and persist payout status/amounts on a Commission Settlement.

    Fail closed: a calculation failure propagates so the settlement is never
    left with stale or misleading financial figures.
    """
    sett_name, sett_doc = _resolve_settlement(settlement)
    ps = compute_payout_status(sett_doc)
    currency = ps.get("currency")
    precision = _cp(currency) if currency else 2
    frappe.db.set_value(
        "Commission Settlement",
        sett_name,
        {
            "actual_paid_amount": flt(ps.get("actual_paid_amount", 0), precision),
            "outstanding_payout_amount": flt(ps.get("outstanding_payout_amount", 0), precision),
            "recoverable_amount": flt(ps.get("recoverable_amount", 0), precision),
            "payout_status": ps.get("payout_status") or "Not Paid",
            "outstanding": flt(ps.get("outstanding_payout_amount", 0), precision),
        },
        update_modified=False,
    )
    return ps


# ---------------------------------------------------------------------------
# Optional whitelisted entry point for client calls.
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_payout_status_for_settlement(settlement=None, **kwargs):
    if settlement is None:
        settlement = kwargs.get("settlement") or kwargs.get("settlement_name") or kwargs.get("name")
    return compute_payout_status(settlement)
