import frappe
from frappe import _
from sales_commission_management.sales_commission_management.payout.event_factory import get_or_create_payout_event


def submit_payout_event(event_name, source_validation_fn=None):
    """Submit a payout event after validating the source transaction.

    This is the trusted server-side handler for the Draft-to-Submitted
    lifecycle of a Commission Payout Event.

    Contract:
    1. The caller (trusted handler) validates the source transaction
       (e.g., Payment Entry submitted, Purchase Invoice matches).
    2. This function retrieves the Draft event (or creates it if missing).
    3. It validates the event's relationships and financial fields.
    4. It submits the event (Draft -> Submitted).
    5. Any failure rolls back both the source transaction and the event.

    A Draft event is never counted as a completed payout. Only Submitted
    events are included in cumulative payout calculations.

    Args:
        event_name: Name of the Commission Payout Event to submit.
        source_validation_fn: Optional callable that validates the source
            transaction. Must raise on failure. Called before submission.

    Returns:
        The event name (unchanged).

    Raises:
        frappe.ValidationError on validation failure.
    """
    if not event_name:
        frappe.throw(_("Event name is required."))

    doc = frappe.get_doc("Commission Payout Event", event_name)

    if doc.docstatus != 0:
        if doc.docstatus == 1:
            # Already submitted - idempotent, return.
            return doc.name
        frappe.throw(_("Cannot submit a cancelled event."))

    # Run source validation if provided.
    if source_validation_fn is not None:
        source_validation_fn(doc)

    # Validate relationships and financials.
    doc.validate()

    # Submit the event (Draft -> Submitted).
    doc.submit()

    return doc.name


def create_and_submit_payout_event(event_data, source_validation_fn=None):
    """Create (or retrieve) and submit a payout event atomically.

    This is the recommended entry point for trusted payout handlers.
    It combines get_or_create_payout_event() with submit_payout_event()
    in a single transaction-safe operation.

    Args:
        event_data: Dict containing event_key and all required financial/source
            fields for the event type.
        source_validation_fn: Optional callable for source transaction validation.

    Returns:
        The event name.

    Raises:
        frappe.ValidationError on validation or submission failure.
    """
    event_name = get_or_create_payout_event(event_data)
    return submit_payout_event(event_name, source_validation_fn)