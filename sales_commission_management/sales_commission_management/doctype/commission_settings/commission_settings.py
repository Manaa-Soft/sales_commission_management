import frappe
from frappe import _
from frappe.model.document import Document


class CommissionSettings(Document):
    def validate(self):
        self._validate_account_mappings()

    def _validate_account_mappings(self):
        seen = set()
        for row in (self.account_mappings or []):
            if not getattr(row, "enabled", 1):
                continue
            company = getattr(row, "company", None)
            if not company:
                continue
            if company in seen:
                frappe.throw(
                    _("Duplicate enabled account mapping for company {0}.").format(company)
                )
            seen.add(company)
