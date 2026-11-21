from frappe.tests import IntegrationTestCase
import frappe


class TestCommissionPayoutEvent(IntegrationTestCase):
    def test_event_key_required(self):
        doc = frappe.get_doc({"doctype": "Commission Payout Event"})
        self.assertRaises(frappe.ValidationError, doc.insert)

    def test_immutability_after_submit(self):
        # Create and submit an event.
        doc = frappe.get_doc({
            "doctype": "Commission Payout Event",
            "event_key": "test-key-001",
            "event_type": "Payout",
            "posting_date": frappe.utils.nowdate(),
            "evidence_level": "ERP Payment Voucher",
            "settlement": "CS-2026-00001",
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
        })
        doc.insert(ignore_permissions=True)
        doc.submit()
        # Attempt to modify - should fail.
        doc.reload()
        doc.remarks = "modified"
        self.assertRaises(frappe.ValidationError, doc.save)

    def test_no_deletion(self):
        doc = frappe.get_doc({
            "doctype": "Commission Payout Event",
            "event_key": "test-key-002",
            "event_type": "Payout",
            "posting_date": frappe.utils.nowdate(),
            "evidence_level": "ERP Payment Voucher",
            "settlement": "CS-2026-00001",
            "commission_entry": "CE-2026-00001",
            "company": "Test Company",
            "beneficiary_type": "Sales Person",
            "beneficiary": "SP-001",
            "source_doctype": "Payment Entry",
            "source_name": "ACC-PAY-002",
            "settlement_currency": "USD",
            "settlement_amount": 50,
            "commission_currency": "USD",
            "commission_amount": 50,
            "company_currency": "YER",
            "company_amount": 30000,
        })
        doc.insert(ignore_permissions=True)
        self.assertRaises(frappe.ValidationError, doc.delete)

    def test_event_key_uniqueness(self):
        doc1 = frappe.get_doc({
            "doctype": "Commission Payout Event",
            "event_key": "test-key-dup",
            "event_type": "Payout",
            "posting_date": frappe.utils.nowdate(),
            "evidence_level": "ERP Payment Voucher",
            "settlement": "CS-2026-00001",
            "commission_entry": "CE-2026-00001",
            "company": "Test Company",
            "beneficiary_type": "Sales Person",
            "beneficiary": "SP-001",
            "source_doctype": "Payment Entry",
            "source_name": "ACC-PAY-003",
            "settlement_currency": "USD",
            "settlement_amount": 25,
            "commission_currency": "USD",
            "commission_amount": 25,
            "company_currency": "YER",
            "company_amount": 15000,
        })
        doc1.insert(ignore_permissions=True)
        doc2 = frappe.get_doc({
            "doctype": "Commission Payout Event",
            "event_key": "test-key-dup",
            "event_type": "Payout",
            "posting_date": frappe.utils.nowdate(),
            "evidence_level": "ERP Payment Voucher",
            "settlement": "CS-2026-00001",
            "commission_entry": "CE-2026-00001",
            "company": "Test Company",
            "beneficiary_type": "Sales Person",
            "beneficiary": "SP-001",
            "source_doctype": "Payment Entry",
            "source_name": "ACC-PAY-004",
            "settlement_currency": "USD",
            "settlement_amount": 25,
            "commission_currency": "USD",
            "commission_amount": 25,
            "company_currency": "YER",
            "company_amount": 15000,
        })
        self.assertRaises(frappe.DuplicateEntryError, doc2.insert)