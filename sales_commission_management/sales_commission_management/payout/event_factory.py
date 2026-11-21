import frappe
import hashlib
import json
import time
from frappe import _

# Required payload fields by event type.
# Base fields required for all event types.
REQUIRED_BASE_FIELDS = {
    "event_type", "posting_date", "evidence_level",
    "settlement", "commission_entry",
    "company", "beneficiary_type", "beneficiary",
    "source_doctype", "source_name",
    "settlement_currency", "settlement_amount",
    "commission_currency", "commission_amount",
    "company_currency", "company_amount",
}

# Payout events require source row (for Payment Entry matching).
REQUIRED_PAYOUT_FIELDS = REQUIRED_BASE_FIELDS | {"source_row", "source_currency", "source_amount", "settlement_item"}

# Reversal events require original event reference and source identity.
REQUIRED_REVERSAL_FIELDS = REQUIRED_BASE_FIELDS | {"reversal_of", "source_row", "source_currency", "source_amount"}

# Recovery events require source row.
REQUIRED_RECOVERY_FIELDS = REQUIRED_BASE_FIELDS | {"source_row", "source_currency", "source_amount", "settlement_item"}

# Recovery Reversal events require original event reference and source identity.
REQUIRED_RECOVERY_REVERSAL_FIELDS = REQUIRED_BASE_FIELDS | {"reversal_of", "source_row", "source_currency", "source_amount"}

# Manual Adjustment events: source document optional (no source_row required).
REQUIRED_MANUAL_ADJUSTMENT_FIELDS = REQUIRED_BASE_FIELDS - {"source_doctype", "source_name", "source_row"}


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


def _normalize_value(fieldname, value):
    """Normalize a value for comparison based on the DocType field type.

    Database values (dates, decimals) may have different Python
    representations than the values supplied by the caller. This
    function normalizes both sides to a consistent representation.
    """
    if value is None:
        return None
    try:
        field = frappe.get_meta("Commission Payout Event").get_field(fieldname)
    except Exception:
        field = None
    ft = getattr(field, "fieldtype", None) if field else None

    if ft in ("Date",):
        from frappe.utils import getdate
        if isinstance(value, str):
            try:
                return str(getdate(value))
            except Exception:
                return value
        return str(value) if value else None
    if ft in ("Datetime",):
        from frappe.utils import get_datetime
        if isinstance(value, str):
            try:
                return str(get_datetime(value))
            except Exception:
                return value
        return str(value) if value else None
    if ft == "Currency":
        from frappe.utils import flt
        from sales_commission_management.sales_commission_management.utils import currency_precision
        return flt(value, currency_precision())
    if ft in ("Float", "Percent"):
        from frappe.utils import flt
        precision = getattr(field, "precision", None)
        if precision:
            return flt(value, int(precision))
        return flt(value, 6)
    if ft in ("Int", "Check"):
        return int(value) if value is not None else 0
    if ft == "Link":
        return str(value) if value is not None else None
    return value


def _validate_payload_schema(event_data):
    """Validate that event_data contains all required fields for its event type.

    Rejects unexpected fields and missing required fields.
    """
    event_type = event_data.get("event_type", "")
    if event_type == "Payout":
        required = REQUIRED_PAYOUT_FIELDS
    elif event_type == "Payout Reversal":
        required = REQUIRED_REVERSAL_FIELDS
    elif event_type == "Recovery":
        required = REQUIRED_RECOVERY_FIELDS
    elif event_type == "Recovery Reversal":
        required = REQUIRED_RECOVERY_REVERSAL_FIELDS
    elif event_type == "Manual Adjustment":
        required = REQUIRED_MANUAL_ADJUSTMENT_FIELDS
    else:
        frappe.throw(_("Unsupported event type: {0}").format(event_type))

    missing = required - set(event_data.keys())
    if missing:
        frappe.throw(_("Missing required payload fields: {0}").format(", ".join(sorted(missing))))

    # Reject unexpected fields (only allow known DocType fields + event_key).
    meta = frappe.get_meta("Commission Payout Event")
    known_fields = {f.fieldname for f in meta.fields} | {"event_key", "doctype"}
    unexpected = set(event_data.keys()) - known_fields
    if unexpected:
        frappe.throw(_("Unexpected payload fields: {0}").format(", ".join(sorted(unexpected))))


def get_or_create_payout_event(event_data):
    """Retrieve an existing payout event or create a new one.

    Idempotent: calling twice with the same key returns the same event.
    event_data must be a dict containing at minimum 'event_key' and all
    required financial/source fields for the event type.

    Handles concurrent inserts by catching DuplicateEntryError and
    retrieving the existing event using a locking read. Verifies that
    the existing event's payload matches the requested payload.
    """
    if not event_data or not isinstance(event_data, dict):
        frappe.throw(_("Event data must be a non-empty dict."))
    if not event_data.get("event_key"):
        frappe.throw(_("Event Key is required in event data."))

    # Validate payload schema.
    _validate_payload_schema(event_data)

    event_key = event_data["event_key"]

    # Canonical payload: all fields except doctype and event_key.
    canonical_payload = {k: v for k, v in event_data.items() if k not in ("doctype", "event_key")}

    # Build the document from the canonical data.
    doc_data = dict(canonical_payload)
    doc_data["doctype"] = "Commission Payout Event"
    doc_data["event_key"] = event_key

    # Full retry loop covering both lookup and insertion.
    for attempt in range(3):
        try:
            # Locking read for existing event.
            locked = frappe.db.sql(
                """
                SELECT name
                FROM 	abCommission Payout Event
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

            # No existing event: attempt insertion.
            doc = frappe.get_doc(doc_data)
            doc.insert(ignore_permissions=True)
            return doc.name
        except Exception as e:
            # Check if it's a duplicate entry or contention error.
            is_dup = isinstance(e, frappe.DuplicateEntryError)
            is_contention = False
            pending = [e]
            seen = set()
            while pending:
                current = pending.pop()
                if not isinstance(current, BaseException) or id(current) in seen:
                    continue
                seen.add(id(current))
                args = getattr(current, "args", ()) or ()
                if args and args[0] in (1205, 1213, "1205", "1213"):
                    is_contention = True
                    break
                if args and isinstance(args[0], BaseException):
                    pending.append(args[0])
                for attr in ("orig", "__cause__", "__context__"):
                    nested = getattr(current, attr, None)
                    if isinstance(nested, BaseException):
                        pending.append(nested)

            if is_dup or is_contention:
                # Race condition: another worker inserted first. Use locking read.
                locked = frappe.db.sql(
                    """
                    SELECT name
                FROM 	abCommission Payout Event
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
                if attempt < 2:
                    time.sleep(0.1)
                    continue
                frappe.throw(_("Could not retrieve existing payout event for key {0}.").format(event_key))
            else:
                raise

def _verify_payload_match(existing_name, payload):
    """Verify that an existing event's payload matches the requested payload.

    Uses a locking read to get current data. Fails closed if the record
    cannot be retrieved.
    """
    locked = frappe.db.sql(
        """
        SELECT *
                FROM 	abCommission Payout Event
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
        if _normalize_value(field, db_value) != _normalize_value(field, requested_value):
            frappe.throw(
                _("Payout event {0} already exists with different {1} (existing: {2}, requested: {3}).").format(
                    existing_name, field, db_value, requested_value
                )
            )