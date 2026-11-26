import frappe
import hashlib
import json
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt
from sales_commission_management.sales_commission_management.utils import currency_precision as _currency_precision


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
            # Settlement must be submitted.
            if sett.docstatus != 1:
                frappe.throw(_("Settlement must be submitted."))
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
                # Verify beneficiary matches entry.
                entry_beneficiary = entry.sales_person or entry.sales_partner
                if entry_beneficiary and self.beneficiary and entry_beneficiary != self.beneficiary:
                    frappe.throw(_("Beneficiary must match the commission entry's beneficiary."))
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
                else:
                    # Require settlement_item for item-level payouts.
                    frappe.throw(_("Settlement item is required for payout events."))
        # Recovery validation.
        if self.event_type == "Recovery":
            if not self.recovery_of:
                frappe.throw(_("Recovery events must reference the original payout event."))
            # Lock the original payout event row.
            orig = frappe.db.sql(
                """
                SELECT *
                FROM `tabCommission Payout Event`
                WHERE name = %s
                FOR UPDATE
                """,
                (self.recovery_of,),
                as_dict=True,
            )
            if not orig:
                frappe.throw(_("Original payout event not found."))
            orig = orig[0]
            if orig.docstatus != 1:
                frappe.throw(_("Original payout event must be submitted."))
            if orig.event_type != "Payout":
                frappe.throw(_("Recovery must reference a Payout event."))
            if orig.company != self.company:
                frappe.throw(_("Company must match original payout event."))
            if orig.beneficiary != self.beneficiary:
                frappe.throw(_("Beneficiary must match original payout event."))
            if orig.commission_entry != self.commission_entry:
                frappe.throw(_("Commission entry must match original payout event."))
            if orig.settlement != self.settlement:
                frappe.throw(_("Settlement must match original payout event."))
            if orig.settlement_item != self.settlement_item:
                frappe.throw(_("Settlement item must match original payout event."))
            # Recovery transactions use their own source identity (e.g. a Receive
            # Payment Entry refund), which need not equal the original payout's
            # source (e.g. a Pay Payment Entry). Source identity is validated
            # against the settlement/supplier/company in _validate_financials.
            if orig.commission_currency != self.commission_currency:
                frappe.throw(_("Commission currency must match original payout event."))
            if orig.settlement_currency != self.settlement_currency:
                frappe.throw(_("Settlement currency must match original payout event."))
            if orig.company_currency != self.company_currency:
                frappe.throw(_("Company currency must match original payout event."))
        else:
            if self.recovery_of:
                frappe.throw(_("Only Recovery events may reference a recovery_of event."))

        # Reversal validation.
        if self.event_type in ("Payout Reversal", "Recovery Reversal"):
            if not self.reversal_of:
                frappe.throw(_("Reversal events must reference the original event."))
            # Lock the original event row for concurrency-safe cumulative check.
            orig = frappe.db.sql(
                """
                SELECT *
                FROM `tabCommission Payout Event`
                WHERE name = %s
                FOR UPDATE
                """,
                (self.reversal_of,),
                as_dict=True,
            )
            if not orig:
                frappe.throw(_("Original event not found."))
            orig = orig[0]
            if orig.docstatus != 1:
                frappe.throw(_("Original event must be submitted."))
            # Exact type mapping.
            if self.event_type == "Payout Reversal" and orig.event_type != "Payout":
                frappe.throw(_("Payout Reversal must reference a Payout event."))
            if self.event_type == "Recovery Reversal" and orig.event_type != "Recovery":
                frappe.throw(_("Recovery Reversal must reference a Recovery event."))
            if orig.company != self.company:
                frappe.throw(_("Company must match original event."))
            if orig.beneficiary != self.beneficiary:
                frappe.throw(_("Beneficiary must match original event."))
            if orig.commission_currency != self.commission_currency:
                frappe.throw(_("Commission currency must match original event."))
            if orig.settlement_currency != self.settlement_currency:
                frappe.throw(_("Settlement currency must match original event."))
            if orig.company_currency != self.company_currency:
                frappe.throw(_("Company currency must match original event."))
            # Reversal must reference the same underlying commission entry.
            if orig.commission_entry != self.commission_entry:
                frappe.throw(_("Commission entry must match original event."))
            if orig.settlement != self.settlement:
                frappe.throw(_("Settlement must match original event."))
            if orig.settlement_item != self.settlement_item:
                frappe.throw(_("Settlement item must match original event."))
            # Reversal must preserve the original source identity.
            if orig.source_doctype != self.source_doctype:
                frappe.throw(_("Source DocType must match original event."))
            if orig.source_name != self.source_name:
                frappe.throw(_("Source document must match original event."))
            if orig.source_row != self.source_row:
                frappe.throw(_("Source row must match original event."))
            # Configured accounting precision for all three currencies.
            prec_comm = _currency_precision(self.commission_currency)
            prec_sett = _currency_precision(self.settlement_currency)
            prec_comp = _currency_precision(self.company_currency)

            # Reject zero or negative reversal amounts in all three currencies.
            if flt(self.commission_amount, prec_comm) <= 0:
                frappe.throw(_("Reversal commission amount must be positive."))
            if flt(self.settlement_amount, prec_sett) <= 0:
                frappe.throw(_("Reversal settlement amount must be positive."))
            if flt(self.company_amount, prec_comp) <= 0:
                frappe.throw(_("Reversal company amount must be positive."))

            # Concurrency-safe: use locking read for existing reversals.
            existing_reversals = frappe.db.sql(
                """
                SELECT commission_amount, settlement_amount, company_amount
                FROM `tabCommission Payout Event`
                WHERE reversal_of = %s
                  AND docstatus = 1
                FOR UPDATE
                """,
                (self.reversal_of,),
                as_dict=True,
            )
            total_reversed_comm = sum(flt(r.commission_amount, prec_comm) for r in (existing_reversals or []))
            total_reversed_sett = sum(flt(r.settlement_amount, prec_sett) for r in (existing_reversals or []))
            total_reversed_comp = sum(flt(r.company_amount, prec_comp) for r in (existing_reversals or []))
            if total_reversed_comm + flt(self.commission_amount, prec_comm) > flt(orig.commission_amount, prec_comm):
                frappe.throw(_("Cumulative reversal exceeds original event amount."))
            if total_reversed_sett + flt(self.settlement_amount, prec_sett) > flt(orig.settlement_amount, prec_sett):
                frappe.throw(_("Cumulative reversal exceeds original settlement amount."))
            if total_reversed_comp + flt(self.company_amount, prec_comp) > flt(orig.company_amount, prec_comp):
                frappe.throw(_("Cumulative reversal exceeds original company amount."))
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

        # Positive amount validation for Payout and Recovery events.
        if self.event_type in ("Payout", "Recovery"):
            prec_comm = _currency_precision(self.commission_currency or "USD")
            if flt(self.commission_amount, prec_comm) <= 0:
                frappe.throw(_("Payout and Recovery events must have a positive commission amount."))
            # Company amount must be finite and positive for Recovery events.
            if self.event_type == "Recovery":
                prec_comp = _currency_precision(self.company_currency or "USD")
                if not math.isfinite(flt(self.company_amount)) or flt(self.company_amount, prec_comp) <= 0:
                    frappe.throw(_("Recovery events must have a finite positive company amount."))
                # Validate recovery amount against outstanding recovery.
                if self.commission_entry:
                    from sales_commission_management.sales_commission_management.payout.recovery import (
                        get_commission_recovery_position,
                    )
                    position = get_commission_recovery_position(self.commission_entry)
                    recovery_amount = flt(self.company_amount, prec_comp)
                    # Validate against the referenced payout's recoverable balance.
                    allocs = position.get("payout_allocations", []) or []
                    referenced_alloc = None
                    for a in allocs:
                        if a.get("payout_event") == self.recovery_of:
                            referenced_alloc = a
                            break
                    if referenced_alloc is None:
                        frappe.throw(
                            _("Recovery event must reference a payout belonging to "
                              "Commission Entry {0}.").format(self.commission_entry)
                        )
                    payout_recoverable = flt(
                        referenced_alloc.get("recoverable_balance", 0)
                    )
                    if recovery_amount > payout_recoverable + 0.005:
                        frappe.throw(
                            _("Recovery amount {0} exceeds the recoverable balance "
                              "{1} of payout {2}.").format(
                                recovery_amount, payout_recoverable, self.recovery_of
                            )
                        )
        # Source row validation for Payment Entry events.
        if self.source_doctype == "Payment Entry":
            if not self.source_row:
                frappe.throw(_("Source row is required for Payment Entry events."))
            # Validate source Payment Entry exists and is submitted.
            pe = frappe.get_doc("Payment Entry", self.source_name)
            if pe.docstatus != 1:
                frappe.throw(_("Payment Entry must be submitted."))
            # Payment direction depends on event type.
            if self.event_type == "Recovery":
                if pe.payment_type != "Receive":
                    frappe.throw(_("Payment Entry must be a Receive type for recovery."))
            else:
                if pe.payment_type != "Pay":
                    frappe.throw(_("Payment Entry must be a Pay type."))
            if pe.party_type != "Supplier":
                frappe.throw(_("Payment Entry must be a Supplier payment."))
            # Validate Payment Entry matches settlement company and supplier.
            if self.settlement:
                sett = frappe.get_doc("Commission Settlement", self.settlement)
                if pe.company != sett.company:
                    frappe.throw(_("Payment Entry company must match settlement company."))
                if pe.party != sett.supplier:
                    frappe.throw(_("Payment Entry supplier must match settlement supplier."))
                # Validate Purchase Invoice exists and is submitted.
                sett_doc = sett.settlement_document
                sett_doc_type = sett.settlement_document_type
                if not sett_doc:
                    frappe.throw(_("Settlement must have a submitted Purchase Invoice."))
                if sett_doc_type != "Purchase Invoice":
                    frappe.throw(_("Settlement must be a Supplier Purchase Invoice settlement."))
                pi = frappe.get_doc("Purchase Invoice", sett_doc)
                if pi.docstatus != 1:
                    frappe.throw(_("Purchase Invoice must be submitted."))
                if pi.supplier != sett.supplier:
                    frappe.throw(_("Purchase Invoice supplier must match settlement supplier."))
                if pi.company != sett.company:
                    frappe.throw(_("Purchase Invoice company must match settlement company."))
            # Validate source_row belongs to the source Payment Entry.
            parent = frappe.db.get_value("Payment Entry Reference", self.source_row, "parent")
            if parent != self.source_name:
                frappe.throw(_("Source row does not belong to the referenced Payment Entry."))
            # Validate reference identifies the settlement's Purchase Invoice.
            ref_doctype = frappe.db.get_value("Payment Entry Reference", self.source_row, "reference_doctype")
            ref_name = frappe.db.get_value("Payment Entry Reference", self.source_row, "reference_name")
            if ref_doctype != "Purchase Invoice":
                frappe.throw(_("Source row must reference a Purchase Invoice."))
            if self.settlement:
                sett_doc = frappe.db.get_value("Commission Settlement", self.settlement, "settlement_document")
                if sett_doc and ref_name != sett_doc:
                    frappe.throw(_("Source row must reference the settlement's Purchase Invoice."))
            # Validate that the Payment Entry Reference's allocated amount agrees
            # with the payout event's source amount. Checking only the referenced
            # Purchase Invoice is insufficient to establish the amount actually
            # allocated to this settlement.
            allocated_amount = frappe.db.get_value(
                "Payment Entry Reference", self.source_row, "allocated_amount"
            )
            # Fail closed: a missing allocated amount is not a valid match.
            if allocated_amount is None:
                frappe.throw(
                    _("Source row has no allocated amount.")
                )
            # Fetch the Payment Entry for currency context.
            pe = frappe.get_doc("Payment Entry", self.source_name)
            # Currency alignment: depends on payment direction.
            # Receive: party account is paid_from (supplier refund).
            # Pay: party account is paid_to (commission payout).
            if pe.payment_type == "Receive":
                alloc_currency = getattr(pe, "paid_from_account_currency", None)
                party_account = pe.paid_from
            else:
                alloc_currency = getattr(pe, "paid_to_account_currency", None)
                party_account = pe.paid_to
            if not alloc_currency:
                alloc_currency = frappe.get_cached_value("Account", party_account, "account_currency")
            if not alloc_currency:
                frappe.throw(_("Cannot resolve the Payment Entry Reference allocated currency."))
            if not self.source_currency:
                frappe.throw(_("Source currency is required for Payment Entry events."))
            if alloc_currency != self.source_currency:
                frappe.throw(
                    _("Source currency ({0}) must match the Payment Entry Reference allocated currency ({1}).").format(
                        self.source_currency, alloc_currency
                    )
                )
            precision = _currency_precision(self.source_currency)
            # Item-level payout events are shares of the Payment Entry Reference
            # allocation, not the whole allocation. Validate that the share is
            # positive and does not exceed the reference's allocated amount; the
            # aggregate of all item shares is reconciled by the supplier payment
            # handler (see payout/supplier_payment.py).
            if flt(self.source_amount, precision) <= 0:
                frappe.throw(
                    _("Source amount must be positive.")
                )
            if flt(self.source_amount, precision) > flt(allocated_amount, precision):
                frappe.throw(
                    _("Source amount ({0}) exceeds the Payment Entry Reference allocated amount ({1}).").format(
                        self.source_amount, allocated_amount
                    )
                )
        # FX snapshot consistency.
        if self.fx_from_currency or self.fx_to_currency or self.fx_rate:
            if not self.fx_from_currency or not self.fx_to_currency:
                frappe.throw(_("Both FX from and to currencies are required."))
            if not self.fx_rate:
                frappe.throw(_("FX rate is required when FX currencies are set."))
            if not math.isfinite(self.fx_rate) or self.fx_rate <= 0:
                frappe.throw(_("FX rate must be a finite positive number."))
        if self.fx_from_currency and self.fx_to_currency and self.fx_from_currency == self.fx_to_currency:
            if self.fx_rate and self.fx_rate != 1.0:
                frappe.throw(_("FX rate must be 1.0 when from and to currencies are the same."))
        # Payment FX snapshot consistency.
        if self.payment_fx_from_currency or self.payment_fx_to_currency or self.payment_fx_rate:
            if not self.payment_fx_from_currency or not self.payment_fx_to_currency:
                frappe.throw(_("Both payment FX from and to currencies are required."))
            if not self.payment_fx_rate:
                frappe.throw(_("Payment FX rate is required when payment FX currencies are set."))
            if not math.isfinite(self.payment_fx_rate) or self.payment_fx_rate <= 0:
                frappe.throw(_("Payment FX rate must be a finite positive number."))
            if self.payment_fx_from_currency == self.payment_fx_to_currency:
                if self.payment_fx_rate and self.payment_fx_rate != 1.0:
                    frappe.throw(_("Payment FX rate must be 1.0 when from and to currencies are the same."))

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
                "reversal_of", "recovery_of", "source_currency", "source_amount",
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

    def on_submit(self):
        # Refresh the settlement's server-derived payout status/amounts so the
        # form and reports reflect the new payout/recovery event.
        if getattr(self, "settlement", None):
            try:
                from sales_commission_management.sales_commission_management.payout.status import (
                    refresh_settlement_payout_status,
                )

                refresh_settlement_payout_status(self.settlement)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "Commission Payout Event: status refresh failed for {0}".format(
                        self.settlement
                    ),
                )
                raise

    def on_trash(self):
        frappe.throw(_("Commission Payout Event records cannot be deleted."))

    def on_cancel(self):
        frappe.throw(_("Commission Payout Event records cannot be cancelled directly. Create a reversal event instead."))