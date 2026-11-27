"""Data patch: migrate legacy (non-single) Commission Settings rows into the
single record and ensure ``tabDocType.issingle = 1``.

Sites created before ``issingle`` was set (or where a manual/migration drift
left ``tabDocType.issingle = 0``) end up with one or more rows in
``tabCommission Settings`` whose ``name`` is not ``Commission Settings``. The
single DocType, however, is addressed by the name ``Commission Settings``. As a
result ``frappe.get_single("Commission Settings")`` either fails or returns an
empty record, losing the configured accounts, mappings and defaults.

Schema sync does not reliably flip ``issingle`` on an already-existing DocType
row and does not move data between rows, so we do both explicitly and
idempotently here. This patch is safe to re-run.

Important ordering: legacy rows MUST be read while the DocType is still
table-backed, i.e. BEFORE ``issingle`` is flipped. Once ``issingle = 1`` Frappe
loads the DocType through its Single mechanism and ``Document.load_from_db()``
reads from ``tabSingles`` instead of the requested legacy row, so
``frappe.get_doc(doctype, legacy_name)`` no longer retrieves the original row.
"""

import frappe


SCALAR_FIELDS = [
    "enabled",
    "default_company",
    "engine_ownership",
    "accounting_mode",
    "default_commission_currency",
    "enable_sales_invoice",
    "enable_payment_entry",
    "enable_pos_invoice",
    "enable_delivery_note",
    "block_native_hrms_overlap",
    "allow_manual_adjustments",
    "rounding_policy",
    "default_cost_center",
    "default_salary_component",
    "default_commission_item",
    "default_commission_payable_account",
    "default_accrued_liability_account",
    "default_commission_expense_account",
]

MAPPING_FIELDS = [
    "company",
    "commission_expense_account",
    "accrued_commission_payable_account",
    "commission_payable_account",
    "payroll_commission_expense_account",
    "commission_recovery_receivable_account",
    "enabled",
    "cost_center",
    "default_supplier_payment_term",
]

CHILD_DOCTYPE = "Commission Account Mapping"
SINGLE_NAME = "Commission Settings"


def _table_exists(table_name):
    try:
        return bool(frappe.db.table_exists(table_name))
    except Exception:
        return frappe.db.exists("DocType", table_name.lstrip("tab")) is not None


def _read_legacy_rows():
    """Read all legacy (non-single) Commission Settings rows while the DocType
    is still table-backed. Returns a list of dicts (each row plus its child
    mapping rows under ``account_mappings``)."""
    if not frappe.db.table_exists("Commission Settings"):
        return []

    rows = frappe.db.sql(
        "SELECT * FROM `tabCommission Settings`",
        as_dict=True,
    ) or []

    legacy = []
    for row in rows:
        name = row.get("name")
        mappings = []
        if name and frappe.db.table_exists(CHILD_DOCTYPE):
            mappings = frappe.db.sql(
                "SELECT * FROM `tabCommission Account Mapping` WHERE parent = %s",
                (name,),
                as_dict=True,
            ) or []
        legacy.append({"row": row, "mappings": mappings})
    return legacy


def _read_singleton_mappings():
    if not frappe.db.table_exists(CHILD_DOCTYPE):
        return []
    return frappe.db.sql(
        "SELECT * FROM `tabCommission Account Mapping` WHERE parent = %s",
        (SINGLE_NAME,),
        as_dict=True,
    ) or []


def _persisted_singleton_dict():
    return frappe.db.get_singles_dict("Commission Settings", cast=True) or {}


def _scalar_value(row, field):
    if not row:
        return None
    return row.get(field)


def _mapping_payload(mapping_row):
    payload = {}
    for f in MAPPING_FIELDS:
        if isinstance(mapping_row, dict):
            if f in mapping_row:
                payload[f] = mapping_row.get(f)
        else:
            if hasattr(mapping_row, f):
                payload[f] = getattr(mapping_row, f)
    return payload


