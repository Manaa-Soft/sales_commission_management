"""Data patch: migrate legacy (non-single) Commission Settings rows into the
single record and ensure ``tabDocType.issingle = 1``.

Sites created before ``is_single`` was set (or where a manual/migration drift
left ``tabDocType.issingle = 0``) end up with one or more rows in
``tabCommission Settings`` whose ``name`` is not ``Commission Settings``. The
single DocType, however, is addressed by the name ``Commission Settings``. As a
result ``frappe.get_single("Commission Settings")`` either fails or returns an
empty record, losing the configured accounts, mappings and defaults.

Schema sync does not reliably flip ``issingle`` on an already-existing DocType
row and does not move data between rows, so we do both explicitly and
idempotently here. This patch is safe to re-run.
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
]


def ensure():
    """Idempotent: ensure single + migrate legacy data. Callable from patches
    and from hooks (after_migrate / after_install)."""
    if not frappe.db.exists("DocType", "Commission Settings"):
        return

    doctype = "Commission Settings"
    single_name = doctype

    # 1. Flip issingle on the DocType row.
    frappe.db.set_value("DocType", doctype, "issingle", 1, update_modified=False)
    frappe.clear_cache(doctype=doctype)

    # 2. Make sure the single row exists.
    if not frappe.db.exists(doctype, single_name):
        try:
            frappe.get_doc({"doctype": doctype, "name": single_name}).insert(
                ignore_permissions=True
            )
        except Exception:
            # get_single below will create it if needed.
            pass

    single_doc = frappe.get_single(doctype)

    # 3. Find legacy rows (name != single_name).
    legacy_names = frappe.get_all(
        doctype, filters=[["name", "!=", single_name]], pluck="name"
    )

    mapping_keys = {
        (getattr(m, "company", None), getattr(m, "commission_expense_account", None))
        for m in (getattr(single_doc, "account_mappings", None) or [])
    }

    for lname in legacy_names:
        try:
            ldoc = frappe.get_doc(doctype, lname)
        except Exception:
            continue

        # Copy scalar fields only when the single record is empty.
        for field in SCALAR_FIELDS:
            if getattr(single_doc, field, None) in (None, "", 0) and getattr(
                ldoc, field, None
            ) not in (None, ""):
                single_doc.set(field, ldoc.get(field))

        # Merge account mappings (dedupe by company + expense account).
        for row in (getattr(ldoc, "account_mappings", None) or []):
            key = (
                getattr(row, "company", None),
                getattr(row, "commission_expense_account", None),
            )
            if key in mapping_keys:
                continue
            payload = {}
            for f in MAPPING_FIELDS:
                if hasattr(row, f):
                    payload[f] = getattr(row, f)
            single_doc.append("account_mappings", payload)
            mapping_keys.add(key)

        # Remove the legacy row now that its data has been merged.
        try:
            frappe.delete_doc(doctype, lname, ignore_permissions=True, force=True)
        except Exception:
            pass

    single_doc.save(ignore_permissions=True)
    frappe.db.commit()
    frappe.clear_cache(doctype=doctype)


def execute():
    ensure()
    print(
        "migrate_commission_settings_legacy_data: issingle =",
        frappe.db.get_value("DocType", "Commission Settings", "issingle"),
        "mappings =",
        len(frappe.get_single("Commission Settings").account_mappings or []),
    )
