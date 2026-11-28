"""Data patch: ensure app-owned ``commission_settlement`` Link fields exist on
Payment Entry, Journal Entry, Purchase Invoice, and Additional Salary.

Idempotent: only creates fields that are missing. Never overwrites existing
field definitions. Installation failures propagate (fail closed).
"""

import frappe


def _ensure_field(doctype, fieldname, label, options, read_only=1):
    if not frappe.db.exists("DocType", doctype):
        return
    if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
        return
    cf = frappe.new_doc("Custom Field")
    cf.dt = doctype
    cf.fieldname = fieldname
    cf.label = label
    cf.fieldtype = "Link"
    cf.options = options
    cf.read_only = read_only
    cf.insert(ignore_permissions=True)
    frappe.clear_cache(doctype=doctype)


def execute():
    _ensure_field("Payment Entry", "commission_settlement", "Commission Settlement", "Commission Settlement")
    _ensure_field("Journal Entry", "commission_settlement", "Commission Settlement", "Commission Settlement")
    # Purchase Invoice and Additional Salary only when the DocType is installed.
    _ensure_field("Purchase Invoice", "commission_settlement", "Commission Settlement", "Commission Settlement")
    if frappe.db.exists("DocType", "Additional Salary"):
        _ensure_field("Additional Salary", "commission_settlement", "Commission Settlement", "Commission Settlement")
    print("ensure_commission_settlement_link_fields: done")