def _mapping_key(mapping_row):
    return (
        mapping_row.get("company"),
        mapping_row.get("commission_expense_account"),
    )


def ensure():
    """Idempotent: ensure single + migrate legacy data. Callable from patches
    and from hooks (after_migrate / after_install)."""
    if not frappe.db.exists("DocType", "Commission Settings"):
        return

    doctype = "Commission Settings"

    # 1. Read legacy rows and their child mappings BEFORE flipping issingle.
    #    (See module docstring for why this ordering is mandatory.)
    legacy_rows = _read_legacy_rows()
    persisted = _persisted_singleton_dict()
    has_persisted_singleton = bool(persisted)

    # Existing singleton child mappings (if any).
    existing_singleton_mappings = _read_singleton_mappings()

    # Conflict policy:
    # - Scalars: if a persisted singleton exists, it is authoritative; legacy
    #   values only fill fields that are truly absent (None/""), so an
    #   intentionally persisted 0/False is preserved. If no persisted singleton
    #   exists, legacy values (including intentional 0/False) take precedence
    #   over DocType defaults.
    # - When multiple legacy rows exist, scalars come from the most recently
    #   modified legacy row; mappings are merged (union, deduped) from ALL
    #   legacy rows plus any existing singleton mappings.
    # - Mappings are deduped by (company, commission_expense_account).
    selected_legacy = None
    if legacy_rows:
        # legacy_rows already in DB order (modified desc not guaranteed); sort.
        legacy_rows.sort(key=lambda r: r["row"].get("modified") or "", reverse=True)
        selected_legacy = legacy_rows[0]["row"]

    # 2. Flip issingle on the DocType row.
    frappe.db.set_value("DocType", doctype, "issingle", 1, update_modified=False)
    frappe.clear_cache(doctype=doctype)

    # 3. Make sure the single row exists.
    if not frappe.db.exists(doctype, SINGLE_NAME):
        try:
            frappe.get_doc({"doctype": doctype, "name": SINGLE_NAME}).insert(
                ignore_permissions=True
            )
        except Exception:
            # get_single below will create it if needed.
            pass

    single_doc = frappe.get_single(doctype)

    # Track expected final scalar values for verification (field -> value).
    expected_scalars = {}

    # 4. Apply scalar fields per conflict policy.
    if not has_persisted_singleton:
        # No persisted singleton: take legacy values, including 0/False.
        for field in SCALAR_FIELDS:
            if selected_legacy is not None:
                leg_val = _scalar_value(selected_legacy, field)
                if leg_val is not None:
                    single_doc.set(field, leg_val)
                    expected_scalars[field] = leg_val
    else:
        # Persisted singleton authoritative; only fill genuinely missing fields.
        for field in SCALAR_FIELDS:
            if persisted.get(field) in (None, ""):
                if selected_legacy is not None:
                    leg_val = _scalar_value(selected_legacy, field)
                    if leg_val not in (None, ""):
                        single_doc.set(field, leg_val)
                        expected_scalars[field] = leg_val
            else:
                expected_scalars[field] = persisted.get(field)

    # 5. Merge account mappings (dedupe by company + expense account); merge
    #    missing values instead of skipping conflicting mappings.
    expected_mappings = [
        _mapping_payload(m) for m in (getattr(single_doc, "account_mappings", None) or [])
    ]
    mapping_keys = {_mapping_key(m) for m in expected_mappings}

    for legacy in legacy_rows:
        for row in (legacy.get("mappings") or []):
            key = _mapping_key(row)
            payload = _mapping_payload(row)
            existing = None
            for em in expected_mappings:
                if _mapping_key(em) == key:
                    existing = em
                    break
            if existing is not None:
                # Merge missing values from legacy into the existing mapping.
                for f in MAPPING_FIELDS:
                    if existing.get(f) in (None, "") and payload.get(f) not in (None, ""):
                        existing[f] = payload.get(f)
                # Also update the live child doc on single_doc.
                for m in (getattr(single_doc, "account_mappings", None) or []):
                    if _mapping_key(m) == key:
                        for f in MAPPING_FIELDS:
                            if getattr(m, f, None) in (None, "") and payload.get(f) not in (None, ""):
                                try:
                                    setattr(m, f, payload.get(f))
                                except Exception:
                                    pass
                        break
            else:
                single_doc.append("account_mappings", payload)
                expected_mappings.append(dict(payload))
                mapping_keys.add(key)

    single_doc.save(ignore_permissions=True)

    # 6. Verify persisted values and mapping field values.
    persisted_after = _persisted_singleton_dict()
    for field, exp in expected_scalars.items():
        if persisted_after.get(field) != exp:
            frappe.throw(
                "Commission Settings migration verification failed for "
                "field '{0}': expected {1}, got {2}.".format(
                    field, exp, persisted_after.get(field)
                )
            )

    persisted_mappings = _read_singleton_mappings()
    # Verify each expected mapping is present with all non-empty field values.
    for exp in expected_mappings:
        key = _mapping_key(exp)
        match = None
        for pm in persisted_mappings:
            if _mapping_key(pm) == key:
                match = pm
                break
        if match is None:
            frappe.throw(
                "Commission Settings migration verification failed: account "
                "mapping {0} not found after migration.".format(key)
            )
        for f in MAPPING_FIELDS:
            if exp.get(f) not in (None, ""):
                if match.get(f) != exp.get(f):
                    frappe.throw(
                        "Commission Settings migration verification failed for "
                        "account mapping {0} field '{1}': expected {2}, got {3}.".format(
                            key, f, exp.get(f), match.get(f)
                        )
                    )

    expected_mapping_count = len(expected_mappings)
    actual_mapping_count = len(persisted_mappings)
    if actual_mapping_count != expected_mapping_count:
        frappe.throw(
            "Commission Settings migration verification failed: expected "
            "{0} account mappings, found {1}.".format(
                expected_mapping_count, actual_mapping_count
            )
        )

    # 7. Remove legacy records only after successful migration+verification.
    #    Use raw SQL because the DocType is now Single and frappe.delete_doc
    #    would route through the Single mechanism, making deletion unreliable.
    #    Failures propagate via the remaining-count check below.
    for legacy in legacy_rows:
        lname = legacy["row"].get("name")
        if not lname:
            continue
        if lname == SINGLE_NAME:
            # Preserve child mapping rows: they now belong to the Single DocType.
            # Only delete the stale same-name parent row so it is not re-read as
            # legacy data on a subsequent migration run.
            frappe.db.sql(
                "DELETE FROM `tabCommission Settings` WHERE name = %s",
                (lname,),
            )
            continue
        if frappe.db.table_exists(CHILD_DOCTYPE):
            frappe.db.sql(
                "DELETE FROM `tabCommission Account Mapping` WHERE parent = %s",
                (lname,),
            )
        frappe.db.sql(
            "DELETE FROM `tabCommission Settings` WHERE name = %s",
            (lname,),
        )

    # Verify no rows remain in the legacy parent table at all.
    remaining = frappe.db.sql(
        "SELECT COUNT(*) AS c FROM `tabCommission Settings`"
    )
    remaining_count = (remaining[0][0] if remaining else 0)
    if remaining_count:
        frappe.throw(
            "Commission Settings migration failed to remove {0} legacy row(s).".format(
                remaining_count
            )
        )

    frappe.clear_cache(doctype=doctype)


def execute():
    ensure()
    print(
        "migrate_commission_settings_legacy_data: issingle =",
        frappe.db.get_value("DocType", "Commission Settings", "issingle"),
        "mappings =",
        len(frappe.get_single("Commission Settings").account_mappings or []),
    )
