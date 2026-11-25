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
        if not frappe.db.exists("Commission Settlement", name):
            frappe.throw(
                _("Commission Settlement {0} does not exist.").format(name)
            )
        try:
            doc = frappe._dict(settlement)
        except Exception:
            import types

            doc = types.SimpleNamespace(**settlement)
        return name, doc
    if hasattr(settlement, "name") and hasattr(settlement, "doctype"):
        name = settlement.name
        if not frappe.db.exists("Commission Settlement", name):
            frappe.throw(
                _("Commission Settlement {0} does not exist.").format(name)
            )
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
# Privileged roles that may read any settlement's payout status.
# ---------------------------------------------------------------------------
_PRIVILEGED_ROLES = {"System Manager", "Accounts Manager", "Sales Manager"}


def _is_privileged_user():
    try:
        return bool(set(frappe.get_roles(frappe.session.user)) & _PRIVILEGED_ROLES)
    except Exception:
        return False


def _enforce_settlement_read_access(sett_name, sett_doc):
    """Enforce read access for a Commission Settlement's payout status.

    Guest users are denied. Non-privileged users may only read settlements
    whose beneficiary is linked to their account.
    """
    if frappe.session.user == "Guest":
        frappe.throw(_("Authentication required."), frappe.PermissionError)

    if _is_privileged_user():
        return

    beneficiary_type = (
        getattr(sett_doc, "beneficiary_type", None)
        or (sett_doc.get("beneficiary_type") if isinstance(sett_doc, dict) else None)
    )

    if beneficiary_type == "Sales Person":
        linked = frappe.db.get_value(
            "Sales Person", {"user": frappe.session.user}, "name"
        )
        sp = getattr(sett_doc, "sales_person", None)
        if isinstance(sett_doc, dict):
            sp = sp or sett_doc.get("sales_person")
        if not linked or linked != sp:
            frappe.throw(
                _("Insufficient permissions to view this settlement."),
                frappe.PermissionError,
            )
    elif beneficiary_type == "Sales Partner":
        # Sales Partner has no user link in standard ERPNext; deny for
        # non-privileged users unless explicitly linked via beneficiary.
        bp = getattr(sett_doc, "sales_partner", None)
        if isinstance(sett_doc, dict):
            bp = bp or sett_doc.get("sales_partner")
        if not bp or bp != getattr(sett_doc, "beneficiary", None):
            frappe.throw(
                _("Insufficient permissions to view this settlement."),
                frappe.PermissionError,
            )
        # Fall through to permission check below for partner.
        if not frappe.has_permission("Commission Settlement", "read", sett_name):
            frappe.throw(
                _("Insufficient permissions to view this settlement."),
                frappe.PermissionError,
            )
    elif beneficiary_type == "Employee":
        linked_emp = frappe.db.get_value(
            "Employee", {"user_id": frappe.session.user}, "name"
        )
        emp = getattr(sett_doc, "employee", None)
        if isinstance(sett_doc, dict):
            emp = emp or sett_doc.get("employee")
        if not linked_emp or linked_emp != emp:
            frappe.throw(
                _("Insufficient permissions to view this settlement."),
                frappe.PermissionError,
            )
    else:
        # Supplier or unknown beneficiary: deny non-privileged read.
        frappe.throw(
            _("Insufficient permissions to view this settlement."),
            frappe.PermissionError,
        )


def _parse_settlement_list(settlements):
    """Normalize the various input shapes into a list of settlement names."""
    if settlements is None:
        return []
    if isinstance(settlements, str):
        s = settlements.strip()
        if not s:
            return []
        # Try JSON array first.
        try:
            import json

            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
            if isinstance(parsed, str):
                return [parsed.strip()] if parsed.strip() else []
        except Exception:
            pass
        # Comma/newline separated.
        parts = [p.strip() for p in s.replace("\n", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(settlements, (list, tuple)):
        out = []
        for x in settlements:
            if isinstance(x, dict):
                n = x.get("name") or x.get("settlement")
                if n:
                    out.append(str(n).strip())
            else:
                s = str(x).strip()
                if s:
                    out.append(s)
        return out
    # Single value.
    s = str(settlements).strip()
    return [s] if s else []


# ---------------------------------------------------------------------------
# Whitelisted entry points for client calls.
# ---------------------------------------------------------------------------
@frappe.whitelist(methods=["GET", "POST"])
def get_payout_status_for_settlement(settlement=None, **kwargs):
    """Return the server-derived payout status for a single settlement.

    Hardened (P3.5.3): requires authentication, validates the settlement ref,
    enforces read permissions and beneficiary scoping.
    """
    if settlement is None:
        settlement = (
            kwargs.get("settlement")
            or kwargs.get("settlement_name")
            or kwargs.get("name")
        )
    if not settlement:
        frappe.throw(_("Commission Settlement is required."), frappe.ValidationError)

    sett_name, sett_doc = _resolve_settlement(settlement)

    if frappe.session.user == "Guest":
        frappe.throw(_("Authentication required."), frappe.PermissionError)

    if not frappe.has_permission("Commission Settlement", "read", sett_name):
        frappe.throw(
            _("Insufficient permissions to view this settlement."),
            frappe.PermissionError,
        )

    _enforce_settlement_read_access(sett_name, sett_doc)

    return compute_payout_status(sett_doc)


@frappe.whitelist(methods=["GET", "POST"])
def get_payout_status_for_settlements(settlements=None, **kwargs):
    """Batch helper: return payout statuses for multiple settlements.

    Accepts a list/JSON array/comma-separated string of settlement names.
    Results are returned as a list of status dicts in the same order as the
    input (deduplicated). Settlements the caller cannot read are omitted
    rather than raising, so a partial result is still useful; an empty input
    returns an empty list.
    """
    if settlements is None:
        settlements = kwargs.get("settlements") or kwargs.get("settlement_names")
    names = _parse_settlement_list(settlements)
    # Deduplicate while preserving order.
    seen = set()
    unique = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)

    results = []
    for n in unique:
        if not frappe.db.exists("Commission Settlement", n):
            continue
        try:
            doc = frappe.get_doc("Commission Settlement", n)
        except Exception:
            continue
        if not frappe.has_permission("Commission Settlement", "read", n):
            continue
        try:
            _enforce_settlement_read_access(n, doc)
        except Exception:
            continue
        try:
            results.append(compute_payout_status(doc))
        except Exception:
            # Fail closed per settlement: skip rather than abort the batch.
            continue
    return results


@frappe.whitelist(methods=["GET", "POST"])
def get_payout_status_map(settlements=None, **kwargs):
    """Batch helper returning a dict keyed by settlement name."""
    items = get_payout_status_for_settlements(settlements, **kwargs)
    return {it.get("settlement"): it for it in items if it.get("settlement")}
