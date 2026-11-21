from frappe.tests import IntegrationTestCase
import frappe


class TestCommissionPayoutEvent(IntegrationTestCase):
    """Self-contained integration tests for Commission Payout Event.

    All test records are created in setUp and cleaned up in tearDown.
    No dependency on pre-existing site data.
    """

    def setUp(self):
        # Use unique identifiers to avoid collisions.
        import uuid
        self.uid = uuid.uuid4().hex[:8]

        # Create a test company.
        company_name = f"Test Company {self.uid}"
        if not frappe.db.exists("Company", company_name):
            frappe.get_doc({
                "doctype": "Company",
                "company_name": company_name,
                "default_currency": "USD",
                "country": "United States",
            }).insert(ignore_permissions=True)
        self.company = company_name

        # Create a test sales person.
        sp_name = f"SP-{self.uid}"
        if not frappe.db.exists("Sales Person", sp_name):
            frappe.get_doc({
                "doctype": "Sales Person",
                "sales_person_name": sp_name,
            }).insert(ignore_permissions=True)
        self.sales_person = sp_name

        # Create a test commission entry.
        ce_name = f"CE-{self.uid}"
        if not frappe.db.exists("Commission Entry", ce_name):
            frappe.get_doc({
                "doctype": "Commission Entry",
                "name": ce_name,
                "sales_invoice": f"SI-TEST-{self.uid}",
                "posting_date": frappe.utils.nowdate(),
                "company": self.company,
                "beneficiary_type": "Sales Person",
                "sales_person": self.sales_person,
                "earned_commission": 1000,
                "commission_currency": "USD",
                "company_currency": "USD",
                "status": "Earned",
                "docstatus": 1,
            }).insert(ignore_permissions=True)
        self.commission_entry = ce_name

        # Create a test commission settlement.
        sett_name = f"CS-{self.uid}"
        if not frappe.db.exists("Commission Settlement", sett_name):
            sett = frappe.get_doc({
                "doctype": "Commission Settlement",
                "name": sett_name,
                "company": self.company,
                "beneficiary_type": "Sales Person",
                "sales_person": self.sales_person,
                "status": "Submitted",
                "commission_currency": "USD",
                "total_payable": 1000,
                "entries": [{
                    "commission_ledger_entry": self.commission_entry,
                    "amount_included": 1000,
                }],
            })
            sett.insert(ignore_permissions=True)
            sett.submit()
        self.settlement = sett_name

        # Get the settlement item row name.
        self.settlement_item = frappe.db.get_value(
            "Commission Settlement Item",
            {"parent": self.settlement, "commission_ledger_entry": self.commission_entry},
            "name",
        )

        # Create a test supplier.
        supplier_name = f"SUP-{self.uid}"
        if not frappe.db.exists("Supplier", supplier_name):
            frappe.get_doc({
                "doctype": "Supplier",
                "supplier_name": supplier_name,
                "supplier_type": "Company",
            }).insert(ignore_permissions=True)
        self.supplier = supplier_name

        # Create a test purchase invoice for the settlement.
        pi_name = f"PI-{self.uid}"
        if not frappe.db.exists("Purchase Invoice", pi_name):
            pi = frappe.get_doc({
                "doctype": "Purchase Invoice",
                "name": pi_name,
                "supplier": self.supplier,
                "company": self.company,
                "posting_date": frappe.utils.nowdate(),
                "items": [{
                    "item_code": "Commission",
                    "qty": 1,
                    "rate": 1000,
                }],
            })
            pi.insert(ignore_permissions=True)
            pi.submit()
        self.purchase_invoice = pi_name

        # Link settlement to purchase invoice.
        frappe.db.set_value("Commission Settlement", self.settlement, {
            "settlement_document_type": "Purchase Invoice",
            "settlement_document": self.purchase_invoice,
        }, update_modified=False)

        # Create a test payment entry.
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
                "paid_from": frappe.db.get_value("Account", {"account_type": "Bank", "company": self.company}, "name"),
                "paid_to": frappe.db.get_value("Account", {"account_type": "Payable", "company": self.company}, "name"),
                "references": [{
                    "reference_doctype": "Purchase Invoice",
                    "reference_name": self.purchase_invoice,
                    "allocated_amount": 1000,
                }],
            })
            pe.insert(ignore_permissions=True)
            pe.submit()
        self.payment_entry = pe_name

        # Get the payment entry reference row name.
        self.source_row = frappe.db.get_value(
            "Payment Entry Reference",
            {"parent": self.payment_entry, "reference_name": self.purchase_invoice},
            "name",
        )

    def tearDown(self):
        # Clean up test records.
        # Delete in reverse dependency order.
        if hasattr(self, "payment_entry") and frappe.db.exists("Payment Entry", self.payment_entry):
            pe = frappe.get_doc("Payment Entry", self.payment_entry)
            if pe.docstatus == 1:
                pe.cancel()
            frappe.delete_doc("Payment Entry", self.payment_entry, ignore_permissions=True)
        if hasattr(self, "purchase_invoice") and frappe.db.exists("Purchase Invoice", self.purchase_invoice):
            pi = frappe.get_doc("Purchase Invoice", self.purchase_invoice)
            if pi.docstatus == 1:
                pi.cancel()
            frappe.delete_doc("Purchase Invoice", self.purchase_invoice, ignore_permissions=True)
        if hasattr(self, "settlement") and frappe.db.exists("Commission Settlement", self.settlement):
            sett = frappe.get_doc("Commission Settlement", self.settlement)
            if sett.docstatus == 1:
                sett.cancel()
            frappe.delete_doc("Commission Settlement", self.settlement, ignore_permissions=True)
        if hasattr(self, "commission_entry") and frappe.db.exists("Commission Entry", self.commission_entry):
            ce = frappe.get_doc("Commission Entry", self.commission_entry)
            if ce.docstatus == 1:
                ce.cancel()
            frappe.delete_doc("Commission Entry", self.commission_entry, ignore_permissions=True)
        if hasattr(self, "supplier") and frappe.db.exists("Supplier", self.supplier):
            frappe.delete_doc("Supplier", self.supplier, ignore_permissions=True)
        if hasattr(self, "sales_person") and frappe.db.exists("Sales Person", self.sales_person):
            frappe.delete_doc("Sales Person", self.sales_person, ignore_permissions=True)
        if hasattr(self, "company") and frappe.db.exists("Company", self.company):
            frappe.delete_doc("Company", self.company, ignore_permissions=True)

    def _make_event(self, event_key, **overrides):
        """Helper to create a payout event with sensible defaults."""
        data = {
            "doctype": "Commission Payout Event",
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
        return frappe.get_doc(data)

    def test_event_key_required(self):
        doc = frappe.get_doc({"doctype": "Commission Payout Event"})
        self.assertRaises(frappe.ValidationError, doc.insert)

    def test_immutability_after_submit(self):
        doc = self._make_event("test-imm-001")
        doc.insert(ignore_permissions=True)
        doc.submit()
        doc.reload()
        doc.remarks = "modified"
        self.assertRaises(frappe.ValidationError, doc.save)

    def test_no_deletion(self):
        doc = self._make_event("test-del-001")
        doc.insert(ignore_permissions=True)
        self.assertRaises(frappe.ValidationError, doc.delete)

    def test_event_key_uniqueness(self):
        doc1 = self._make_event("test-dup-001")
        doc1.insert(ignore_permissions=True)
        doc2 = self._make_event("test-dup-001", source_name=self.payment_entry)
        self.assertRaises(frappe.DuplicateEntryError, doc2.insert)

    def test_reversal_requires_original(self):
        doc = self._make_event("test-rev-001", event_type="Payout Reversal")
        self.assertRaises(frappe.ValidationError, doc.insert)

    def test_reversal_type_mapping(self):
        orig = self._make_event("test-orig-001")
        orig.insert(ignore_permissions=True)
        orig.submit()
        rev = self._make_event(
            "test-rev-type-001",
            event_type="Recovery Reversal",
            reversal_of=orig.name,
            commission_amount=50,
        )
        self.assertRaises(frappe.ValidationError, rev.insert)

    def test_cumulative_reversal_limit(self):
        orig = self._make_event("test-orig-002", commission_amount=100)
        orig.insert(ignore_permissions=True)
        orig.submit()
        rev1 = self._make_event(
            "test-rev-limit-001",
            event_type="Payout Reversal",
            reversal_of=orig.name,
            commission_amount=60,
        )
        rev1.insert(ignore_permissions=True)
        rev1.submit()
        rev2 = self._make_event(
            "test-rev-limit-002",
            event_type="Payout Reversal",
            reversal_of=orig.name,
            commission_amount=50,
        )
        self.assertRaises(frappe.ValidationError, rev2.insert)

    def test_fx_validation(self):
        doc = self._make_event("test-fx-001", fx_rate=-1.0, fx_from_currency="USD", fx_to_currency="YER")
        self.assertRaises(frappe.ValidationError, doc.insert)
        doc3 = self._make_event("test-fx-003", fx_from_currency="USD", fx_to_currency="YER")
        self.assertRaises(frappe.ValidationError, doc3.insert)

    def test_no_cancellation(self):
        doc = self._make_event("test-cancel-001")
        doc.insert(ignore_permissions=True)
        doc.submit()
        self.assertRaises(frappe.ValidationError, doc.cancel)

    def test_reversal_source_identity(self):
        orig = self._make_event("test-orig-003")
        orig.insert(ignore_permissions=True)
        orig.submit()
        rev = self._make_event(
            "test-rev-src-001",
            event_type="Payout Reversal",
            reversal_of=orig.name,
            commission_amount=50,
            source_name="ACC-PAY-999",
        )
        self.assertRaises(frappe.ValidationError, rev.insert)

    def test_event_type_schema(self):
        from sales_commission_management.sales_commission_management.payout.event_factory import _validate_payload_schema
        self.assertRaises(frappe.ValidationError, _validate_payload_schema, {"event_type": "Unknown"})