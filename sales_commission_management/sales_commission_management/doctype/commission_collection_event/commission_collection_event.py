import frappe
from frappe import _
from frappe.model.document import Document


class CommissionCollectionEvent(Document):
    def validate(self):
        # Immutable audit record: once inserted, no field may change.
        if not self.is_new():
            frappe.throw(_("Commission Collection Event records are immutable."))

    def on_trash(self):
        frappe.throw(_("Commission Collection Event records cannot be deleted."))

    def before_save(self):
        # Prevent modification of existing records.
        if not self.is_new():
            frappe.throw(_("Commission Collection Event records are immutable."))