import frappe
import hashlib
import json
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt
from sales_commission_management.sales_commission_management.utils import currency_precision as _currency_precision


def _expected_party_type(party):
    """Resolve the actual ERPNext party type for a beneficiary name.

    Returns the DocType name whose record ``party`` refers to, or None when
    it cannot be determined.
    """
    if not party:
        return None
    for doctype, type_name in (
        ("Employee", "Employee"),
        ("Supplier", "Supplier"),
        ("Customer", "Customer"),
        ("Sales Person", None),
    ):
        try:
            if frappe.db.exists(doctype, party):
                return type_name if type_name else None
        except Exception:
            continue
    return None


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
            if not self.source_name:
                frappe.throw(_("Source document is required for Payment Entry events."))
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

            # Determine workflow: Manual Approved Route vs automatic Supplier PI.
            is_manual = False
            sett = None
            if self.settlement:
                sett = frappe.get_doc("Commission Settlement", self.settlement)
                if getattr(sett, "settlement_method", None) == "Manual Approved Route":
                    is_manual = True

            if is_manual:
                # Manual Approved Route: no Supplier Purchase Invoice and no
                # Payment Entry Reference row required. source_row is a
                # Commission Settlement Item id.
                if not self.source_row:
                    frappe.throw(_("Source row is required for Payment Entry events."))
                # source_row must be a Commission Settlement Item of this settlement.
                item = frappe.db.get_value(
                    "Commission Settlement Item", self.source_row,
                    ["parent", "commission_ledger_entry"], as_dict=True,
                )
                if not item:
                    frappe.throw(_("Source row is not a valid Commission Settlement Item."))
                if item.parent != self.settlement:
                    frappe.throw(_("Source row does not belong to the referenced settlement."))
                if self.commission_entry and item.commission_ledger_entry != self.commission_entry:
                    frappe.throw(_("Source row does not match the referenced commission entry."))
                # Company check.
                if pe.company != sett.company:
                    frappe.throw(_("Payment Entry company must match settlement company."))
                # Account validation (fail closed).
                from sales_commission_management.sales_commission_management.payout.manual_payment import _resolve_payable_account
                payable = _resolve_payable_account(pe.company)
                _payable_company = frappe.get_cached_value("Account", payable, "company")
                if _payable_company and pe.company and _payable_company != pe.company:
                    frappe.throw(_("Commission payable account does not belong to the company."))
                if not getattr(pe, "paid_to", None):
                    frappe.throw(_("Payment Entry paid_to account is required."))
                if pe.paid_to != payable:
                    frappe.throw(_("Payment Entry paid_to must be the Commission Payable account."))
                if not getattr(pe, "paid_from", None):
                    frappe.throw(_("Payment Entry paid_from account is required."))
                try:
                    _acct_type = frappe.get_cached_value("Account", pe.paid_from, "account_type")
                    _acct_company = frappe.get_cached_value("Account", pe.paid_from, "company")
                except Exception:
                    _acct_type = None
                    _acct_company = None
                if _acct_company and pe.company and _acct_company != pe.company:
                    frappe.throw(_("Payment Entry paid_from account does not belong to the company."))
                if _acct_type not in ("Cash", "Bank"):
                    frappe.throw(_("Payment Entry paid_from must be a Cash or Bank account."))
                # Independently verify the Commission Payable account currency.
                actual_paid_to_currency = None
                try:
                    actual_paid_to_currency = frappe.get_cached_value("Account", pe.paid_to, "account_currency")
                except Exception:
                    actual_paid_to_currency = None
                if not actual_paid_to_currency:
                    frappe.throw(_("Cannot resolve Commission Payable account currency."))
                paid_to_currency = getattr(pe, "paid_to_account_currency", None)
                if paid_to_currency and actual_paid_to_currency != paid_to_currency:
                    frappe.throw(_("Payment Entry beneficiary account currency does not match its Account."))
                _comm_curr = self.commission_currency or self.source_currency
                if _comm_curr and actual_paid_to_currency != _comm_curr:
                    frappe.throw(_("Commission Payable account currency must match commission currency."))
                # Canonical beneficiary enforcement (no legacy alias fallback).
                from sales_commission_management.sales_commission_management.payout.manual_payment import (
                    resolve_commission_accounting_beneficiary,
                )
                resolved = resolve_commission_accounting_beneficiary(sett)
                expected_party = resolved["party"]
                expected_party_type = resolved["party_type"]
                party = getattr(pe, "party", None)
                party_type = getattr(pe, "party_type", None)
                if not party:
                    frappe.throw(_("Payment Entry must identify the beneficiary."))
                if party != expected_party:
                    frappe.throw(_("Incorrect accounting beneficiary."))
                if not party_type:
                    frappe.throw(_("Party type is required when a party is referenced."))
                if party_type != expected_party_type:
                    frappe.throw(_("Incorrect accounting party type."))

                # source_row must match settlement_item (Manual Approved Route).
                if self.source_row and self.settlement_item and self.source_row != self.settlement_item:
                    frappe.throw(_("Source row must match the settlement item."))
                # Require source amount positive.
                prec = _currency_precision(self.source_currency or self.commission_currency)
                if self.source_amount is not None and flt(self.source_amount, prec) <= 0:
                    frappe.throw(_("Source amount must be positive."))
                # Currency alignment (beneficiary side).
                ev_currency = getattr(pe, "paid_to_account_currency", None)
                if not ev_currency:
                    ev_currency = getattr(pe, "currency", None)
                if ev_currency and self.source_currency and ev_currency != self.source_currency:
                    frappe.throw(
                        _("Source currency ({0}) must match the Payment Entry currency ({1}).").format(
                            self.source_currency, ev_currency
                        )
                    )
                # Beneficiary-side Party Type must match the referenced party's
                # actual ERPNext party type (not merely be non-empty).
                party = getattr(pe, "party", None)
                party_type = getattr(pe, "party_type", None)
                if not party:
                    frappe.throw(_("Payment Entry must identify the beneficiary."))
                _exp = _expected_party_type(party)
                if _exp is None or party_type != _exp:
                    frappe.throw(_("Payment Entry party type does not match the referenced party."))
            else:
                # Automatic Supplier Purchase Invoice route: retain existing validation.
                if pe.party_type != "Supplier":
                    frappe.throw(_("Payment Entry must be a Supplier payment."))
                if not self.source_row:
                    frappe.throw(_("Source row is required for Payment Entry events."))
                if self.settlement:
                    if pe.company != sett.company:
                        frappe.throw(_("Payment Entry company must match settlement company."))
                    if pe.party != sett.supplier:
                        frappe.throw(_("Payment Entry supplier must match settlement supplier."))
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
                ref_doctype = frappe.db.get_value("Payment Entry Reference", self.source_row, "reference_doctype")
                ref_name = frappe.db.get_value("Payment Entry Reference", self.source_row, "reference_name")
                if ref_doctype != "Purchase Invoice":
                    frappe.throw(_("Source row must reference a Purchase Invoice."))
                if self.settlement:
                    sett_doc = frappe.db.get_value("Commission Settlement", self.settlement, "settlement_document")
                    if sett_doc and ref_name != sett_doc:
                        frappe.throw(_("Source row must reference the settlement's Purchase Invoice."))
                allocated_amount = frappe.db.get_value(
                    "Payment Entry Reference", self.source_row, "allocated_amount"
                )
                if allocated_amount is None:
                    frappe.throw(
                        _("Source row has no allocated amount.")
                    )
                pe = frappe.get_doc("Payment Entry", self.source_name)
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
        # Source document validation for Journal Entry evidence.
        if self.source_doctype == "Journal Entry":
            if not self.source_name:
                frappe.throw(_("Source document is required for Journal Entry events."))
            je = frappe.get_doc("Journal Entry", self.source_name)
            if je.docstatus != 1:
                frappe.throw(_("Journal Entry must be submitted."))
            if self.company and je.company != self.company:
                frappe.throw(_("Journal Entry company must match event company."))
            if self.posting_date and getattr(je, "posting_date", None) and str(je.posting_date) != str(self.posting_date):
                frappe.throw(_("Journal Entry date does not match posting date."))
            # Manual Approved Route enforcement for Journal Entry evidence.
            if self.settlement:
                sett = frappe.get_doc("Commission Settlement", self.settlement)
                if getattr(sett, "settlement_method", None) != "Manual Approved Route":
                    frappe.throw(_("Journal Entry manual payout requires a Manual Approved Route settlement."))
            if not self.source_row:
                frappe.throw(_("Source row is required for Journal Entry events."))
            if self.settlement:
                item = frappe.db.get_value(
                    "Commission Settlement Item", self.source_row,
                    ["parent", "commission_ledger_entry"], as_dict=True,
                )
                if not item:
                    frappe.throw(_("Source row is not a valid Commission Settlement Item."))
                if item.parent != self.settlement:
                    frappe.throw(_("Source row does not belong to the referenced settlement."))
                if self.commission_entry and item.commission_ledger_entry != self.commission_entry:
                    frappe.throw(_("Source row does not match the referenced commission entry."))
            if self.source_row and self.settlement_item and self.source_row != self.settlement_item:
                frappe.throw(_("Source row must match the settlement item."))
            total_debit = flt(je.total_debit)
            total_credit = flt(je.total_credit)
            if total_debit <= 0 or total_credit <= 0:
                frappe.throw(_("Journal Entry must contain debit and credit lines."))
            if abs(total_debit - total_credit) > 0.005:
                frappe.throw(_("Journal Entry debits and credits do not balance."))
            # Per-event voucher-total comparison is intentionally NOT performed
            # here: a single Journal Entry may cover multiple settlement items.
            # The complete voucher is reconciled once in the manual payout
            # workflow against the full allocation set. Only structural checks
            # remain at controller level.
            from sales_commission_management.sales_commission_management.payout.manual_payment import _resolve_payable_account
            payable = _resolve_payable_account(self.company)
            _payable_company = frappe.get_cached_value("Account", payable, "company")
            if _payable_company and self.company and _payable_company != self.company:
                frappe.throw(_("Commission payable account does not belong to the company."))
            account_names = [getattr(r, "account", None) for r in (je.accounts or [])]
            if payable not in account_names:
                frappe.throw(_("Journal Entry does not include the commission payable account."))
            has_cash_bank = False
            for acct in account_names:
                try:
                    acct_type = frappe.get_cached_value("Account", acct, "account_type")
                except Exception:
                    acct_type = None
                if acct_type in ("Cash", "Bank"):
                    has_cash_bank = True
                    break
            if not has_cash_bank:
                frappe.throw(_("Journal Entry must include a cash or bank account."))
            from sales_commission_management.sales_commission_management.payout.manual_payment import (
                resolve_commission_accounting_beneficiary,
            )
            expected_party = None
            expected_party_type = None
            if self.settlement:
                sett_doc = frappe.get_doc("Commission Settlement", self.settlement)
                resolved = resolve_commission_accounting_beneficiary(sett_doc)
                expected_party = resolved["party"]
                expected_party_type = resolved["party_type"]
            payable_debit = 0.0
            payable_credit = 0.0
            cash_credit = 0.0
            cash_debit = 0.0
            for row in (je.accounts or []):
                acct = getattr(row, "account", None)
                debit = flt(getattr(row, "debit", 0))
                credit = flt(getattr(row, "credit", 0))
                if acct == payable:
                    payable_debit += debit
                    payable_credit += credit
                    party = getattr(row, "party", None)
                    party_type = getattr(row, "party_type", None)
                    if not party:
                        frappe.throw(_("Commission payable account row must identify the beneficiary."))
                    if expected_party is not None and party != expected_party:
                        frappe.throw(_("Incorrect accounting beneficiary."))
                    if not party_type:
                        frappe.throw(_("Party type is required when a party is referenced."))
                    if expected_party_type is not None and party_type != expected_party_type:
                        frappe.throw(_("Incorrect accounting party type."))
                else:
                    try:
                        acct_type = frappe.get_cached_value("Account", acct, "account_type")
                        acct_company = frappe.get_cached_value("Account", acct, "company")
                    except Exception:
                        acct_type = None
                        acct_company = None
                    if acct_company and self.company and acct_company != self.company:
                        frappe.throw(_("Cash or bank account does not belong to the company."))
                    if acct_type in ("Cash", "Bank"):
                        cash_credit += credit
                        cash_debit += debit
            company_currency = self.company_currency
            if not company_currency:
                frappe.throw(_("Company currency is required."))
            company_precision = _currency_precision(company_currency)
            tolerance = (
                10 ** (-company_precision)
                if company_precision is not None and company_precision >= 0
                else 0.005
            )
            if payable_credit > tolerance:
                frappe.throw(_("Commission payable account must be debited, not credited."))
            if cash_debit > tolerance:
                frappe.throw(_("Cash or bank account must be credited, not debited."))
            # Per-event amount checks are intentionally relaxed here because a
            # single voucher (JE/PE) may cover multiple settlement items.  The
            # voucher-total reconciliation is performed in the manual_payment
            # workflow where the full allocation set is known.  Only structural
            # checks (payable account present, cash/bank present, party type)
            # remain at controller level.

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

        # Commission FX (commission -> company) snapshot consistency.
        if self.commission_fx_from_currency or self.commission_fx_to_currency or self.commission_fx_rate or self.commission_to_company_rate:
            if not self.commission_fx_from_currency or not self.commission_fx_to_currency:
                frappe.throw(_("Both commission FX from and to currencies are required."))
            if not self.commission_fx_rate:
                frappe.throw(_("Commission FX rate is required when commission FX currencies are set."))
            if not math.isfinite(self.commission_fx_rate) or self.commission_fx_rate <= 0:
                frappe.throw(_("Commission FX rate must be a finite positive number."))
            if self.commission_fx_from_currency == self.commission_fx_to_currency:
                if self.commission_fx_rate and self.commission_fx_rate != 1.0:
                    frappe.throw(_("Commission FX rate must be 1.0 when from and to currencies are the same."))
            if self.commission_to_company_rate is not None and self.commission_to_company_rate != 0:
                if not math.isfinite(self.commission_to_company_rate) or self.commission_to_company_rate <= 0:
                    frappe.throw(_("Commission to company rate must be a finite positive number."))

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
                "payment_fx_from_currency", "payment_fx_to_currency",
                "payment_fx_rate", "payment_fx_date", "payment_fx_source",
                "commission_to_company_rate", "commission_fx_from_currency",
                "commission_fx_to_currency", "commission_fx_rate",
                "commission_fx_date", "commission_fx_source",
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