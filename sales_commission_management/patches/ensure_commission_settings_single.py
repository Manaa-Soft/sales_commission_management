"""Data patch: ensure 'Commission Settings' is registered as a single DocType.

On sites created before ``is_single`` was set (or where a manual/migration
drift left ``tabDocType.issingle = 0``), ``frappe.get_single("Commission
Settings")`` raises, which breaks the entire commission engine. Schema sync
does not reliably flip ``issingle`` on an already-existing DocType row, so we
do it explicitly and idempotently here.

This patch is safe to re-run.
"""


import frappe


def execute():
    if not frappe.db.exists("DocType", "Commission Settings"):
        # Nothing to fix; the doctype itself hasn't been synced yet.
        return

    # Only act if the flag is actually wrong (idempotent).
    current = frappe.db.get_value("DocType", "Commission Settings", "issingle")
    if not current:
        frappe.db.set_value("DocType", "Commission Settings", "issingle", 1, update_modified=False)

    # Reload cached meta so the in-process value reflects the change.
    frappe.clear_cache(doctype="Commission Settings")
    print("ensure_commission_settings_single: issingle =", frappe.db.get_value("DocType", "Commission Settings", "issingle"))