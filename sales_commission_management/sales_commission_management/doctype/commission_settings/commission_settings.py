import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class CommissionSettings(Document):
    def validate(self):
        self._validate_account_mappings()
        acct = getattr(self, "default_payout_account", None)
        if acct:
            try:
                info = frappe.get_cached_value(
                    "Account", acct, ["company", "account_type", "is_group", "disabled"], as_dict=True
                )
            except Exception:
                info = None
            if (
                not info
                or getattr(info, "account_type", None) not in ("Cash", "Bank")
                or cint(getattr(info, "is_group", 0))
                or cint(getattr(info, "disabled", 0))
            ):
                frappe.throw(
                    _("Default payout account must be an enabled, non-group Cash or Bank account.")
                )

    def _validate_account_mappings(self):
        seen = set()
        for row in (self.account_mappings or []):
            if not cint(getattr(row, "enabled", 1)):
                continue
            company = getattr(row, "company", None)
            if not company:
                continue
            if company in seen:
                frappe.throw(
                    _("Duplicate enabled account mapping for company {0}.").format(company)
                )
            seen.add(company)
