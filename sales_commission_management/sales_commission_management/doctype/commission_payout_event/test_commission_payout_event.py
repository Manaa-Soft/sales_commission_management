from frappe.tests import IntegrationTestCase
import frappe


class TestCommissionPayoutEvent(IntegrationTestCase):
    def _make_event(self, event_key, **overrides):
        """Helper to create a payout event with sensible defaults."""
        data = {
            "doctype": "Commission Payout Event",
            "event_key": event_key,
            "event_type": "Payout",
            "posting_date": frappe.utils.nowdate(),
            "evidence_level": "ERP Payment Voucher",
            "settlement": "CS-2026-00001",
            "settlement_item": "row-001",
            "commission_entry": "CE-2026-00001",
            "company": "Test Company",
            "beneficiary_type": "Sales Person",
            "beneficiary": "SP-001",
            "source_doctype": "Payment Entry",
            "source_name": "ACC-PAY-001",
            "settlement_currency": "USD",
            "settlement_amount": 100,
            "commission_currency": "USD",
            "commission_amount": 100,
            "company_currency": "YER",
            "company_amount": 60000,
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
        # Create original payout event.
        orig = self._make_event("test-orig-001")
        orig.insert(ignore_permissions=True)
        orig.submit()
        # Recovery Reversal referencing a Payout should fail.
        rev = self._make_event(
            "test-rev-type-001",
            event_type="Recovery Reversal",
            reversal_of=orig.name,
            commission_amount=50,
        )
        self.assertRaises(frappe.ValidationError, rev.insert)

    def test_cumulative_reversal_limit(self):
        # Create original payout event.
        orig = self._make_event("test-orig-002", commission_amount=100)
        orig.insert(ignore_permissions=True)
        orig.submit()
        # First reversal of 60.
        rev1 = self._make_event(
            "test-rev-limit-001",
            event_type="Payout Reversal",
            reversal_of=orig.name,
            commission_amount=60,
        )
        rev1.insert(ignore_permissions=True)
        rev1.submit()
        # Second reversal of 50 should fail (60+50 > 100).
        rev2 = self._make_event(
            "test-rev-limit-002",
            event_type="Payout Reversal",
            reversal_of=orig.name,
            commission_amount=50,
        )
        self.assertRaises(frappe.ValidationError, rev2.insert)

    def test_fx_validation(self):
        # FX rate must be positive finite.
        doc = self._make_event("test-fx-001", fx_rate=-1.0, fx_from_currency="USD", fx_to_currency="YER")
        self.assertRaises(frappe.ValidationError, doc.insert)
        doc2 = self._make_event("test-fx-002", fx_rate=0, fx_from_currency="USD", fx_to_currency="YER")
        # fx_rate=0 is allowed (no conversion). But fx_from/to without valid rate should fail.
        doc3 = self._make_event("test-fx-003", fx_from_currency="USD", fx_to_currency="YER")
        self.assertRaises(frappe.ValidationError, doc3.insert)

    def test_no_cancellation(self):
        doc = self._make_event("test-cancel-001")
        doc.insert(ignore_permissions=True)
        doc.submit()
        self.assertRaises(frappe.ValidationError, doc.cancel)