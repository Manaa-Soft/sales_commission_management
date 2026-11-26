"""Commission recovery position calculation and recovery source validation.

This module provides:
- P3.4.2: Recovery position engine (get_commission_recovery_position)
- P3.4.5: Recovery source validation (supplier and employee)
"""

import hashlib
import math

import frappe
from frappe import _
from frappe.utils import flt

from sales_commission_management.sales_commission_management.utils import (
    currency_precision as _cp,
)


def get_commission_recovery_position(commission_entry, for_update=True):
    """Calculate the recoverable balance for a commission entry.

    Locks the original Commission Entry, reads all submitted ledger events,
    and computes the recovery position in company currency.

    Args:
        commission_entry: Name of the original Commission Entry.

    Returns:
        dict with recovery position details.

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

    # Read the original entry. Lock only when for_update=True (default, for
    # transactional callers); audit/read-only paths pass for_update=False to
    # avoid acquiring row locks.
    if for_update:
        sql = (
            "SELECT name, docstatus, company, company_currency, "
            "commission_currency, commission_to_company_rate, earned_commission, "
            "event_type, original_entry, reverses_entry "
            "FROM `tabCommission Entry` WHERE name = %s FOR UPDATE"
        )
    else:
        sql = (
            "SELECT name, docstatus, company, company_currency, "
            "commission_currency, commission_to_company_rate, earned_commission, "
            "event_type, original_entry, reverses_entry "
            "FROM `tabCommission Entry` WHERE name = %s"
        )
    locked = frappe.db.sql(sql, (commission_entry,), as_dict=True)
    if not locked:
        frappe.throw(
            _("Could not lock Commission Entry {0}.").format(commission_entry)
        )

    orig = locked[0]
    if orig["docstatus"] != 1:
        frappe.throw(
            _("Commission Entry {0} must be submitted.").format(commission_entry)
        )

    company = orig["company"]
    company_currency = orig["company_currency"]
    if not company or not company_currency:
        frappe.throw(
            _("Commission Entry {0} is missing company or company currency.").format(
                commission_entry
            )
        )

    comm_currency = orig["commission_currency"] or company_currency
    # Recovery position is computed in commission currency to avoid mixing
    # FX differences with commission overpayment.
    precision = _cp(comm_currency)

    # --- Original commission (in commission currency) ---
    rate = flt(orig["commission_to_company_rate"])
    if comm_currency == company_currency:
        rate = 1.0
    elif rate <= 0:
        frappe.throw(
            _("Invalid commission-to-company rate for entry {0}.").format(
                commission_entry
            )
        )
    # Keep entitlement in commission currency (do not convert to company).
    original_commission = flt(orig["earned_commission"], precision)

    # --- Return adjustments ---
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
    adjustment_names = [a["name"] for a in adjustments]

    # --- Cancellation reversals of Return Adjustments ---
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

    # --- Cancellation reversals directly referencing original ---
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
        adj_comm_curr = adj["commission_currency"] or comm_currency
        if adj_comm_curr != comm_currency:
            frappe.throw(
                _("Adjustment {0} currency {1} does not match entry currency {2}.").format(
                    adj["name"], adj_comm_curr, comm_currency
                )
            )
        adj_amount = flt(adj["earned_commission"], precision)
        net_entitlement_raw += adj_amount

    for cr in cancel_of_returns:
        cr_comm_curr = cr["commission_currency"] or comm_currency
        if cr_comm_curr != comm_currency:
            frappe.throw(
                _("Cancellation {0} currency {1} does not match entry currency {2}.").format(
                    cr["name"], cr_comm_curr, comm_currency
                )
            )
        cr_amount = flt(cr["earned_commission"], precision)
        net_entitlement_raw += cr_amount

    for co in cancel_of_original:
        co_comm_curr = co["commission_currency"] or comm_currency
        if co_comm_curr != comm_currency:
            frappe.throw(
                _("Cancellation {0} currency {1} does not match entry currency {2}.").format(
                    co["name"], co_comm_curr, comm_currency
                )
            )
        co_amount = flt(co["earned_commission"], precision)
        net_entitlement_raw += co_amount

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
            "commission_amount",
            "commission_currency",
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

    reversal_map = {}
    for pr in payout_reversals:
        reversal_map.setdefault(pr["reversal_of"], []).append(pr)

    net_paid = 0.0
    payout_allocations = []
    for pe in payout_events:
        pe_amount = flt(pe["commission_amount"], precision)
        pe_reversals = reversal_map.get(pe["name"], [])
        reversal_total = flt(
            sum(flt(pr["commission_amount"], precision) for pr in pe_reversals),
            precision,
        )
        net_pe = flt(pe_amount - reversal_total, precision)
        net_paid = flt(net_paid + net_pe, precision)

        payout_allocations.append({
            "payout_event": pe["name"],
            "posting_date": pe["posting_date"],
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
        fields=["name", "company_amount", "company_currency", "recovery_of"],
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

    # Map recovery event name -> original payout event (recovery_of).
    recovery_of_map = {re["name"]: re["recovery_of"] for re in recovery_events}

    # Map recovery reversal -> its recovery event's original payout.
    reversal_payout_map = {}
    for rr in recovery_reversals:
        payout = recovery_of_map.get(rr["reversal_of"])
        reversal_payout_map[rr["name"]] = payout

    gross_net_recovered = flt(
        sum(flt(r["commission_amount"], precision) for r in recovery_events)
        - sum(flt(rr["commission_amount"], precision) for rr in recovery_reversals),
        precision,
    )

    # Allocate recovered amounts per referenced payout event.
    payout_event_names = {pe["name"] for pe in payout_events}
    # Validate recovery references: reject (do not silently skip) any
    # Recovery/Recovery Reversal whose recovery_of does not identify a
    # submitted Payout belonging to this Commission Entry.
    for re in recovery_events:
        po = re["recovery_of"]
        if po and po not in payout_event_names:
            frappe.throw(
                _("Recovery event {0} references payout {1}, which does not "
                  "belong to Commission Entry {2}.").format(
                    re["name"], po, commission_entry
                )
            )
    for rr in recovery_reversals:
        po = reversal_payout_map.get(rr["name"])
        if po and po not in payout_event_names:
            frappe.throw(
                _("Recovery Reversal {0} references an invalid payout.").format(
                    rr["name"]
                )
            )

    recovered_by_payout = {}
    for re in recovery_events:
        po = re["recovery_of"]
        if not po:
            continue
        recovered_by_payout[po] = flt(
            recovered_by_payout.get(po, 0.0) + flt(re["commission_amount"], precision),
            precision,
        )
    for rr in recovery_reversals:
        po = reversal_payout_map.get(rr["name"])
        if not po:
            continue
        recovered_by_payout[po] = flt(
            recovered_by_payout.get(po, 0.0) - flt(rr["commission_amount"], precision),
            precision,
        )

    # --- Calculate recovery obligation ---
    recovery_due_before_recovery = flt(
        max(0.0, net_paid - net_entitlement), precision
    )
    outstanding_recovery = flt(
        max(0.0, recovery_due_before_recovery - gross_net_recovered), precision
    )

    # Over-recovery check (on normalized values, no tolerance after normalization)
    if gross_net_recovered > recovery_due_before_recovery + 0.005:
        frappe.throw(
            _("Over-recovery detected for Commission Entry {0}: "
              "net_paid={1}, net_entitlement={2}, net_recovered={3}, "
              "recovery_due={4}.").format(
                commission_entry,
                net_paid,
                net_entitlement,
                gross_net_recovered,
                recovery_due_before_recovery,
            )
        )

    # --- Allocate against payouts (FIFO for the due amount; recovery is
    # attributed to the specific payout referenced by recovery_of) ---
    remaining_recovery_due = recovery_due_before_recovery

    allocated_recovered = 0.0
    for alloc in payout_allocations:
        net_pe = alloc["net_payout"]
        due_i = flt(min(net_pe, remaining_recovery_due), precision)
        remaining_recovery_due = flt(remaining_recovery_due - due_i, precision)

        pe_name = alloc["payout_event"]
        recovered_for_pe = flt(recovered_by_payout.get(pe_name, 0.0), precision)
        recovered_i = flt(min(due_i, recovered_for_pe), precision)

        alloc["recovered"] = recovered_i
        alloc["recoverable_balance"] = flt(due_i - recovered_i, precision)
        allocated_recovered = flt(allocated_recovered + recovered_i, precision)

    # Reconcile aggregate recovery with payout-level allocations. For valid
    # data these must agree; a mismatch indicates inconsistent references.
    if abs(gross_net_recovered - allocated_recovered) > 0.005:
        frappe.throw(
            _("Recovery reconciliation failed for Commission Entry {0}: "
              "aggregate recovered {1} != payout-allocated recovered {2}.").format(
                commission_entry, gross_net_recovered, allocated_recovered
            )
        )

    net_recovered = gross_net_recovered

    # --- Return position ---
    return {
        "commission_entry": commission_entry,
        "currency": comm_currency,
        "original_commission": original_commission,
        "net_entitlement": net_entitlement,
        "net_paid": net_paid,
        "net_recovered": net_recovered,
        "recovery_due_before_recovery": recovery_due_before_recovery,
        "outstanding_recovery": outstanding_recovery,
        "payout_events": [pe["name"] for pe in payout_events],
        "recovery_events": [re["name"] for re in recovery_events],
        "return_adjustments": adjustment_names,
        "cancellation_reversals": (
            [cr["name"] for cr in cancel_of_returns]
            + [co["name"] for co in cancel_of_original]
        ),
        "payout_allocations": payout_allocations,
    }


# ---------------------------------------------------------------------------
# P3.4.5 — Recovery source validation
# ---------------------------------------------------------------------------


def validate_supplier_recovery_link(
    settlement,
    payment_entry,
    payment_entry_reference,
    company,
):
    """Validate a supplier recovery (refund receipt) against a settlement.

    Ensures the Payment Entry is a genuine supplier refund linked to the
    settlement's Purchase Invoice, with correct company and amounts.

    Args:
        settlement: Name of the Commission Settlement.
        payment_entry: Name of the supplier Payment Entry (refund).
        payment_entry_reference: Name of the Payment Entry Reference row.
        company: Expected company.

    Raises:
        frappe.ValidationError on any mismatch.
    """
    if not frappe.db.exists("Commission Settlement", settlement):
        frappe.throw(_("Commission Settlement {0} does not exist.").format(settlement))

    sett = frappe.get_doc("Commission Settlement", settlement)
    if sett.docstatus != 1:
        frappe.throw(_("Commission Settlement must be submitted."))

    if sett.company != company:
        frappe.throw(
            _("Settlement company must match {0}.").format(company)
        )

    if not frappe.db.exists("Payment Entry", payment_entry):
        frappe.throw(_("Payment Entry {0} does not exist.").format(payment_entry))

    pe = frappe.get_doc("Payment Entry", payment_entry)
    if pe.docstatus != 1:
        frappe.throw(_("Payment Entry must be submitted."))

    if pe.company != company:
        frappe.throw(
            _("Payment Entry company must match settlement company.")
        )

    # Validate supplier matches settlement supplier.
    if sett.supplier and pe.party != sett.supplier:
        frappe.throw(
            _("Payment Entry supplier must match settlement supplier.")
        )

    # For supplier recovery, the company receives a refund from the supplier.
    if pe.payment_type not in ("Receive", None):
        frappe.throw(
            _("Payment Entry must be a Receive type for supplier recovery refund.")
        )

    if pe.party_type != "Supplier":
        frappe.throw(_("Payment Entry must be for a Supplier."))

    # Validate the reference row exists and belongs to this payment entry.
    if not frappe.db.exists("Payment Entry Reference", payment_entry_reference):
        frappe.throw(
            _("Payment Entry Reference {0} does not exist.").format(
                payment_entry_reference
            )
        )

    ref = frappe.get_doc("Payment Entry Reference", payment_entry_reference)
    if ref.parent != payment_entry:
        frappe.throw(
            _("Payment Entry Reference must belong to Payment Entry {0}.").format(
                payment_entry
            )
        )

    # If the settlement has a linked Purchase Invoice, validate the reference
    # matches it.
    if sett.settlement_document_type == "Purchase Invoice" and sett.settlement_document:
        if ref.reference_doctype != "Purchase Invoice":
            frappe.throw(
                _("Payment Entry Reference must be for a Purchase Invoice.")
            )
        if ref.reference_name != sett.settlement_document:
            frappe.throw(
                _("Payment Entry Reference must match the settlement's "
                  "Purchase Invoice {0}.").format(sett.settlement_document)
            )

    # Validate allocated amount is positive.
    allocated = flt(ref.allocated_amount)
    if allocated <= 0:
        frappe.throw(_("Allocated amount must be positive."))

    return True


def validate_employee_recovery_link(
    settlement,
    additional_salary,
    salary_slip=None,
    payroll_entry=None,
    bank_entry=None,
    bank_transaction=None,
):
    """Validate an employee recovery (payroll deduction) against a settlement.

    Similar to validate_employee_payroll_link but validates the deduction
    component rather than the earnings component of the Salary Slip.

    Args:
        settlement: Name of the Commission Settlement.
        additional_salary: Name of the Additional Salary (deduction).
        salary_slip: Optional Salary Slip name.
        payroll_entry: Optional Payroll Entry name.
        bank_entry: Optional Bank/Cash Journal Entry name.
        bank_transaction: Optional Bank Transaction name.

    Raises:
        frappe.ValidationError on any mismatch.
    """
    if not frappe.db.exists("Commission Settlement", settlement):
        frappe.throw(_("Commission Settlement {0} does not exist.").format(settlement))

    sett = frappe.get_doc("Commission Settlement", settlement)
    if sett.docstatus != 1:
        frappe.throw(_("Commission Settlement must be submitted."))

    if sett.beneficiary_type != "Sales Person":
        frappe.throw(_("Settlement must be for a Sales Person."))

    if not frappe.db.exists("DocType", "Additional Salary"):
        frappe.throw(_("HRMS is not installed or 'Additional Salary' DocType is not available."))
    if not frappe.db.exists("Additional Salary", additional_salary):
        frappe.throw(
            _("Additional Salary {0} does not exist.").format(additional_salary)
        )

    as_doc = frappe.get_doc("Additional Salary", additional_salary)
    if as_doc.docstatus != 1:
        frappe.throw(_("Additional Salary must be submitted."))

    # Exact reverse-link consistency.
    if as_doc.ref_doctype != "Commission Settlement" or as_doc.ref_docname != settlement:
        frappe.throw(_("Additional Salary must be linked to this settlement."))

    if as_doc.employee != sett.employee:
        frappe.throw(_("Additional Salary employee must match settlement employee."))

    # Strict company validation.
    if not sett.company or not as_doc.company or as_doc.company != sett.company:
        frappe.throw(
            _("Additional Salary company must match settlement company.")
        )

    emp_company = frappe.db.get_value("Employee", as_doc.employee, "company")
    if not emp_company or emp_company != sett.company:
        frappe.throw(_("Employee company must match settlement company."))

    # The Salary Component must be a deduction type.
    if as_doc.salary_component:
        comp_type = frappe.db.get_value(
            "Salary Component", as_doc.salary_component, "type"
        )
        if comp_type != "Deduction":
            frappe.throw(
                _("Salary Component must be a Deduction type for recovery.")
            )

    # Validate Salary Slip if provided.
    if salary_slip:
        if not frappe.db.exists("Salary Slip", salary_slip):
            frappe.throw(_("Salary Slip {0} does not exist.").format(salary_slip))
        ss = frappe.get_doc("Salary Slip", salary_slip)
        if ss.docstatus != 1:
            frappe.throw(_("Salary Slip must be submitted."))
        if ss.employee != as_doc.employee:
            frappe.throw(_("Salary Slip employee must match."))
        if ss.company != sett.company:
            frappe.throw(_("Salary Slip company must match settlement company."))

        # Verify the Additional Salary appears in deductions.
        found = False
        for ded in (ss.deductions or []):
            if getattr(ded, "additional_salary", None) == additional_salary:
                found = True
                break
        if not found:
            frappe.throw(
                _("Additional Salary not found in Salary Slip deductions.")
            )

    # Payroll Entry validation.
    if payroll_entry:
        if not frappe.db.exists("Payroll Entry", payroll_entry):
            frappe.throw(_("Payroll Entry {0} does not exist.").format(payroll_entry))
        pe_status = frappe.db.get_value("Payroll Entry", payroll_entry, "docstatus")
        if pe_status != 1:
            frappe.throw(_("Payroll Entry must be submitted."))
        pe_company = frappe.db.get_value("Payroll Entry", payroll_entry, "company")
        if not pe_company or pe_company != sett.company:
            frappe.throw(
                _("Payroll Entry company must match settlement company.")
            )

    # Bank Entry validation.
    if bank_entry:
        if not frappe.db.exists("Journal Entry", bank_entry):
            frappe.throw(_("Bank Entry {0} does not exist.").format(bank_entry))
        je_status = frappe.db.get_value("Journal Entry", bank_entry, "docstatus")
        if je_status != 1:
            frappe.throw(_("Bank Entry must be submitted."))
        je_company = frappe.db.get_value("Journal Entry", bank_entry, "company")
        if not je_company or je_company != sett.company:
            frappe.throw(
                _("Bank Entry company must match settlement company.")
            )
        voucher_type = frappe.db.get_value("Journal Entry", bank_entry, "voucher_type")
        if voucher_type not in ("Bank Entry", "Cash Entry"):
            frappe.throw(_("Bank Entry must be a Bank or Cash Entry."))

    # Bank Transaction validation.
    if bank_transaction:
        if not bank_entry or not payroll_entry:
            frappe.throw(
                _("Bank Transaction requires Bank Entry and Payroll Entry.")
            )
        if not frappe.db.exists("Bank Transaction", bank_transaction):
            frappe.throw(
                _("Bank Transaction {0} does not exist.").format(bank_transaction)
            )
        bt = frappe.db.get_value(
            "Bank Transaction",
            bank_transaction,
            ["docstatus", "status", "company"],
            as_dict=True,
        )
        if not bt or bt.docstatus != 1:
            frappe.throw(_("Bank Transaction must be submitted."))
        if bt.status != "Reconciled":
            frappe.throw(_("Bank Transaction must be Reconciled."))
        if not bt.company or bt.company != sett.company:
            frappe.throw(
                _("Bank Transaction company must match settlement company.")
            )

    return True


def create_recovery_event(event_data):
    """Create and submit a Recovery payout event.

    Uses the event_factory to create a Recovery-type Commission Payout Event.
    Requires recovery_of to link to the original Payout event.

    Args:
        event_data: dict with required fields for a Recovery event.
            Must include recovery_of (name of original Payout event).

    Returns:
        str: Name of the created Recovery event.

    Raises:
        frappe.ValidationError on validation failure.
    """
    if not event_data or not isinstance(event_data, dict):
        frappe.throw(_("Event data must be a non-empty dict."))

    # Reject non-Recovery event types.
    if event_data.get("event_type") and event_data["event_type"] != "Recovery":
        frappe.throw(_("Only Recovery event type is allowed."))

    if not event_data.get("recovery_of"):
        frappe.throw(_("recovery_of is required for Recovery events."))

    # Validate the original payout event exists and is submitted.
    recovery_of = event_data["recovery_of"]
    if not frappe.db.exists("Commission Payout Event", recovery_of):
        frappe.throw(
            _("Original Payout Event {0} does not exist.").format(recovery_of)
        )

    orig_event = frappe.get_doc("Commission Payout Event", recovery_of)
    if orig_event.docstatus != 1:
        frappe.throw(_("Original Payout Event must be submitted."))

    if orig_event.event_type != "Payout":
        frappe.throw(_("recovery_of must reference a Payout event."))

    # Validate company and beneficiary consistency.
    if event_data.get("company") and event_data["company"] != orig_event.company:
        frappe.throw(_("Company must match original Payout Event."))
    if event_data.get("beneficiary") and event_data["beneficiary"] != orig_event.beneficiary:
        frappe.throw(_("Beneficiary must match original Payout Event."))
    if event_data.get("commission_entry") and event_data["commission_entry"] != orig_event.commission_entry:
        frappe.throw(_("Commission Entry must match original Payout Event."))
    if event_data.get("settlement") and event_data["settlement"] != orig_event.settlement:
        frappe.throw(_("Settlement must match original Payout Event."))

    # Generate the authoritative event_key server-side. A caller-supplied key
    # must match this identity; otherwise reject it.
    try:
        from sales_commission_management.sales_commission_management.payout.event_factory import (
            generate_event_key,
            _normalize_value,
        )
        from sales_commission_management.sales_commission_management.payout.lifecycle import (
            create_and_submit_payout_event,
        )
    except Exception:
        frappe.throw(_("Event factory or lifecycle helper is not available."))

    event_data["event_type"] = "Recovery"

    expected_key = generate_event_key(
        event_data.get("source_doctype"),
        event_data.get("source_name"),
        event_data.get("source_row"),
        event_data.get("settlement"),
        event_data.get("settlement_item"),
        event_data.get("commission_entry"),
        "Recovery",
        recovery_of=event_data.get("recovery_of"),
    )
    supplied_key = event_data.get("event_key")
    if supplied_key and supplied_key != expected_key:
        frappe.throw(_("Supplied event_key does not match the expected identity."))
    event_data["event_key"] = expected_key

    # Idempotent retry: if an event with this key already exists, verify its
    # payload and return it (when submitted) without reapplying the balance
    # check. Balance validation applies only to genuinely new events.
    locked = frappe.db.sql(
        "SELECT name, docstatus FROM `tabCommission Payout Event` "
        "WHERE event_key = %s FOR UPDATE",
        (expected_key,),
        as_dict=True,
    )
    if locked:
        existing_name = locked[0]["name"]
        existing_status = locked[0]["docstatus"]
        # Verify payload matches.
        existing_doc = frappe.get_doc("Commission Payout Event", existing_name)
        # Complete canonical payload comparison: compare every field present on
        # either side, treating omitted optional values as None (not skipping
        # them). Normalize dates and currency/numeric values.
        skip_fields = {"doctype", "name", "owner", "creation", "modified",
                       "modified_by", "docstatus", "idx", "parent", "parenttype",
                       "parentfield"}
        all_fields = set(event_data.keys()) | set(existing_doc.as_dict().keys())
        for fld in sorted(all_fields - skip_fields):
            req_val = event_data.get(fld, None)
            db_val = getattr(existing_doc, fld, None)
            if _normalize_value(fld, db_val) != _normalize_value(fld, req_val):
                frappe.throw(
                    _("Existing Recovery event {0} has a different {1}.").format(
                        existing_name, fld
                    )
                )
        if existing_status == 1:
            return existing_name
        frappe.throw(
            _("Existing Recovery event {0} is not submitted.").format(existing_name)
        )

    # Enforce recovery limits against the referenced payout's recoverable
    # balance (payout-specific), not just the aggregate outstanding recovery.
    commission_entry = event_data.get("commission_entry") or orig_event.commission_entry
    if commission_entry:
        position = get_commission_recovery_position(commission_entry)
        recovery_amount = flt(event_data.get("company_amount", 0))
        recovery_of = event_data.get("recovery_of")
        allocs = position.get("payout_allocations", []) or []
        referenced_alloc = None
        for a in allocs:
            if a.get("payout_event") == recovery_of:
                referenced_alloc = a
                break
        if referenced_alloc is None:
            frappe.throw(
                _("Recovery event must reference a payout belonging to "
                  "Commission Entry {0}.").format(commission_entry)
            )
        payout_recoverable = flt(referenced_alloc.get("recoverable_balance", 0))
        if recovery_amount > payout_recoverable + 0.005:
            frappe.throw(
                _("Recovery amount {0} exceeds the recoverable balance {1} "
                  "of payout {2}.").format(
                    recovery_amount, payout_recoverable, recovery_of
                )
            )

    # Create and submit in one transaction-safe operation.
    name = create_and_submit_payout_event(event_data)
    return name

# GL entry functions for recovery accounting (P3.4.6)


def _verify_source_event_identity(source_event, company, commission_entry=None):
    """Enforce source-event identity before a recovery GL posting.

    - Commission Payout Event: must be submitted, Recovery type, matching
      company and (when provided) matching commission_entry.
    - Payment Entry / Journal Entry: must exist and be submitted, with
      matching company.
    - Unknown references are rejected (not silently accepted).
    """
    if not source_event:
        frappe.throw(_("source_event is required for recovery GL postings."))

    # Commission Payout Event
    if frappe.db.exists("Commission Payout Event", source_event):
        doc = frappe.get_doc("Commission Payout Event", source_event)
        if doc.docstatus != 1:
            frappe.throw(
                _("Source event {0} must be submitted.").format(source_event)
            )
        if getattr(doc, "event_type", None) != "Recovery":
            frappe.throw(
                _("Source event {0} must be a Recovery event.").format(source_event)
            )
        if company and getattr(doc, "company", None) and doc.company != company:
            frappe.throw(
                _("Source event {0} company must match.").format(source_event)
            )
        if commission_entry and getattr(doc, "commission_entry", None) and doc.commission_entry != commission_entry:
            frappe.throw(
                _("Source event {0} commission entry must match.").format(source_event)
            )
        return

    # Payment Entry
    if frappe.db.exists("Payment Entry", source_event):
        pe = frappe.get_doc("Payment Entry", source_event)
        if pe.docstatus != 1:
            frappe.throw(
                _("Source event {0} must be submitted.").format(source_event)
            )
        if company and getattr(pe, "company", None) and pe.company != company:
            frappe.throw(
                _("Source event {0} company must match.").format(source_event)
            )
        return

    # Journal Entry
    if frappe.db.exists("Journal Entry", source_event):
        je = frappe.get_doc("Journal Entry", source_event)
        if je.docstatus != 1:
            frappe.throw(
                _("Source event {0} must be submitted.").format(source_event)
            )
        if company and getattr(je, "company", None) and je.company != company:
            frappe.throw(
                _("Source event {0} company must match.").format(source_event)
            )
        return

    frappe.throw(_("Source event {0} does not exist.").format(source_event))


def _posting_fingerprint(company, commission_entry, recovery_amount, currency,
                        posting_date, bank_account, source_event, purpose):
    """Deterministic fingerprint of a recovery GL posting's financial identity."""
    import hashlib
    parts = [
        str(company or ""),
        str(commission_entry or ""),
        "{0:.6f}".format(float(recovery_amount or 0)),
        str(currency or ""),
        str(posting_date or ""),
        str(bank_account or ""),
        str(source_event or ""),
        str(purpose or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

def create_recovery_receivable_gl_entry(
    company,
    commission_entry,
    recovery_amount,
    currency,
    posting_date,
    remarks=None,
    source_event=None,
):
    """Create GL entries for a recovery receivable.

    When a paid commission becomes recoverable (due to returns after payout):
    - Debit: Commission Recovery Receivable account
    - Credit: Commission Expense or Payroll Expense account

    Args:
        company: Company name.
        commission_entry: Name of the original Commission Entry.
        recovery_amount: Amount to recognize as receivable (company currency).
        currency: Company currency.
        posting_date: Date for the GL entry.
        remarks: Optional remarks.

    Returns:
        str: Name of the created Journal Entry, or None if no account mapping.
    """
    import frappe
    from frappe.utils import flt
    from sales_commission_management.sales_commission_management.utils import (
        currency_precision as _cp,
    )

    import math
    if not company or not commission_entry:
        frappe.throw(_("Company and commission entry are required."))
    if not math.isfinite(float(recovery_amount)) or float(recovery_amount) <= 0:
        frappe.throw(_("Recovery amount must be a finite positive number."))

    PURPOSE_RECEIVABLE = "purpose:receivable"
    # Require a valid source_event for idempotent GL postings.
    if not source_event:
        frappe.throw(_("source_event is required for recovery GL postings."))

    _verify_source_event_identity(source_event, company, commission_entry)

    idem_key = "{0}:{1}".format(source_event, PURPOSE_RECEIVABLE)
    current_fp = _posting_fingerprint(
        company, commission_entry, recovery_amount, currency, posting_date,
        None, source_event, PURPOSE_RECEIVABLE
    )

    # Look up an existing idempotency record.
    existing_je = frappe.db.get_value(
        "Commission GL Idempotency", {"idempotency_key": idem_key},
        ["journal_entry", "posting_fingerprint"], as_dict=True
    )
    if existing_je and existing_je.journal_entry:
        je_status = frappe.db.get_value("Journal Entry", existing_je.journal_entry, "docstatus")
        if je_status == 1:
            fp = current_fp
            if not existing_je.posting_fingerprint or existing_je.posting_fingerprint != fp:
                frappe.throw(
                    _("Idempotency key {0} was reused with different posting "
                      "details.").format(idem_key)
                )
            _verify_source_event_identity(source_event, company)
            return existing_je.journal_entry
        frappe.throw(
            _("Existing recovery receivable posting for {0} is not submitted "
              "(docstatus={1}).").format(source_event, je_status)
        )

    mapping = None
    try:
        mappings = frappe.get_all(
            "Commission Account Mapping",
            filters={"company": company, "enabled": 1},
            fields=[
                "commission_expense_account",
                "payroll_commission_expense_account",
                "commission_recovery_receivable_account",
                "cost_center",
            ],
            limit=1,
        )
        if mappings:
            mapping = mappings[0]
    except Exception:
        frappe.throw(_("Failed to retrieve account mapping for {0}.").format(company))

    if not mapping:
        frappe.throw(
            _("Commission Account Mapping is missing for company {0}.").format(company)
        )

    receivable_account = mapping.get("commission_recovery_receivable_account")
    if not receivable_account:
        frappe.throw(
            _("Commission Recovery Receivable Account is not configured for {0}.").format(
                company
            )
        )

    expense_account = mapping.get("commission_expense_account")
    if not expense_account:
        expense_account = mapping.get("payroll_commission_expense_account")
    if not expense_account:
        frappe.throw(
            _("Commission Expense Account is not configured for {0}.").format(company)
        )

    # Validate accounts belong to company, are enabled, non-group, correct type.
    for acct, expected_root in ((receivable_account, "Asset"), (expense_account, "Expense")):
        acct_info = frappe.db.get_value(
            "Account", acct, ["company", "is_group", "disabled", "root_type", "account_currency"], as_dict=True
        )
        if not acct_info:
            frappe.throw(_("Account {0} not found.").format(acct))
        if acct_info.company != company:
            frappe.throw(
                _("Account {0} does not belong to company {1}.").format(acct, company)
            )
        if acct_info.is_group:
            frappe.throw(_("Account {0} must be a ledger account.").format(acct))
        if acct_info.disabled:
            frappe.throw(_("Account {0} is disabled.").format(acct))
        if acct_info.root_type != expected_root:
            frappe.throw(
                _("Account {0} must be an {1} account.").format(acct, expected_root)
            )
        if acct_info.account_currency != currency:
            frappe.throw(
                _("Account {0} currency ({1}) does not match posting currency ({2}).").format(
                    acct, acct_info.account_currency, currency
                )
            )

    cost_center = mapping.get("cost_center")

    # Claim the idempotency slot FIRST so concurrent workers cannot both
    # create Journal Entries. The unique constraint on idempotency_key is the
    # database-level guard.
    claimed = False
    for _i in range(3):
        try:
            frappe.get_doc({
                "doctype": "Commission GL Idempotency",
                "idempotency_key": idem_key,
                "journal_entry": None,
                "posting_purpose": PURPOSE_RECEIVABLE,
                "source_event": source_event,
                "company": company,
                "posting_fingerprint": current_fp,
            }).insert(ignore_permissions=True)
            claimed = True
            break
        except frappe.DuplicateEntryError:
            claimed = False
            break
        except Exception:
            raise

    if not claimed:
        # Another worker owns the slot. Wait for it to finish posting.
        winner = None
        winner_fp = None
        for _i in range(20):
            rows = frappe.db.sql(
                "SELECT journal_entry, posting_fingerprint "
                "FROM `tabCommission GL Idempotency` "
                "WHERE idempotency_key = %s FOR UPDATE",
                (idem_key,),
                as_dict=True,
            )
            if rows and rows[0].get("journal_entry"):
                winner = rows[0]["journal_entry"]
                winner_fp = rows[0].get("posting_fingerprint")
                break
        if winner:
            w_status = frappe.db.get_value("Journal Entry", winner, "docstatus")
            if w_status == 1:
                if not winner_fp or winner_fp != current_fp:
                    frappe.throw(
                        _("Idempotency key {0} was reused with different posting "
                          "details.").format(idem_key)
                    )
                _verify_source_event_identity(source_event, company, commission_entry)
                return winner
            frappe.throw(
                _("Concurrent recovery receivable posting for {0} is not "
                  "submitted (docstatus={1}).").format(source_event, w_status)
            )
        frappe.throw(
            _("Concurrent recovery receivable posting for {0} is still in "
              "progress; retry.").format(source_event)
        )

    # Slot claimed: create and submit the Journal Entry. On failure the
    # transaction rolls back, removing the claim and any Draft JE.
    try:
        je = frappe.get_doc({
            "doctype": "Journal Entry",
            "voucher_type": "Journal Entry",
            "company": company,
            "posting_date": posting_date,
            "accounts": [
                {
                    "account": receivable_account,
                    "debit_in_account_currency": flt(recovery_amount, _cp(currency)),
                    "credit_in_account_currency": 0,
                },
                {
                    "account": expense_account,
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": flt(recovery_amount, _cp(currency)),
                },
            ],
            "user_remark": "{0} [source_event:{1}] [purpose:receivable]".format(
                remarks or "Commission recovery receivable for {0}".format(commission_entry),
                source_event,
            ),
        })
        if cost_center:
            for a in je.accounts:
                a.cost_center = cost_center
        je.insert(ignore_permissions=True)
        je.submit()
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "create_recovery_receivable_gl_entry failed for {0}".format(
                commission_entry
            ),
        )
        raise

    # Record the JE name against the claimed slot.
    frappe.db.set_value(
        "Commission GL Idempotency",
        {"idempotency_key": idem_key},
        "journal_entry",
        je.name,
    )
    return je.name


def create_recovery_collection_gl_entry(
    company,
    commission_entry,
    recovery_amount,
    currency,
    posting_date,
    bank_account=None,
    remarks=None,
    source_event=None,
):
    """Create GL entries for an actual recovery collection.

    When money is actually recovered (cash collected or offset applied):
    - Debit: Bank or Payable account
    - Credit: Commission Recovery Receivable account

    Args:
        company: Company name.
        commission_entry: Name of the original Commission Entry.
        recovery_amount: Amount collected (company currency).
        currency: Company currency.
        posting_date: Date for the GL entry.
        bank_account: Bank/Payable account to debit.
        remarks: Optional remarks.

    Returns:
        str: Name of the created Journal Entry, or None.
    """
    import math
    if not company or not commission_entry:
        frappe.throw(_("Company and commission entry are required."))
    if not math.isfinite(float(recovery_amount)) or float(recovery_amount) <= 0:
        frappe.throw(_("Recovery amount must be a finite positive number."))

    # Idempotency check by durable source_event + posting purpose.
    PURPOSE_COLLECTION = "purpose:collection"
    if not source_event:
        frappe.throw(_("source_event is required for recovery GL postings."))

    _verify_source_event_identity(source_event, company, commission_entry)

    idem_key = "{0}:{1}".format(source_event, PURPOSE_COLLECTION)

    # Resolve the default bank account early so the fingerprint and the
    # existing-posting check use the same value on retries with bank_account=None.
    if not bank_account:
        bank_account = frappe.db.get_value(
            "Account",
            {"account_type": "Bank", "company": company, "is_group": 0, "disabled": 0},
            "name",
            order_by="name asc",
        )
    if not bank_account:
        frappe.throw(
            _("No active Bank account found for company {0}.").format(company)
        )

    current_fp = _posting_fingerprint(
        company, commission_entry, recovery_amount, currency, posting_date,
        bank_account, source_event, PURPOSE_COLLECTION
    )

    existing_je = frappe.db.get_value(
        "Commission GL Idempotency", {"idempotency_key": idem_key},
        ["journal_entry", "posting_fingerprint"], as_dict=True
    )
    if existing_je and existing_je.journal_entry:
        je_status = frappe.db.get_value("Journal Entry", existing_je.journal_entry, "docstatus")
        if je_status == 1:
            fp = current_fp
            if not existing_je.posting_fingerprint or existing_je.posting_fingerprint != fp:
                frappe.throw(
                    _("Idempotency key {0} was reused with different posting "
                      "details.").format(idem_key)
                )
            _verify_source_event_identity(source_event, company)
            return existing_je.journal_entry
        frappe.throw(
            _("Concurrent recovery collection posting for {0} is not submitted "
              "(docstatus={1}).").format(source_event, je_status)
        )

    mapping = None
    try:
        mappings = frappe.get_all(
            "Commission Account Mapping",
            filters={"company": company, "enabled": 1},
            fields=[
                "commission_recovery_receivable_account",
                "cost_center",
            ],
            limit=1,
        )
        if mappings:
            mapping = mappings[0]
    except Exception:
        frappe.throw(_("Failed to retrieve account mapping for {0}.").format(company))

    if not mapping:
        frappe.throw(
            _("Commission Account Mapping is missing for company {0}.").format(company)
        )

    receivable_account = mapping.get("commission_recovery_receivable_account")
    if not receivable_account:
        frappe.throw(
            _("Commission Recovery Receivable Account is not configured for {0}.").format(
                company
            )
        )

    # Validate bank account.
    bank_info = frappe.db.get_value(
        "Account", bank_account, ["company", "is_group", "disabled", "account_type", "root_type", "account_currency"], as_dict=True
    )
    if not bank_info:
        frappe.throw(_("Bank account {0} not found.").format(bank_account))
    if bank_info.company != company:
        frappe.throw(
            _("Bank account {0} does not belong to company {1}.").format(
                bank_account, company
            )
        )
    if bank_info.is_group:
        frappe.throw(_("Bank account {0} must be a ledger account.").format(bank_account))
    if bank_info.disabled:
        frappe.throw(_("Bank account {0} is disabled.").format(bank_account))
    if bank_info.account_type != "Bank":
        frappe.throw(_("Account {0} must be a Bank account.").format(bank_account))
    if bank_info.root_type != "Asset":
        frappe.throw(_("Account {0} must be an Asset account.").format(bank_account))
    if bank_info.account_currency != currency:
        frappe.throw(
            _("Bank account {0} currency ({1}) does not match posting currency ({2}).").format(
                bank_account, bank_info.account_currency, currency
            )
        )

    # Validate receivable account.
    rec_info = frappe.db.get_value(
        "Account", receivable_account, ["company", "is_group", "disabled", "root_type", "account_currency"], as_dict=True
    )
    if not rec_info:
        frappe.throw(_("Receivable account {0} not found.").format(receivable_account))
    if rec_info.company != company:
        frappe.throw(
            _("Receivable account {0} does not belong to company {1}.").format(
                receivable_account, company
            )
        )
    if rec_info.is_group:
        frappe.throw(_("Receivable account {0} must be a ledger account.").format(receivable_account))
    if rec_info.disabled:
        frappe.throw(_("Receivable account {0} is disabled.").format(receivable_account))
    if rec_info.root_type != "Asset":
        frappe.throw(_("Receivable account {0} must be an Asset account.").format(receivable_account))
    if rec_info.account_currency != currency:
        frappe.throw(
            _("Receivable account {0} currency ({1}) does not match posting currency ({2}).").format(
                receivable_account, rec_info.account_currency, currency
            )
        )

    cost_center = mapping.get("cost_center")

    # Claim the idempotency slot FIRST.
    claimed = False
    for _i in range(3):
        try:
            frappe.get_doc({
                "doctype": "Commission GL Idempotency",
                "idempotency_key": idem_key,
                "journal_entry": None,
                "posting_purpose": PURPOSE_COLLECTION,
                "source_event": source_event,
                "company": company,
                "posting_fingerprint": current_fp,
            }).insert(ignore_permissions=True)
            claimed = True
            break
        except frappe.DuplicateEntryError:
            claimed = False
            break
        except Exception:
            raise

    if not claimed:
        winner = None
        winner_fp = None
        for _i in range(20):
            rows = frappe.db.sql(
                "SELECT journal_entry, posting_fingerprint "
                "FROM `tabCommission GL Idempotency` "
                "WHERE idempotency_key = %s FOR UPDATE",
                (idem_key,),
                as_dict=True,
            )
            if rows and rows[0].get("journal_entry"):
                winner = rows[0]["journal_entry"]
                winner_fp = rows[0].get("posting_fingerprint")
                break
        if winner:
            w_status = frappe.db.get_value("Journal Entry", winner, "docstatus")
            if w_status == 1:
                if not winner_fp or winner_fp != current_fp:
                    frappe.throw(
                        _("Idempotency key {0} was reused with different posting "
                          "details.").format(idem_key)
                    )
                _verify_source_event_identity(source_event, company, commission_entry)
                return winner
            frappe.throw(
                _("Concurrent recovery collection posting for {0} is not "
                  "submitted (docstatus={1}).").format(source_event, w_status)
            )
        frappe.throw(
            _("Concurrent recovery collection posting for {0} is still in "
              "progress; retry.").format(source_event)
        )

    try:
        je = frappe.get_doc({
            "doctype": "Journal Entry",
            "voucher_type": "Journal Entry",
            "company": company,
            "posting_date": posting_date,
            "accounts": [
                {
                    "account": bank_account,
                    "debit_in_account_currency": flt(recovery_amount, _cp(currency)),
                    "credit_in_account_currency": 0,
                },
                {
                    "account": receivable_account,
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": flt(recovery_amount, _cp(currency)),
                },
            ],
            "user_remark": "{0} [source_event:{1}] [purpose:collection]".format(
                remarks or "Commission recovery collection for {0}".format(commission_entry),
                source_event,
            ),
        })
        if cost_center:
            for a in je.accounts:
                a.cost_center = cost_center
        je.insert(ignore_permissions=True)
        je.submit()
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "create_recovery_collection_gl_entry failed for {0}".format(
                commission_entry
            ),
        )
        raise

    frappe.db.set_value(
        "Commission GL Idempotency",
        {"idempotency_key": idem_key},
        "journal_entry",
        je.name,
    )
    return je.name
