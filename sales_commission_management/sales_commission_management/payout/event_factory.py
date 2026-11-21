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


def get_or_create_payout_event(event_data):
    """Retrieve an existing payout event or create a new one.

    Idempotent: calling twice with the same key returns the same event.
    event_data must be a dict containing at minimum 'event_key' and the
    financial/source fields. The same canonical data is used for both
    insertion and later comparison.

    Handles concurrent inserts by catching DuplicateEntryError and
    retrieving the existing event using a locking read. Verifies that
    the existing event's payload matches the requested payload.
    """
    if not event_data or not isinstance(event_data, dict):
        frappe.throw(_("Event data must be a non-empty dict."))
    if not event_data.get("event_key"):
        frappe.throw(_("Event Key is required in event data."))

    event_key = event_data["event_key"]

    # Canonical payload: all fields except doctype and event_key.
    canonical_payload = {k: v for k, v in event_data.items() if k not in ("doctype", "event_key")}

    existing = frappe.db.get_value("Commission Payout Event", {"event_key": event_key}, "name")
    if existing:
        _verify_payload_match(existing, canonical_payload)
        return existing

    # Build the document from the canonical data.
    doc_data = dict(canonical_payload)
    doc_data["doctype"] = "Commission Payout Event"
    doc_data["event_key"] = event_key
    try:
        doc = frappe.get_doc(doc_data)
        doc.insert(ignore_permissions=True)
        return doc.name
    except frappe.DuplicateEntryError:
        # Race condition: another worker inserted first. Use locking read.
        locked = frappe.db.sql(
            """
            SELECT name
            FROM `tabCommission Payout Event`
            WHERE event_key = %s
            FOR UPDATE
            """,
            (event_key,),
            as_dict=True,
        )
        if locked:
            existing = locked[0].name
            _verify_payload_match(existing, canonical_payload)
            return existing
        raise


def _verify_payload_match(existing_name, payload):
    """Verify that an existing event's payload matches the requested payload.

    Uses a locking read to get current data. Fails closed if the record
    cannot be retrieved.
    """
    locked = frappe.db.sql(
        """
        SELECT *
        FROM `tabCommission Payout Event`
        WHERE name = %s
        FOR UPDATE
        """,
        (existing_name,),
        as_dict=True,
    )
    if not locked:
        frappe.throw(_("Could not retrieve existing payout event {0}.").format(existing_name))

    db_values = locked[0]
    for field, requested_value in payload.items():
        db_value = db_values.get(field)
        if db_value != requested_value:
            frappe.throw(
                _("Payout event {0} already exists with different {1} (existing: {2}, requested: {3}).").format(
                    existing_name, field, db_value, requested_value
                )
            )