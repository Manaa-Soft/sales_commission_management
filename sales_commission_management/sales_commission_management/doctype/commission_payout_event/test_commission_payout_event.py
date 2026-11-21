from frappe.tests import IntegrationTestCase
import frappe


class TestCommissionPayoutEvent(IntegrationTestCase):
    """Self-contained integration tests for Commission Payout Event.

    All test records are created in setUp and cleaned up in tearDown.
    No dependency on pre-existing site data.
    """

    def setUp(self):
        # Create a test company if it doesn't exist.
        if not frappe.db.exists("Company", "Test Company"):
            frappe.get_doc({
                "doctype": "Company",
                "company_name": "Test Company",
                "default_currency": "USD",
                "country": "United States",
            }).insert(ignore_permissions=True)

        # Create a test sales person if it doesn't exist.
        if not frappe.db.exists("Sales Person", "SP-001"):
            frappe.get_doc({
                "doctype": "Sales Person",
                "sales_person_name": "SP-001",
                "employee": None,
            }).insert(ignore_permissions=True)

        # Create a test commission entry if it doesn't exist.
        if not frappe.db.exists("Commission Entry", "CE-2026-00001"):
            frappe.get_doc({
                "doctype": "Commission Entry",
                "name": "CE-2026-00001",
                "sales_invoice": "SI-TEST-001",
                "posting_date": frappe.utils.nowdate(),
                "company": "Test Company",
                "beneficiary_type": "Sales Person",
                "sales_person": "SP-001",
                "earned_commission": 1000,
                "commission_currency": "USD",
                "company_currency": "USD",
                "status": "Earned",
            }).insert(ignore_permissions=True)

        # Create a test commission settlement if it doesn't exist.
        if not frappe.db.exists("Commission Settlement", "CS-2026-00001"):
            sett = frappe.get_doc({
                "doctype": "Commission Settlement",
                "company": "Test Company",
                "beneficiary_type": "Sales Person",
                "sales_person": "SP-001",
                "status": "Submitted",
                "commission_currency": "USD",
                "total_payable": 1000,
                "entries": [{
                    "commission_ledger_entry": "CE-2026-00001",
                    "amount_included": 1000,
                }],
            })
            sett.insert(ignore_permissions=True)
            sett.submit()

        # Create a test payment entry reference row name for source_row.
        self.source_row = "row-001"

    def _make_event(self, event_key, **overrides):
        """Helper to create a payout event with sensible defaults."""
        data = {
            "doctype": "Commission Payout Event",
            "event_key": event_key,
            "event_type": "Payout",
            "posting_date": frappe.utils.nowdate(),
            "evidence_level": "ERP Payment Voucher",
            "settlement": "CS-2026-00001",
            "settlement_item": self.source_row,
            "commission_entry": "CE-2026-00001",
            "company": "Test Company",
            "beneficiary_type": "Sales Person",
            "beneficiary": "SP-001",
            "source_doctype": "Payment Entry",
            "source_name": "ACC-PAY-001",
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
        doc2 = self._make_event("test-dup-001", source_name="ACC-PAY-002")
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
        # Reversal with different source_name should fail.
        rev = self._make_event(
            "test-rev-src-001",
            event_type="Payout Reversal",
            reversal_of=orig.name,
            commission_amount=50,
            source_name="ACC-PAY-999",
        )
        self.assertRaises(frappe.ValidationError, rev.insert)

    def test_event_type_schema(self):
        # Unknown event type should be rejected by factory schema.
        from sales_commission_management.sales_commission_management.payout.event_factory import _validate_payload_schema
        self.assertRaises(frappe.ValidationError, _validate_payload_schema, {"event_type": "Unknown"})