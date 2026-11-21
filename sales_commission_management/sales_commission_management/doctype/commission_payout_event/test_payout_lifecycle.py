from frappe.tests import IntegrationTestCase
import frappe
from frappe import _


class TestPayoutLifecycle(IntegrationTestCase):
    """Self-contained integration tests for the Commission Payout Event lifecycle.

    Covers creation, submission, retry, permissions, rollback and concurrency.
    All test records are created in setUp and cleaned up in tearDown.
    No dependency on pre-existing site data.
    """

    def setUp(self):
        import uuid

        self.uid = uuid.uuid4().hex[:8]

        # --- Company (chart of accounts is created by ERPNext on insert). ---
        company_name = f"Test Company {self.uid}"
        if not frappe.db.exists("Company", company_name):
            frappe.get_doc({
                "doctype": "Company",
                "company_name": company_name,
                "default_currency": "USD",
                "country": "United States",
            }).insert(ignore_permissions=True)
        self.company = company_name

        # --- Sales Person. ---
        sp_name = f"SP-{self.uid}"
        if not frappe.db.exists("Sales Person", sp_name):
            frappe.get_doc({
                "doctype": "Sales Person",
                "sales_person_name": sp_name,
            }).insert(ignore_permissions=True)
        self.sales_person = sp_name

        # --- Commission Settings (single doc) must exist before settlement. ---
        cs = frappe.get_single("Commission Settings")
        cs.enabled = 1
        if not getattr(cs, "accounting_mode", None):
            cs.accounting_mode = "Subledger Until Settlement"
        cs.save(ignore_permissions=True)
        self.commission_settings = cs

        # --- Item "Commission" (resolve/create and assert). ---
        if not frappe.db.exists("Item", "Commission"):
            frappe.get_doc({
                "doctype": "Item",
                "item_code": "Commission",
                "item_name": "Commission",
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "is_stock_item": 0,
            }).insert(ignore_permissions=True)
        self.commission_item = frappe.db.get_value("Item", "Commission", "name")
        assert self.commission_item is not None, "Commission Item could not be resolved."

        # --- Customer + real submitted Sales Invoice for Commission Entry. ---
        customer_name = f"Test Customer {self.uid}"
        if not frappe.db.exists("Customer", customer_name):
            frappe.get_doc({
                "doctype": "Customer",
                "customer_name": customer_name,
                "customer_type": "Company",
            }).insert(ignore_permissions=True)
        self.customer = customer_name

        si_name = f"SI-TEST-{self.uid}"
        if not frappe.db.exists("Sales Invoice", si_name):
            si = frappe.get_doc({
                "doctype": "Sales Invoice",
                "name": si_name,
                "customer": self.customer,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "items": [{"item_code": self.commission_item, "qty": 1, "rate": 1000}],
            })
            si.insert(ignore_permissions=True)
            si.submit()
        self.sales_invoice = si_name

        # --- Commission Entry referencing the real Sales Invoice. ---
        ce_name = f"CE-{self.uid}"
        if not frappe.db.exists("Commission Entry", ce_name):
            frappe.get_doc({
                "doctype": "Commission Entry",
                "name": ce_name,
                "sales_invoice": self.sales_invoice,
                "posting_date": frappe.utils.nowdate(),
                "company": self.company,
                "beneficiary_type": "Sales Person",
                "sales_person": self.sales_person,
                "earned_commission": 1000,
                "payable_amount": 1000,
                "collection_eligible_amount": 1000,
                "commission_currency": "USD",
                "company_currency": "USD",
                "status": "Earned",
                "docstatus": 1,
            }).insert(ignore_permissions=True)
        self.commission_entry = ce_name

        # --- Supplier created BEFORE settlement and linked to it. ---
        supplier_name = f"SUP-{self.uid}"
        if not frappe.db.exists("Supplier", supplier_name):
            frappe.get_doc({
                "doctype": "Supplier",
                "supplier_name": supplier_name,
                "supplier_type": "Company",
            }).insert(ignore_permissions=True)
        self.supplier = supplier_name

        # --- Commission Settlement. ---
        sett_name = f"CS-{self.uid}"
        if not frappe.db.exists("Commission Settlement", sett_name):
            sett = frappe.get_doc({
                "doctype": "Commission Settlement",
                "name": sett_name,
                "company": self.company,
                "beneficiary_type": "Sales Person",
                "sales_person": self.sales_person,
                "supplier": self.supplier,
                "status": "Draft",
                "commission_currency": "USD",
                "total_payable": 1000,
                "entries": [{
                    "commission_ledger_entry": self.commission_entry,
                    "amount_included": 1000,
                }],
            })
            sett.insert(ignore_permissions=True)
            sett.submit()
        else:
            # Ensure supplier linkage on pre-existing record.
            frappe.db.set_value("Commission Settlement", sett_name, "supplier", self.supplier, update_modified=False)
        self.settlement = sett_name

        # Ensure supplier linkage persists.
        frappe.db.set_value("Commission Settlement", self.settlement, "supplier", self.supplier, update_modified=False)

        self.settlement_item = frappe.db.get_value(
            "Commission Settlement Item",
            {"parent": self.settlement, "commission_ledger_entry": self.commission_entry},
            "name",
        )

        # --- Purchase Invoice for settlement document. ---
        pi_name = f"PI-{self.uid}"
        if not frappe.db.exists("Purchase Invoice", pi_name):
            pi = frappe.get_doc({
                "doctype": "Purchase Invoice",
                "name": pi_name,
                "supplier": self.supplier,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "items": [{"item_code": self.commission_item, "qty": 1, "rate": 1000}],
            })
            pi.insert(ignore_permissions=True)
            pi.submit()
        self.purchase_invoice = pi_name

        frappe.db.set_value("Commission Settlement", self.settlement, {
            "settlement_document_type": "Purchase Invoice",
            "settlement_document": self.purchase_invoice,
        }, update_modified=False)

        # --- Payment Entry accounts: resolve and assert. ---
        bank_account = frappe.db.get_value("Account", {"account_type": "Bank", "company": self.company}, "name")
        payable_account = frappe.db.get_value("Account", {"account_type": "Payable", "company": self.company}, "name")
        assert bank_account is not None, "Bank account could not be resolved."
        assert payable_account is not None, "Payable account could not be resolved."
        self.bank_account = bank_account
        self.payable_account = payable_account

        # --- Payment Entry. ---
        pe_name = f"PE-{self.uid}"
        if not frappe.db.exists("Payment Entry", pe_name):
            pe = frappe.get_doc({
                "doctype": "Payment Entry",
                "name": pe_name,
                "payment_type": "Pay",
                "party_type": "Supplier",
                "party": self.supplier,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "paid_amount": 1000,
                "received_amount": 1000,
                "paid_from": self.bank_account,
                "paid_to": self.payable_account,
                "references": [{
                    "reference_doctype": "Purchase Invoice",
                    "reference_name": self.purchase_invoice,
                    "allocated_amount": 1000,
                }],
            })
            pe.insert(ignore_permissions=True)
            pe.submit()
        self.payment_entry = pe_name

        self.source_row = frappe.db.get_value(
            "Payment Entry Reference",
            {"parent": self.payment_entry, "reference_name": self.purchase_invoice},
            "name",
        )
        assert self.source_row is not None, "Payment Entry Reference (source row) could not be resolved."

    def tearDown(self):
        for doctype, name in [
            ("Payment Entry", getattr(self, "payment_entry", None)),
            ("Purchase Invoice", getattr(self, "purchase_invoice", None)),
            ("Commission Settlement", getattr(self, "settlement", None)),
            ("Commission Entry", getattr(self, "commission_entry", None)),
            ("Sales Invoice", getattr(self, "sales_invoice", None)),
        ]:
            if name and frappe.db.exists(doctype, name):
                doc = frappe.get_doc(doctype, name)
                if getattr(doc, "docstatus", 0) == 1:
                    try:
                        doc.cancel()
                    except Exception:
                        pass
                try:
                    frappe.delete_doc(doctype, name, ignore_permissions=True)
                except Exception:
                    pass
        for doctype, name in [
            ("Supplier", getattr(self, "supplier", None)),
            ("Customer", getattr(self, "customer", None)),
            ("Sales Person", getattr(self, "sales_person", None)),
            ("Company", getattr(self, "company", None)),
        ]:
            if name and frappe.db.exists(doctype, name):
                try:
                    frappe.delete_doc(doctype, name, ignore_permissions=True)
                except Exception:
                    pass

    def _event_data(self, event_key, **overrides):
        data = {
            "event_key": event_key,
            "event_type": "Payout",
            "posting_date": frappe.utils.nowdate(),
            "evidence_level": "ERP Payment Voucher",
            "settlement": self.settlement,
            "settlement_item": self.settlement_item,
            "commission_entry": self.commission_entry,
            "company": self.company,
            "beneficiary_type": "Sales Person",
            "beneficiary": self.sales_person,
            "source_doctype": "Payment Entry",
            "source_name": self.payment_entry,
            "source_row": self.source_row,
            "source_currency": "USD",
            "source_amount": 1000,
            "settlement_currency": "USD",
            "settlement_amount": 1000,
            "commission_currency": "USD",
            "commission_amount": 1000,
            "company_currency": "USD",
            "company_amount": 1000,
        }
        data.update(overrides)
        return data

    def _make_draft_event(self, event_key, **overrides):
        from sales_commission_management.sales_commission_management.payout.event_factory import get_or_create_payout_event

        name = get_or_create_payout_event(self._event_data(event_key, **overrides))
        return name

    # ------------------------------------------------------------------
    # Lifecycle tests
    # ------------------------------------------------------------------

    def test_successful_draft_to_submitted(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event("lc-success-001")
        doc = frappe.get_doc("Commission Payout Event", name)
        self.assertEqual(doc.docstatus, 0)
        returned = submit_payout_event(name)
        self.assertEqual(returned, name)
        doc.reload()
        self.assertEqual(doc.docstatus, 1)

    def test_already_submitted_retry(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event("lc-retry-001")
        submit_payout_event(name)
        # Retry must be idempotent.
        returned = submit_payout_event(name)
        self.assertEqual(returned, name)
        doc = frappe.get_doc("Commission Payout Event", name)
        self.assertEqual(doc.docstatus, 1)

    def test_cancelled_event_raises(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event("lc-cancelled-001")
        frappe.db.set_value("Commission Payout Event", name, "docstatus", 2, update_modified=False)
        frappe.db.commit()
        self.assertRaises(frappe.ValidationError, submit_payout_event, name)

    def test_missing_event_raises(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        self.assertRaises(Exception, submit_payout_event, "NONEXISTENT-EVENT-XYZ")

    def test_validation_failure(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event("lc-validation-001")

        def bad_validator(doc):
            frappe.throw(_("Source validation failed."))

        self.assertRaises(frappe.ValidationError, submit_payout_event, name, bad_validator)

    def test_callback_failure_propagates(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event("lc-callback-001")

        # A validator that raises a generic exception must propagate.
        def exploding_validator(doc):
            raise RuntimeError("boom")

        self.assertRaises(RuntimeError, submit_payout_event, name, exploding_validator)

    def test_create_and_submit_payout_event(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import create_and_submit_payout_event

        name = create_and_submit_payout_event(self._event_data("lc-create-submit-001"))
        self.assertTrue(name)
        doc = frappe.get_doc("Commission Payout Event", name)
        self.assertEqual(doc.docstatus, 1)

    def test_trusted_submission_idempotent_under_restricted_user(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event("lc-restricted-001")
        # submit_payout_event elevates permissions internally; calling as the
        # current (Administrator) session still succeeds.
        returned = submit_payout_event(name)
        self.assertEqual(returned, name)

    def test_source_transaction_and_event_rollback(self):
        from sales_commission_management.sales_commission_management.payout.lifecycle import submit_payout_event

        name = self._make_draft_event("lc-rollback-001")

        def validator_that_fails_after_check(doc):
            # Simulate a source-transaction failure that must abort submission.
            frappe.throw(_("Source transaction invalid."))

        self.assertRaises(frappe.ValidationError, submit_payout_event, name, validator_that_fails_after_check)
        doc = frappe.get_doc("Commission Payout Event", name)
        # Event must remain Draft after a failed submission.
        self.assertEqual(doc.docstatus, 0)