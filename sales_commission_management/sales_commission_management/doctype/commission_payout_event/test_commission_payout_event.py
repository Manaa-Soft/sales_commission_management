from frappe.tests import IntegrationTestCase
import frappe
from frappe import _


class TestCommissionPayoutEvent(IntegrationTestCase):
    """Self-contained integration tests for Commission Payout Event.

    Covers event schema, immutability, reversal and financial validation.
    All test records are created in setUp and cleaned up in tearDown.
    No dependency on pre-existing site data.
    """

    def setUp(self):
        import uuid

        self.uid = uuid.uuid4().hex[:8]

        # --- Company. ---
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

        # --- Commission Settings (single doc). ---
        cs = frappe.get_single("Commission Settings")
        self._original_cs_enabled = getattr(cs, "enabled", None)
        self._original_cs_accounting_mode = getattr(cs, "accounting_mode", None)
        cs.enabled = 1
        cs.accounting_mode = "Subledger Until Settlement"
        cs.save(ignore_permissions=True)
        self.commission_settings = cs

        # --- Item "Commission". ---
        self._commission_item_created = not frappe.db.exists("Item", "Commission")
        if self._commission_item_created:
            frappe.get_doc({
                "doctype": "Item",
                "item_code": "Commission",
                "item_name": "Commission",
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "is_stock_item": 0,
            }).insert(ignore_permissions=True)
        self.commission_item = frappe.db.get_value("Item", "Commission", "name")
        assert self.commission_item is not None

        # --- Customer + real submitted Sales Invoice. ---
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

        # --- Commission Entry. ---
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

        # --- Supplier. ---
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
            frappe.db.set_value("Commission Settlement", sett_name, "supplier", self.supplier, update_modified=False)
        self.settlement = sett_name
        frappe.db.set_value("Commission Settlement", self.settlement, "supplier", self.supplier, update_modified=False)

        self.settlement_item = frappe.db.get_value(
            "Commission Settlement Item",
            {"parent": self.settlement, "commission_ledger_entry": self.commission_entry},
            "name",
        )

        # --- Purchase Invoice. ---
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

        # --- Payment Entry accounts. ---
        bank_account = frappe.db.get_value("Account", {"account_type": "Bank", "company": self.company}, "name")
        payable_account = frappe.db.get_value("Account", {"account_type": "Payable", "company": self.company}, "name")
        assert bank_account is not None
        assert payable_account is not None
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
        assert self.source_row is not None

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
        # Restore Commission Settings original values.
        try:
            cs = frappe.get_single("Commission Settings")
            if getattr(self, "_original_cs_enabled", None) is not None:
                cs.enabled = self._original_cs_enabled
            if getattr(self, "_original_cs_accounting_mode", None) is not None:
                cs.accounting_mode = self._original_cs_accounting_mode
            cs.save(ignore_permissions=True)
        except Exception:
            pass
        # Remove Commission Item only if created by this test.
        if getattr(self, "_commission_item_created", False):
            try:
                frappe.delete_doc("Item", "Commission", ignore_permissions=True)
            except Exception:
                pass

    def _make_event(self, event_key, **overrides):
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