import frappe
import hashlib
import json
from frappe import _
from frappe.model.document import Document


class CommissionPayoutEvent(Document):
    """Immutable audit record of a commission payout or recovery event."""

    def validate(self):
        # Require event_key.
        if not self.event_key:
            frappe.throw(_("Event Key is required."))

        # Server-side relationship and financial validation.
        self._validate_relationships()
        self._validate_financials()

        # Allow initial creation (new record).
        if self.is_new():
            return

        # Allow Draft -> Submitted transition, but only if no fields have changed.
        if self.docstatus == 1:
            db_docstatus = frappe.db.get_value(self.doctype, self.name, "docstatus")
            if db_docstatus == 0:
                self._verify_no_field_changes()
                return

        # Reject any modification to existing records.
        frappe.throw(_("Commission Payout Event records are immutable."))

    def _validate_relationships(self):
        """Validate relationships between settlement, entry, company, beneficiary."""
        if self.settlement:
            sett = frappe.get_doc("Commission Settlement", self.settlement)
            if self.company and sett.company != self.company:
                frappe.throw(_("Company must match the settlement's company."))
            if self.beneficiary_type and sett.beneficiary_type != self.beneficiary_type:
                frappe.throw(_("Beneficiary type must match the settlement."))
            if self.beneficiary and (sett.sales_person or sett.sales_partner) != self.beneficiary:
                frappe.throw(_("Beneficiary must match the settlement's beneficiary."))
            if self.commission_entry:
                entry = frappe.get_doc("Commission Entry", self.commission_entry)
                if entry.company != sett.company:
                    frappe.throw(_("Commission Entry company must match settlement company."))
                # Verify settlement item belongs to the referenced settlement AND entry.
                if self.settlement_item:
                    item = frappe.get_value(
                        "Commission Settlement Item",
                        self.settlement_item,
                        ["parent", "commission_ledger_entry"],
                        as_dict=True,
                    )
                    if not item:
                        frappe.throw(_("Settlement item not found."))
                    if item.parent != self.settlement:
                        frappe.throw(_("Settlement item does not belong to the referenced settlement."))
                    if item.commission_ledger_entry != self.commission_entry:
                        frappe.throw(_("Settlement item does not match the referenced commission entry."))
        # Reversal validation.
        if self.event_type in ("Payout Reversal", "Recovery Reversal"):
            if not self.reversal_of:
                frappe.throw(_("Reversal events must reference the original event."))
            # Validate original event exists and is submitted.
            orig = frappe.get_doc("Commission Payout Event", self.reversal_of)
            if orig.docstatus != 1:
                frappe.throw(_("Original event must be submitted."))
            if orig.event_type not in ("Payout", "Recovery"):
                frappe.throw(_("Reversal type must match original event type."))
            if orig.company != self.company:
                frappe.throw(_("Company must match original event."))
            if orig.beneficiary != self.beneficiary:
                frappe.throw(_("Beneficiary must match original event."))
            if orig.settlement_currency != self.settlement_currency:
                frappe.throw(_("Settlement currency must match original event."))
            # Check cumulative reversal doesn't exceed original.
            existing_reversals = frappe.get_all(
                "Commission Payout Event",
                filters={
                    "reversal_of": self.reversal_of,
                    "docstatus": 1,
                },
                fields=["commission_amount"],
            )
            total_reversed = sum(flt(r.commission_amount) for r in existing_reversals)
            if total_reversed + flt(self.commission_amount) > flt(orig.commission_amount):
                frappe.throw(_("Cumulative reversal exceeds original event amount."))
        else:
            if self.reversal_of:
                frappe.throw(_("Only reversal events may reference an original event."))

    def _validate_financials(self):
        """Validate financial fields and FX snapshot consistency."""
        import math
        # Each amount must have its currency.
        if self.source_amount and not self.source_currency:
            frappe.throw(_("Source currency is required when source amount is set."))
        if self.settlement_amount and not self.settlement_currency:
            frappe.throw(_("Settlement currency is required when settlement amount is set."))
        if self.commission_amount and not self.commission_currency:
            frappe.throw(_("Commission currency is required when commission amount is set."))
        if self.company_amount and not self.company_currency:
            frappe.throw(_("Company currency is required when company amount is set."))
        # FX snapshot consistency.
        if self.fx_rate is not None and self.fx_rate != 0:
            if not self.fx_from_currency or not self.fx_to_currency:
                frappe.throw(_("FX from/to currencies are required when FX rate is set."))
            if not math.isfinite(self.fx_rate) or self.fx_rate <= 0:
                frappe.throw(_("FX rate must be a finite positive number."))
        if self.fx_from_currency and self.fx_to_currency and self.fx_from_currency == self.fx_to_currency:
            if self.fx_rate and self.fx_rate != 1.0:
                frappe.throw(_("FX rate must be 1.0 when from and to currencies are the same."))

    def _verify_no_field_changes(self):
        """Verify that no fields changed during submission."""
        db_values = frappe.db.get_value(
            self.doctype,
            self.name,
            [
                "event_key", "event_type", "posting_date", "evidence_level",
                "settlement", "settlement_item", "commission_entry",
                "company", "beneficiary_type", "beneficiary",
                "source_doctype", "source_name", "source_row",
                "reversal_of", "source_currency", "source_amount",
                "settlement_currency", "settlement_amount",
                "commission_currency", "commission_amount",
                "company_currency", "company_amount",
                "fx_from_currency", "fx_to_currency", "fx_rate",
                "fx_date", "fx_source", "remarks", "evidence_attachment",
                "naming_series",
            ],
            as_dict=True,
        )
        if not db_values:
            return
        for field in db_values:
            if getattr(self, field, None) != db_values.get(field):
                frappe.throw(
                    _("Commission Payout Event field '{0}' cannot be changed after creation.").format(field)
                )

    def before_save(self):
        if not self.is_new():
            db_docstatus = frappe.db.get_value(self.doctype, self.name, "docstatus")
            if db_docstatus == 0 and self.docstatus == 1:
                return
            frappe.throw(_("Commission Payout Event records are immutable."))

    def on_trash(self):
        frappe.throw(_("Commission Payout Event records cannot be deleted."))

    def on_cancel(self):
        frappe.throw(_("Commission Payout Event records cannot be cancelled directly. Create a reversal event instead."))