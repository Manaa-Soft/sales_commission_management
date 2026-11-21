import frappe
import hashlib
import json
from frappe import _


def generate_event_key(source_doctype, source_name, source_row, settlement, settlement_item, commission_entry, event_type, reversal_of=None):
    """Generate a deterministic, fixed-length event key for a payout event.

    Uses SHA-256 over a canonical JSON serialization of identity fields.
    This produces a fixed-length key that avoids delimiter ambiguity
    and collisions from variable-length document names.
    """
    identity = {
        "source_doctype": source_doctype or "",
        "source_name": source_name or "",
        "source_row": source_row or "",
        "settlement": settlement or "",
        "settlement_item": settlement_item or "",
        "commission_entry": commission_entry or "",
        "event_type": event_type or "",
        "reversal_of": reversal_of or "",
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_or_create_payout_event(event_key, payload=None, **kwargs):
    """Retrieve an existing payout event or create a new one.

    Idempotent: calling twice with the same key returns the same event.
    Handles concurrent inserts by catching DuplicateEntryError and
    retrieving the existing event. Verifies that the existing event's
    payload matches the requested payload (rejects mismatched data).
    """
    existing = frappe.db.get_value("Commission Payout Event", {"event_key": event_key}, "name")
    if existing:
        if payload:
            _verify_payload_match(existing, payload)
        return existing

    kwargs["event_key"] = event_key
    kwargs["doctype"] = "Commission Payout Event"
    try:
        doc = frappe.get_doc(kwargs)
        doc.insert(ignore_permissions=True)
        return doc.name
    except frappe.DuplicateEntryError:
        # Race condition: another worker inserted first. Retrieve it.
        existing = frappe.db.get_value("Commission Payout Event", {"event_key": event_key}, "name")
        if existing:
            if payload:
                _verify_payload_match(existing, payload)
            return existing
        raise


def _verify_payload_match(existing_name, payload):
    """Verify that an existing event's payload matches the requested payload.

    Raises if any financial or source field differs.
    """
    db_values = frappe.db.get_value(
        "Commission Payout Event",
        existing_name,
        list(payload.keys()),
        as_dict=True,
    )
    if not db_values:
        return
    for field, requested_value in payload.items():
        db_value = db_values.get(field)
        if db_value != requested_value:
            frappe.throw(
                _("Payout event {0} already exists with different {1} (existing: {2}, requested: {3}).").format(
                    existing_name, field, db_value, requested_value
                )
            )