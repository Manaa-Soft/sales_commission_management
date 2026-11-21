import frappe
from frappe import _


def generate_event_key(source_doctype, source_name, source_row, settlement, commission_entry, event_type):
    """Generate a deterministic event key for a payout event.

    Format: {source_doctype}|{source_name}|{source_row}|{settlement}|{commission_entry}|{event_type}
    """
    parts = [
        source_doctype or "",
        source_name or "",
        source_row or "",
        settlement or "",
        commission_entry or "",
        event_type or "",
    ]
    return "|".join(parts)


def get_or_create_payout_event(event_key, **kwargs):
    """Retrieve an existing payout event or create a new one.

    Returns the existing event name if found, otherwise creates and returns
    the new event name. Idempotent: calling twice with the same key returns
    the same event.
    """
    existing = frappe.db.get_value("Commission Payout Event", {"event_key": event_key}, "name")
    if existing:
        return existing

    kwargs["event_key"] = event_key
    kwargs["doctype"] = "Commission Payout Event"
    doc = frappe.get_doc(kwargs)
    doc.insert(ignore_permissions=True)
    return doc.name