import frappe
from frappe import _
from frappe.model.document import Document


class CommissionPayoutEvent(Document):
    """Immutable audit record of a commission payout or recovery event."""

    def validate(self):
        # Immutable once created.
        if not self.is_new():
            frappe.throw(_("Commission Payout Event records are immutable."))
        # Require event_key.
        if not self.event_key:
            frappe.throw(_("Event Key is required."))

    def before_save(self):
        if not self.is_new():
            frappe.throw(_("Commission Payout Event records are immutable."))

    def on_trash(self):
        frappe.throw(_("Commission Payout Event records cannot be deleted."))

    def on_cancel(self):
        frappe.throw(_("Commission Payout Event records cannot be cancelled directly. Create a reversal event instead."))