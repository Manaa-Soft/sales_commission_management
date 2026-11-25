"""Commission recovery position calculation and recovery source validation.

This module provides:
- P3.4.2: Recovery position engine (get_commission_recovery_position)
- P3.4.5: Recovery source validation (supplier and employee)
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
    if comm_currency == company_currency:
        rate = 1.0
    elif rate <= 0:
        frappe.throw(
            _("Invalid commission-to-company rate for entry {0}.").format(
                commission_entry
            )
        )
    original_commission = flt(
        flt(orig.earned_commission) * rate, precision
    )

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
    adjustment_names = [a.name for a in adjustments]

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
        adj_rate = flt(adj.commission_to_company_rate)
        adj_comm_curr = adj.commission_currency or company_currency
        if adj_comm_curr == company_currency:
            adj_rate = 1.0
        elif adj_rate <= 0:
            frappe.throw(
                _("Invalid commission-to-company rate for adjustment {0}.").format(
                    adj.name
                )
            )
        adj_amount = flt(flt(adj.earned_commission) * adj_rate, precision)
        net_entitlement_raw += adj_amount

    for cr in cancel_of_returns:
        cr_rate = flt(cr.commission_to_company_rate)
        cr_comm_curr = cr.commission_currency or company_currency
        if cr_comm_curr == company_currency:
            cr_rate = 1.0
        elif cr_rate <= 0:
            frappe.throw(
                _("Invalid commission-to-company rate for cancellation {0}.").format(
                    cr.name
                )
            )
        cr_amount = flt(flt(cr.earned_commission) * cr_rate, precision)
        net_entitlement_raw += cr_amount

    for co in cancel_of_original:
        co_rate = flt(co.commission_to_company_rate)
        co_comm_curr = co.commission_currency or company_currency
        if co_comm_curr == company_currency:
            co_rate = 1.0
        elif co_rate <= 0:
            frappe.throw(
                _("Invalid commission-to-company rate for cancellation {0}.").format(
                    co.name
                )
            )
        co_amount = flt(flt(co.earned_commission) * co_rate, precision)
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
        reversal_map.setdefault(pr.reversal_of, []).append(pr)

    net_paid = 0.0
    payout_allocations = []
    for pe in payout_events:
        pe_amount = flt(pe.company_amount, precision)
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
        due_i = flt(min(net_pe, remaining_recovery_due), precision)
        remaining_recovery_due = flt(remaining_recovery_due - due_i, precision)

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

    # Enforce recovery limits.
    commission_entry = event_data.get("commission_entry") or orig_event.commission_entry
    if commission_entry:
        position = get_commission_recovery_position(commission_entry)
        recovery_amount = flt(event_data.get("company_amount", 0))
        outstanding = position.get("outstanding_recovery", 0)
        if recovery_amount > outstanding + 0.005:
            frappe.throw(
                _("Recovery amount {0} exceeds outstanding recovery {1}.").format(
                    recovery_amount, outstanding
                )
            )

    # Use event_factory to create the event.
    try:
        from sales_commission_management.sales_commission_management.payout.event_factory import (
            get_or_create_payout_event,
        )
    except Exception:
        frappe.throw(_("Event factory is not available."))

    event_data["event_type"] = "Recovery"

    # Generate event_key if not provided, including recovery_of for uniqueness.
    if not event_data.get("event_key"):
        from sales_commission_management.sales_commission_management.payout.event_factory import (
            generate_event_key,
        )
        event_data["event_key"] = generate_event_key(
            event_data.get("source_doctype"),
            event_data.get("source_name"),
            event_data.get("source_row"),
            event_data.get("settlement"),
            event_data.get("settlement_item"),
            event_data.get("commission_entry"),
            "Recovery",
            recovery_of=event_data.get("recovery_of"),
        )

    name = get_or_create_payout_event(event_data)
    return name

# GL entry functions for recovery accounting (P3.4.6)

def create_recovery_receivable_gl_entry(
    company,
    commission_entry,
    recovery_amount,
    currency,
    posting_date,
    remarks=None,
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

    if not company or not commission_entry or not recovery_amount:
        return None

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

    # Validate accounts belong to company.
    for acct in (receivable_account, expense_account):
        acct_company = frappe.db.get_value("Account", acct, "company")
        if acct_company != company:
            frappe.throw(
                _("Account {0} does not belong to company {1}.").format(acct, company)
            )
        is_group = frappe.db.get_value("Account", acct, "is_group")
        if is_group:
            frappe.throw(_("Account {0} must be a ledger account.").format(acct))

    cost_center = mapping.get("cost_center")

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
            "user_remark": remarks or "Commission recovery receivable for {0}".format(
                commission_entry
            ),
        })
        if cost_center:
            for a in je.accounts:
                a.cost_center = cost_center
        je.insert(ignore_permissions=True)
        je.submit()
        return je.name
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "create_recovery_receivable_gl_entry failed for {0}".format(
                commission_entry
            ),
        )
        raise


def create_recovery_collection_gl_entry(
    company,
    commission_entry,
    recovery_amount,
    currency,
    posting_date,
    bank_account=None,
    remarks=None,
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
    if not company or not commission_entry or not recovery_amount:
        return None

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

    if not bank_account:
        bank_account = frappe.db.get_value(
            "Account", {"account_type": "Bank", "company": company}, "name"
        )
    if not bank_account:
        return None

    cost_center = mapping.get("cost_center")

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
            "user_remark": remarks or "Commission recovery collection for {0}".format(
                commission_entry
            ),
        })
        if cost_center:
            for a in je.accounts:
                a.cost_center = cost_center
        je.insert(ignore_permissions=True)
        je.submit()
        return je.name
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "create_recovery_collection_gl_entry failed for {0}".format(
                commission_entry
            ),
        )
        raise