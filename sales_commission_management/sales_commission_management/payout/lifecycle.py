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
    2. This function retrieves an existing Draft event by name. It does NOT
       create an event; creation is performed by
       ``create_and_submit_payout_event()`` / ``get_or_create_payout_event()``.
    3. It runs the optional source validation callable.
    4. It submits the event (Draft -> Submitted) using trusted server-side
       authorization, because ordinary roles have no submit permission on
       Commission Payout Event.
    5. Any failure propagates to the caller. This helper does not commit or
       roll back the transaction itself; rollback is the responsibility of
       the surrounding Frappe transaction (e.g. the Payment Entry handler),
       which must propagate exceptions and avoid swallowing failures.
    6. A Draft event is never counted as a completed payout. Only Submitted
       events are included in cumulative payout calculations.

    Idempotency / retry behavior:
    - An already-Submitted event is treated as success and its name returned.
    - A Cancelled event raises ``frappe.ValidationError``.
    - A missing event raises (the document does not exist).

    Args:
        event_name: Name of an existing Commission Payout Event to submit.
        source_validation_fn: Optional callable that validates the source
            transaction. Must raise on failure. Called before submission.
            For new Payout events, callers should supply a validation that
            verifies the source Payment Entry Reference's allocated amount
            agrees with the event's source amount.

    Returns:
        The event name (unchanged).

    Raises:
        frappe.ValidationError on validation or submission failure.
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

    # Submit the event (Draft -> Submitted). Frappe's submit() runs
    # validation during the Draft-to-Submitted transition, so an explicit
    # validate() call here is both unnecessary and incorrect (it would
    # reject a Draft whose docstatus is still 0).
    #
    # Trusted server-side authorization: ordinary roles have no write/submit
    # permission on Commission Payout Event. We elevate only for this submit
    # and restore immediately, keeping direct user permissions restricted.
    doc.flags.ignore_permissions = True
    frappe.flags.ignore_permissions = True
    try:
        doc.submit()
    finally:
        doc.flags.ignore_permissions = False
        frappe.flags.ignore_permissions = False

    return doc.name


def create_and_submit_payout_event(event_data, source_validation_fn=None):
    """Create (or retrieve) and submit a payout event atomically.

    This is the recommended entry point for trusted payout handlers.
    It combines get_or_create_payout_event() with submit_payout_event()
    in a single transaction-safe operation. Creation (or idempotent
    retrieval) happens in ``get_or_create_payout_event()``; submission
    happens in ``submit_payout_event()``.

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