import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class CommissionPeriod(Document):
    def validate(self):
        self._validate_dates()
        if self.status == "Closed":
            self._check_reconciliation()

    def before_submit(self):
        pass

    def _validate_dates(self):
        if self.period_from and self.period_to:
            if frappe.utils.getdate(self.period_from) > frappe.utils.getdate(
                self.period_to
            ):
                frappe.throw(_("Period From must be before Period To."))

    def _check_reconciliation(self):
        """Verify that ledger events and payment allocations are reconciled before closing."""
        # Placeholder: in production, verify no unprocessed source docs and no open exceptions

    def close_period(self, reason=None):
        if self.status == "Closed":
            frappe.throw(_("Period is already closed."))
        self.status = "Closed"
        self.closed_by = frappe.session.user
        self.closed_on = now_datetime()
        self.save(ignore_permissions=True)

    def reopen_period(self, reason=None):
        if not reason:
            frappe.throw(_("A reason is required to reopen a closed period."))
        if self.status != "Closed":
            frappe.throw(_("Only closed periods can be reopened."))
        self.status = "Reopened"
        self.reopened_by = frappe.session.user
        self.reopened_on = now_datetime()
        self.reopen_reason = reason
        self.save(ignore_permissions=True)
